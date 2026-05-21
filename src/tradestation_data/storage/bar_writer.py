from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar

log = logging.getLogger(__name__)

BAR_SCHEMA: pa.Schema = pa.schema(
    [
        # bucket_start (UTC) and bucket_start_et (America/New_York) describe
        # the same instant in different zones; both are persisted so tooling
        # can filter on either without a runtime conversion. ET is the
        # authoritative basis for session-time decisions downstream.
        pa.field("bucket_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field(
            "bucket_start_et",
            pa.timestamp("us", tz="America/New_York"),
            nullable=False,
        ),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.int64(), nullable=False),
        pa.field("tick_count", pa.int32(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)


@dataclass(slots=True)
class _DayPartition:
    symbol: str
    day: date
    writer: pq.ParquetWriter | None = None

    def path(self, root: Path, timeframe: str) -> Path:
        return (
            root
            / f"timeframe={timeframe}"
            / f"symbol={self.symbol}"
            / f"date={self.day.isoformat()}"
            / "bars.parquet"
        )


class BarWriter:
    """
    Tier 2 1-min bar cache writer.

    Layout: `{root}/timeframe=1m/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet`

    Unlike `TickWriter`, bars are not buffered — one bar per symbol per
    minute is tiny (~15 rows/min across the universe). We write each
    emitted bar immediately so a mid-session crash costs at most one
    minute of bar cache (which is recoverable anyway from Tier 1 ticks).
    """

    def __init__(
        self,
        root: Path | str,
        *,
        timeframe: str = "1m",
        compression: str = "zstd",
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._timeframe = timeframe
        self._compression = compression
        self._partitions: dict[tuple[str, date], _DayPartition] = {}
        self._closed = False

    def write(self, bar: Bar) -> None:
        if self._closed:
            raise RuntimeError("BarWriter is closed")
        # Partition on the ET calendar date so all bars from one US trading
        # session land in a single date= directory, regardless of the UTC
        # rollover (which splits a session at 19:00/20:00 ET otherwise).
        partition_day = bar.bucket_start_et.date()
        key = (bar.symbol, partition_day)
        part = self._partitions.get(key)
        if part is None:
            part = _DayPartition(symbol=bar.symbol, day=partition_day)
            self._partitions[key] = part
        self._append(part, bar)

    def _append(self, part: _DayPartition, bar: Bar) -> None:
        table = _bars_to_table([bar])
        if part.writer is None:
            path = part.path(self._root, self._timeframe)
            path.parent.mkdir(parents=True, exist_ok=True)
            part.writer = pq.ParquetWriter(path, BAR_SCHEMA, compression=self._compression)
        part.writer.write_table(table)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for part in self._partitions.values():
            if part.writer is not None:
                part.writer.close()
                part.writer = None

    def __enter__(self) -> BarWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _bars_to_table(bars: list[Bar]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "bucket_start": [b.bucket_start for b in bars],
            "bucket_start_et": [b.bucket_start_et for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
            "tick_count": [b.tick_count for b in bars],
            "source": [b.source for b in bars],
        },
        schema=BAR_SCHEMA,
    )
