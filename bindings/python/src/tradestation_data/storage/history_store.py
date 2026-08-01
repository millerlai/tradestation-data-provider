from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pyarrow as pa

from tradestation_data.domain.timeframe import (
    SINGLE_FILE_TIMEFRAMES,
    SUPPORTED_TIMEFRAMES,
    Timeframe,
)
from tradestation_data.storage.bar_writer import BAR_SCHEMA
from tradestation_data.storage.tick_writer import TICK_SCHEMA

_ET_TZ = ZoneInfo("America/New_York")

# polars accepts a dtype as either the class or an instance, and the hive
# columns below are named with the class form.
_Dtype = type[pl.DataType] | pl.DataType


def _as_utc(value: datetime) -> datetime:
    """Resolve a caller's instant, reading a naive one as ET.

    This is a US-equity API — sessions, holidays and `date=` partitions are
    all defined in America/New_York, so a bare ``datetime(2026, 4, 20, 9, 30)``
    means the open. It once meant 05:30 ET, which nobody chose: the query
    engine ran its session in UTC for determinism and that leaked out into the
    API. An aware input is unambiguous and is left alone.
    See contract/semantics.md §2.3.
    """
    return value.replace(tzinfo=_ET_TZ).astimezone(UTC) if value.tzinfo is None else value


def _empty(schema: pa.Schema, hive: dict[str, _Dtype]) -> pl.DataFrame:
    """An empty frame shaped exactly like a populated answer from the same path.

    Built from the writer's own Arrow schema rather than a second hand-kept
    copy, so the two cannot drift. The hive columns are appended because
    ``scan_parquet(hive_partitioning=True)`` materialises them from the
    directory names, and contract/semantics.md §2.4 requires the empty and
    non-empty answers of one call to agree on columns, order and dtype —
    otherwise a caller stacking results across days breaks on the first day a
    symbol did not trade, instead of getting one fewer row.
    """
    frame = pl.from_arrow(schema.empty_table())
    assert isinstance(frame, pl.DataFrame)
    return frame.with_columns([pl.lit(None, dtype=t).alias(n) for n, t in hive.items()])


class HistoryStore:
    """Read-side view over the Parquet store.

    Layout, written by TickWriter / BarWriter:

      {root}/ticks/symbol=.../date=.../ticks.parquet
      {root}/bars/timeframe=<tf>/symbol=.../date=.../bars.parquet
      {root}/bars/timeframe=1d/symbol=.../bars.parquet    — one file, no date=

    THIS CLASS ONLY READS. It does not resample, aggregate, cache, backfill
    or repair. A query with nothing behind it returns zero rows; it never
    computes a plausible-looking substitute and never writes. Everything on
    disk came off the wire, so a bar that is not there is a bar TradeStation
    did not publish, and inventing one would be indistinguishable from the
    real thing the moment it was persisted.

    Consumers wanting derived intervals either chart them in TradeStation, or
    build them from what is stored here on their own terms.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._ticks_root = self._root / "ticks"
        self._bars_root = self._root / "bars"

    def load_ticks(self, symbol: str, start: datetime, end: datetime) -> pl.DataFrame:
        pattern = self._ticks_root / f"symbol={symbol}" / "date=*" / "ticks.parquet"
        return self._read(
            pattern,
            time_column="timestamp",
            start=start,
            end=end,
            schema=TICK_SCHEMA,
            hive={"symbol": pl.String, "date": pl.Date},
        )

    def load_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
    ) -> pl.DataFrame:
        tf = str(timeframe)
        if tf not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {tf!r}. Valid: {sorted(SUPPORTED_TIMEFRAMES)}")

        base = self._bars_root / f"timeframe={tf}" / f"symbol={symbol}"
        hive: dict[str, _Dtype] = {"timeframe": pl.String, "symbol": pl.String}
        if tf in SINGLE_FILE_TIMEFRAMES:
            # One file per symbol, no date= level — a day partition of daily
            # bars is a single row inside a file whose schema and footer cost
            # ~2.9 KB regardless.
            pattern = base / "bars.parquet"
        else:
            pattern = base / "date=*" / "bars.parquet"
            hive["date"] = pl.Date

        return self._read(
            pattern,
            time_column="bucket_start",
            start=start,
            end=end,
            schema=BAR_SCHEMA,
            hive=hive,
        )

    def _read(
        self,
        pattern: Path,
        *,
        time_column: str,
        start: datetime,
        end: datetime,
        schema: pa.Schema,
        hive: dict[str, _Dtype],
    ) -> pl.DataFrame:
        # scan_parquet raises rather than returning nothing when a glob
        # matches no file, so this check is what turns "never recorded" into
        # the same zero-row answer as "recorded, but quiet". Both are ordinary
        # questions and neither is an error — §2.4.
        if not _any_file(pattern):
            return _empty(schema, hive)

        lo, hi = _as_utc(start), _as_utc(end)
        return (
            pl.scan_parquet(pattern.as_posix(), hive_partitioning=True)
            .filter(pl.col(time_column).is_between(lo, hi))
            .sort(time_column)
            .collect()
        )


def _any_file(pattern: Path) -> bool:
    """Whether a writer-shaped path matches anything on disk.

    Both layouts have at most one wildcard and it is always the `date=`
    directory, so this needs no general glob expansion.
    """
    if "*" not in pattern.as_posix():
        return pattern.is_file()
    symbol_dir = pattern.parent.parent
    return symbol_dir.is_dir() and any(symbol_dir.glob(f"{pattern.parent.name}/{pattern.name}"))
