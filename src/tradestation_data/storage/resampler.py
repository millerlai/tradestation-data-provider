from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import duckdb
import polars as pl

log = logging.getLogger(__name__)


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"


_INTERVALS: dict[str, str] = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "1h": "1 hour",
    "1d": "1 day",
}

_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "1d": 60 * 24,
}


def timeframe_to_minutes(timeframe: str | Timeframe) -> int:
    tf = str(timeframe)
    if tf not in _MINUTES:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Valid: {list(_MINUTES)}")
    return _MINUTES[tf]


_EMPTY_SCHEMA: dict[str, type[pl.DataType]] = {
    "bucket_start": pl.Datetime,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "tick_count": pl.Int32,
    "source": pl.Utf8,
    "symbol": pl.Utf8,
}


class Resampler:
    """
    On-demand resampler: reads Tier 1 ticks with DuckDB, produces OHLCV
    bars at the requested timeframe. Aggregation rules match
    docs/design.md §3.6.4:

      open       = first(price ORDER BY timestamp)
      high       = max(price)
      low        = min(price)
      close      = last(price ORDER BY timestamp)
      volume     = sum(volume)
      tick_count = count(*)

    Bar-from-bar fallback (``resample_from_bars``): when raw ticks are
    missing for a symbol but 1-minute bars are cached, HistoryStore can
    roll them up to the requested timeframe.

      open       = first(open  ORDER BY bucket_start)
      high       = max(high)
      low        = min(low)
      close      = last(close  ORDER BY bucket_start)
      volume     = sum(volume)
      tick_count = sum(tick_count)
    """

    def __init__(
        self,
        ticks_root: Path | str,
        bars_root: Path | str | None = None,
    ) -> None:
        self._ticks_root = Path(ticks_root)
        self._bars_root = Path(bars_root) if bars_root is not None else None

    def resample(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
    ) -> pl.DataFrame:
        tf = str(timeframe)
        if tf not in _INTERVALS:
            raise ValueError(f"Unsupported timeframe: {tf!r}. Valid: {list(_INTERVALS)}")

        files = sorted(self._ticks_root.glob(f"symbol={symbol}/date=*/ticks.parquet"))
        if not files:
            return pl.DataFrame(schema=_EMPTY_SCHEMA)

        interval = _INTERVALS[tf]
        pattern = (self._ticks_root / f"symbol={symbol}" / "date=*" / "ticks.parquet").as_posix()
        sql = f"""
        SELECT
          time_bucket(INTERVAL '{interval}', timestamp) AS bucket_start,
          first(price ORDER BY timestamp) AS open,
          max(price)                      AS high,
          min(price)                      AS low,
          last(price ORDER BY timestamp)  AS close,
          sum(volume)                     AS volume,
          CAST(count(*) AS INTEGER)       AS tick_count,
          first(source)                   AS source
        FROM read_parquet(?, hive_partitioning = true)
        WHERE timestamp BETWEEN ? AND ?
        GROUP BY bucket_start
        ORDER BY bucket_start
        """
        con = duckdb.connect()
        try:
            # Force UTC so TIMESTAMPTZ results don't inherit the system
            # timezone (Windows lacks tzdata, and polars refuses to parse
            # non-UTC zones without it).
            con.execute("SET TimeZone='UTC'")
            arrow_tbl = con.execute(sql, [pattern, start, end]).arrow()
        finally:
            con.close()

        df = pl.from_arrow(arrow_tbl)
        assert isinstance(df, pl.DataFrame)
        return df.with_columns(pl.lit(symbol).alias("symbol"))

    def resample_from_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
        *,
        source_timeframe: str = "1m",
    ) -> pl.DataFrame:
        if self._bars_root is None:
            return pl.DataFrame(schema=_EMPTY_SCHEMA)

        tf = str(timeframe)
        if tf not in _INTERVALS:
            raise ValueError(f"Unsupported timeframe: {tf!r}. Valid: {list(_INTERVALS)}")
        if tf == source_timeframe:
            raise ValueError(
                f"source_timeframe must differ from target ({tf}); "
                "caller should read the cache directly."
            )

        src_dir = self._bars_root / f"timeframe={source_timeframe}" / f"symbol={symbol}"
        files = sorted(src_dir.glob("date=*/bars.parquet"))
        if not files:
            return pl.DataFrame(schema=_EMPTY_SCHEMA)

        interval = _INTERVALS[tf]
        pattern = (src_dir / "date=*" / "bars.parquet").as_posix()
        # Alias the bucketed column to ``bkt`` to avoid an ambiguous
        # GROUP BY against the source's ``bucket_start`` column.
        sql = f"""
        SELECT
          time_bucket(INTERVAL '{interval}', bucket_start) AS bkt,
          first(open  ORDER BY bucket_start) AS open,
          max(high)                          AS high,
          min(low)                           AS low,
          last(close  ORDER BY bucket_start) AS close,
          sum(volume)                        AS volume,
          CAST(sum(tick_count) AS INTEGER)   AS tick_count,
          first(source)                      AS source
        FROM read_parquet(?, hive_partitioning = true)
        WHERE bucket_start BETWEEN ? AND ?
        GROUP BY bkt
        ORDER BY bkt
        """
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            arrow_tbl = con.execute(sql, [pattern, start, end]).arrow()
        finally:
            con.close()
        df = pl.from_arrow(arrow_tbl)
        assert isinstance(df, pl.DataFrame)
        if "bkt" in df.columns:
            df = df.rename({"bkt": "bucket_start"})
        return df.with_columns(pl.lit(symbol).alias("symbol"))
