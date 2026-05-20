from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.base import Sink

log = logging.getLogger(__name__)


class SinkPipeline:
    """Fan-out container that broadcasts every event to every sink.

    Failure of one sink is logged but does not stop the others — the
    runtime previously did this inline (one ``try/except`` per writer
    in ``IngestionRuntime._handle_tick`` / ``_on_closed_bar``); this
    class centralises that policy.

    The pipeline owns its sinks: :meth:`close` closes them in
    construction order. Construction order matters for output —
    e.g. an in-memory sink that captures events for assertions runs
    before a parquet sink that takes longer to write, so a test can
    rely on the buffer being populated first.
    """

    def __init__(self, sinks: Iterable[Sink] = ()) -> None:
        self._sinks: list[Sink] = list(sinks)
        self._closed = False

    # ---- introspection ---------------------------------------------------

    def __iter__(self) -> Iterator[Sink]:
        return iter(self._sinks)

    def __len__(self) -> int:
        return len(self._sinks)

    def get(self, name: str) -> Sink | None:
        """Return the sink with this ``name`` or ``None`` if not found."""
        for sink in self._sinks:
            if getattr(sink, "name", None) == name:
                return sink
        return None

    # ---- event dispatch --------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        for sink in self._sinks:
            try:
                sink.on_tick(tick)
            except Exception:
                log.exception(
                    "sink_on_tick_failed",
                    extra={"sink": getattr(sink, "name", type(sink).__name__),
                           "symbol": tick.symbol},
                )

    def on_bar(self, bar: Bar) -> None:
        for sink in self._sinks:
            try:
                sink.on_bar(bar)
            except Exception:
                log.exception(
                    "sink_on_bar_failed",
                    extra={"sink": getattr(sink, "name", type(sink).__name__),
                           "symbol": bar.symbol},
                )

    # ---- buffered-sink lifecycle ----------------------------------------

    def has_pending_flush(self) -> bool:
        """True if any sink reports it wants to be flushed."""
        for sink in self._sinks:
            try:
                if sink.should_flush():
                    return True
            except Exception:
                log.exception(
                    "sink_should_flush_failed",
                    extra={"sink": getattr(sink, "name", type(sink).__name__)},
                )
        return False

    def flush_pending(self) -> None:
        """Call ``flush()`` on every sink whose ``should_flush()`` is True."""
        for sink in self._sinks:
            try:
                if not sink.should_flush():
                    continue
                sink.flush()
            except Exception:
                log.exception(
                    "sink_flush_failed",
                    extra={"sink": getattr(sink, "name", type(sink).__name__)},
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Close in construction order so a sink that depends on another's
        # final state (rare, but e.g. an audit sink reading a buffer)
        # still sees that state before the upstream sink tears down.
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                log.exception(
                    "sink_close_failed",
                    extra={"sink": getattr(sink, "name", type(sink).__name__)},
                )
