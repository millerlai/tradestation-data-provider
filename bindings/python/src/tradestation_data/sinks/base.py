from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick


@runtime_checkable
class Sink(Protocol):
    """Output-side consumer of ingested :class:`Tick` and :class:`Bar`.

    A sink represents one output destination/format. The ingestion
    runtime fans every event out to every registered sink via
    :class:`SinkPipeline`.

    Implementations should keep ``on_tick`` / ``on_bar`` non-blocking;
    they run inside the ingest loop. Heavy work (large parquet writes,
    network IO) belongs behind a buffer that :meth:`should_flush` /
    :meth:`flush` drives from the runtime's flush loop.

    Contract:
      * ``name`` — stable identifier from ``sinks.yaml``. Used for
        logging and for :func:`tradestation_data.sinks.callback.get_sink`.
      * ``on_tick`` / ``on_bar`` — called once per emitted event in
        ingest order. Sinks that do not care about one of these may
        implement it as a no-op.
      * ``should_flush`` — return True when ``flush`` should be called
        from the runtime's periodic flush loop. Default False for
        sinks that write inline (no buffer).
      * ``flush`` — synchronously persist any pending state. May be a
        no-op.
      * ``close`` — final flush + release resources. Must be idempotent.
    """

    name: str

    def on_tick(self, tick: Tick) -> None: ...

    def on_bar(self, bar: Bar) -> None: ...

    def should_flush(self) -> bool: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class BaseSink:
    """Convenience base providing no-op defaults for the optional hooks.

    Sinks may subclass this and override only the events they care
    about. Sticking to the :class:`Sink` :class:`typing.Protocol`
    (duck-typed) is also fine — this class exists purely as ergonomic
    sugar so trivial sinks can be one method long.
    """

    name: str = ""

    def on_tick(self, tick: Tick) -> None:
        return None

    def on_bar(self, bar: Bar) -> None:
        return None

    def should_flush(self) -> bool:
        return False

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None
