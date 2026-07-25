"""Callback sink — dispatch ticks/bars to user-registered Python functions.

The runtime declares the sink in ``sinks.yaml``; user code then looks
it up by name and registers callbacks dynamically::

    from tradestation_data.sinks.callback import get_sink

    sink = get_sink("dispatch")    # name from sinks.yaml

    def my_bar_handler(bar):
        print(bar.symbol, bar.close)

    handle = sink.on("SPY", "bar", my_bar_handler)
    sink.on_any("tick", lambda t: log_tick(t))

    # Later:
    sink.off(handle)               # remove specific handler

Callbacks are invoked synchronously inside the ingest loop — keep them
fast (microseconds). Spawn ``asyncio.create_task`` / a thread inside
the callback if you need to do real work.

Exceptions raised by a callback are caught and logged; other
callbacks for the same event still fire.
"""

from __future__ import annotations

import itertools
import logging
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.base import BaseSink

log = logging.getLogger(__name__)

EventKind = Literal["tick", "bar"]
TickCallback = Callable[[Tick], None]
BarCallback = Callable[[Bar], None]
AnyCallback = TickCallback | BarCallback


# Module-level registry of CallbackSink instances keyed by their
# ``name``. Uses a WeakValueDictionary so an unreferenced sink is
# GC'd; we deliberately weak-ref so a test that builds a pipeline,
# tears it down, and builds another with the same name doesn't see
# the old instance.
_REGISTRY: weakref.WeakValueDictionary[str, CallbackSink] = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def get_sink(name: str) -> CallbackSink:
    """Look up a :class:`CallbackSink` previously declared in ``sinks.yaml``.

    Raises :class:`KeyError` if no sink with that name is currently
    alive — typically means the runtime hasn't started yet, or
    ``sinks.yaml`` declares a different name than the one passed.
    """
    with _REGISTRY_LOCK:
        sink = _REGISTRY.get(name)
    if sink is None:
        raise KeyError(
            f"CallbackSink {name!r} is not registered — "
            "is the ingestion runtime running and is the name correct?"
        )
    return sink


@dataclass(slots=True)
class Handle:
    """Opaque token returned by :meth:`CallbackSink.on` / :meth:`on_any`.

    Pass it back to :meth:`CallbackSink.off` to unregister the
    callback. Comparing handles by identity is the supported contract;
    handles are not meaningful across process restarts.
    """

    id: int
    kind: EventKind
    symbol: str | None  # None means catch-all


@dataclass(slots=True)
class _Entry:
    handle: Handle
    fn: AnyCallback
    # symbol is cached on the handle; duplicated here so the dispatch
    # loop doesn't need to read through to handle.symbol on every event.
    symbol: str | None = field(init=False)

    def __post_init__(self) -> None:
        self.symbol = self.handle.symbol


class CallbackSink(BaseSink):
    """Dispatch ticks/bars to dynamically registered Python callbacks.

    Thread-safety: ``on`` / ``on_any`` / ``off`` use an internal lock
    so user code can register from any thread. Dispatch (``on_tick`` /
    ``on_bar``) takes the same lock briefly to snapshot the callback
    list, then releases it — callbacks themselves run unlocked so a
    slow callback does not block registration.
    """

    def __init__(self, *, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._tick_handlers: list[_Entry] = []
        self._bar_handlers: list[_Entry] = []
        self._id_counter = itertools.count(1)
        with _REGISTRY_LOCK:
            _REGISTRY[name] = self

    # ---- registration ----------------------------------------------------

    def on(
        self,
        symbol: str,
        kind: EventKind,
        fn: AnyCallback,
    ) -> Handle:
        """Register ``fn`` to fire when events of ``kind`` arrive for ``symbol``.

        ``kind`` is ``"tick"`` or ``"bar"``. Returns a :class:`Handle`
        that can be passed to :meth:`off` to deregister.
        """
        return self._register(symbol=symbol, kind=kind, fn=fn)

    def on_any(self, kind: EventKind, fn: AnyCallback) -> Handle:
        """Like :meth:`on` but fires for *every* symbol."""
        return self._register(symbol=None, kind=kind, fn=fn)

    def off(self, handle: Handle) -> bool:
        """Remove a previously registered callback. Returns True if removed."""
        target_list = self._handlers_for(handle.kind)
        with self._lock:
            for i, entry in enumerate(target_list):
                if entry.handle is handle:
                    del target_list[i]
                    return True
        return False

    def _register(
        self,
        *,
        symbol: str | None,
        kind: EventKind,
        fn: AnyCallback,
    ) -> Handle:
        if kind not in ("tick", "bar"):
            raise ValueError(f"kind must be 'tick' or 'bar', got {kind!r}")
        if not callable(fn):
            raise TypeError(f"callback must be callable, got {type(fn).__name__}")
        handle = Handle(id=next(self._id_counter), kind=kind, symbol=symbol)
        entry = _Entry(handle=handle, fn=fn)
        with self._lock:
            self._handlers_for(kind).append(entry)
        return handle

    def _handlers_for(self, kind: EventKind) -> list[_Entry]:
        return self._tick_handlers if kind == "tick" else self._bar_handlers

    # ---- Sink protocol ---------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        with self._lock:
            entries = list(self._tick_handlers)
        for entry in entries:
            if entry.symbol is not None and entry.symbol != tick.symbol:
                continue
            try:
                entry.fn(tick)  # type: ignore[arg-type]
            except Exception:
                log.exception(
                    "callback_failed",
                    extra={
                        "sink": self.name,
                        "kind": "tick",
                        "symbol": tick.symbol,
                        "handle_id": entry.handle.id,
                    },
                )

    def on_bar(self, bar: Bar) -> None:
        with self._lock:
            entries = list(self._bar_handlers)
        for entry in entries:
            if entry.symbol is not None and entry.symbol != bar.symbol:
                continue
            try:
                entry.fn(bar)  # type: ignore[arg-type]
            except Exception:
                log.exception(
                    "callback_failed",
                    extra={
                        "sink": self.name,
                        "kind": "bar",
                        "symbol": bar.symbol,
                        "handle_id": entry.handle.id,
                    },
                )

    def close(self) -> None:
        with self._lock:
            self._tick_handlers.clear()
            self._bar_handlers.clear()
        with _REGISTRY_LOCK:
            # If our entry in the registry still points to us, drop it.
            # weakref dict handles this on GC too, but doing it eagerly
            # makes get_sink() raise KeyError right after close().
            current = _REGISTRY.get(self.name)
            if current is self:
                del _REGISTRY[self.name]
