from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl

from tradestation_data.domain.bar import derived_source
from tradestation_data.domain.timeframe import TIMEFRAME_MINUTES, Timeframe

log = logging.getLogger(__name__)

# DuckDB INTERVAL literals, derived from the one minutes table so a new
# Timeframe member cannot be half-added. `1d` is spelled as a calendar day
# because it is bucketed on the ET clock, where a day is 23 or 25 hours twice
# a year.
_INTERVALS: dict[str, str] = {
    tf: ("1 day" if tf == Timeframe.D1 else f"{minutes} minutes")
    for tf, minutes in TIMEFRAME_MINUTES.items()
}

# ---- bucket alignment ----------------------------------------------------
#
# Buckets are anchored to the trading session, not to the Unix epoch. See
# contract/semantics.md §2.2, and keep this in step with
# domain.timeframe.align_bucket_start — that function is the Python twin of
# the SQL below.
#
# Anchoring to the epoch (what a bare time_bucket does) happens to be right for
# 5m/15m/30m — the ET offset is a whole number of hours and 09:30 is a multiple
# of 30 minutes — but it is wrong for the two longer frames:
#
#   1h  epoch-aligned gives 09:00 ET buckets, so the first regular-session bar
#       covers only 09:30-10:00: half a bar that looks like a whole one.
#   1d  epoch-aligned splits on UTC midnight, which falls at 20:00 ET — exactly
#       the end of the extended session. Post-market activity lands on the
#       following day.
_ANCHOR_DATE = "2000-01-03"
_SESSION_DATE_CUTOFF_LOCAL = "04:00:00"

# 09:30 ET on the anchor date, as UTC. See domain.timeframe for why the
# intraday grid is laid out from a UTC origin instead of an ET one.
_INTRADAY_ORIGIN_UTC_SQL = f"TIMESTAMPTZ '{_ANCHOR_DATE} 14:30:00+00'"

_ET_ZONE = "America/New_York"


def _bucket_expr(column: str, timeframe: str) -> str:
    """SQL that buckets a TIMESTAMPTZ column, session-anchored, back to UTC.

    Intraday frames (1m..1h) bucket in UTC from a fixed origin. All of them
    divide the one-hour DST shift evenly, so a UTC grid still lands on 09:30 ET
    on both sides of a transition — and, unlike an ET wall-clock grid, it stays
    unambiguous inside the 01:00-02:00 fold, where two instants an hour apart
    share one wall-clock reading.

    `1d` has to bucket on the ET clock instead, because a calendar day is 23 or
    25 hours twice a year. That is safe: the 04:00 ET anchor sits outside the
    fold, so the bucket edge converts back to UTC unambiguously.
    """
    interval = _INTERVALS[timeframe]
    if timeframe == Timeframe.D1:
        origin = f"TIMESTAMP '{_ANCHOR_DATE} {_SESSION_DATE_CUTOFF_LOCAL}'"
        local = f"timezone('{_ET_ZONE}', {column})"
        bucketed = f"time_bucket(INTERVAL '{interval}', {local}, {origin})"
        return f"timezone('{_ET_ZONE}', {bucketed})"
    return f"time_bucket(INTERVAL '{interval}', {column}, {_INTRADAY_ORIGIN_UTC_SQL})"


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

        pattern = (self._ticks_root / f"symbol={symbol}" / "date=*" / "ticks.parquet").as_posix()
        sql = f"""
        SELECT
          {_bucket_expr("timestamp", tf)} AS bucket_start,
          first(price ORDER BY timestamp) AS open,
          max(price)                      AS high,
          min(price)                      AS low,
          last(price ORDER BY timestamp)  AS close,
          sum(volume)                     AS volume,
          CAST(count(*) AS INTEGER)       AS tick_count,
          ? AS source
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
            arrow_tbl = con.execute(sql, [derived_source("ticks"), pattern, start, end]).arrow()
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

        pattern = (src_dir / "date=*" / "bars.parquet").as_posix()
        # Alias the bucketed column to ``bkt`` to avoid an ambiguous
        # GROUP BY against the source's ``bucket_start`` column.
        sql = f"""
        SELECT
          {_bucket_expr("bucket_start", tf)} AS bkt,
          first(open  ORDER BY bucket_start) AS open,
          max(high)                          AS high,
          min(low)                           AS low,
          last(close  ORDER BY bucket_start) AS close,
          sum(volume)                        AS volume,
          CAST(sum(tick_count) AS INTEGER)   AS tick_count,
          ? AS source
        FROM read_parquet(?, hive_partitioning = true)
        WHERE bucket_start BETWEEN ? AND ?
        GROUP BY bkt
        ORDER BY bkt
        """
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            arrow_tbl = con.execute(
                sql, [derived_source(source_timeframe), pattern, start, end]
            ).arrow()
        finally:
            con.close()
        df = pl.from_arrow(arrow_tbl)
        assert isinstance(df, pl.DataFrame)
        if "bkt" in df.columns:
            df = df.rename({"bkt": "bucket_start"})
        return df.with_columns(pl.lit(symbol).alias("symbol"))
