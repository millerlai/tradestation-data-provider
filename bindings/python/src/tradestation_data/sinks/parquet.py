"""Built-in Parquet sinks.

These are thin adapters over :class:`BarWriter` / :class:`TickWriter` —
the on-disk layout, schema, and flush semantics are unchanged from the
pre-sink runtime. They exist so the sink-driven runtime can keep doing
exactly what the old `tick_writer` / `bar_writer` parameters did, just
behind the :class:`Sink` protocol.
"""

from __future__ import annotations

from pathlib import Path

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.base import BaseSink
from tradestation_data.storage.bar_writer import BarWriter
from tradestation_data.storage.tick_writer import TickWriter


class ParquetBarSink(BaseSink):
    """Write closed 1m bars to Hive-partitioned Parquet.

    Layout: ``{root}/timeframe={timeframe}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet``.
    Bars are written inline (no buffer), matching the previous BarWriter
    behaviour — one minute per symbol is tiny, and crash-loss is bounded
    to that single minute (Tier-1 ticks are the recovery source anyway).
    """

    def __init__(
        self,
        *,
        name: str,
        root: Path | str,
        timeframe: str = "1m",
        compression: str = "zstd",
    ) -> None:
        self.name = name
        self._writer = BarWriter(root, timeframe=timeframe, compression=compression)

    def on_bar(self, bar: Bar) -> None:
        self._writer.write(bar)

    def close(self) -> None:
        self._writer.close()


class ParquetTickSink(BaseSink):
    """Buffered tick → Parquet writer.

    Layout: ``{root}/symbol={SYM}/date={YYYY-MM-DD}/ticks.parquet``.
    Buffered with the two existing flush triggers (max ticks / max
    seconds since oldest buffered tick). The pipeline's flush loop
    drives :meth:`flush` via :meth:`should_flush`.
    """

    def __init__(
        self,
        *,
        name: str,
        root: Path | str,
        max_buffered_ticks: int = 10_000,
        max_flush_seconds: float = 30.0,
        compression: str = "zstd",
    ) -> None:
        self.name = name
        self._writer = TickWriter(
            root,
            max_buffered_ticks=max_buffered_ticks,
            max_flush_seconds=max_flush_seconds,
            compression=compression,
        )

    def on_tick(self, tick: Tick) -> None:
        self._writer.write(tick)

    def should_flush(self) -> bool:
        return self._writer.should_flush()

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()
