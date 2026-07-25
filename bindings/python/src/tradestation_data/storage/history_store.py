from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.storage.bar_writer import BAR_SCHEMA
from tradestation_data.storage.resampler import Resampler, Timeframe, is_derived

log = logging.getLogger(__name__)


class HistoryStore:
    """
    Unified read/write facade over the tiered storage.

    Layout (see docs/design.md §3.6.3):
      {root}/ticks/symbol=.../date=.../ticks.parquet         — Tier 1 raw
      {root}/bars/timeframe=1m/symbol=.../date=.../bars.parquet  — Tier 2 live
      {root}/bars/timeframe=<tf>/symbol=.../date=.../bars.parquet — Tier 3 lazy

    Public API (§3.6.6):
      - load_ticks(symbol, start, end)                 → Polars DataFrame
      - load_bars(symbol, start, end, timeframe)       → Polars DataFrame
          · cache hit  → read {root}/bars/timeframe=<tf>/...
          · cache miss → resample from Tier 1 → persist → return
      - rebuild_bar_cache(symbol, start, end, timeframe) — force rebuild
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._ticks_root = self._root / "ticks"
        self._bars_root = self._root / "bars"
        self._resampler = Resampler(self._ticks_root, bars_root=self._bars_root)

    # ----- ticks ----------------------------------------------------

    def load_ticks(self, symbol: str, start: datetime, end: datetime) -> pl.DataFrame:
        files = sorted(self._ticks_root.glob(f"symbol={symbol}/date=*/ticks.parquet"))
        if not files:
            return pl.DataFrame()
        pattern = (self._ticks_root / f"symbol={symbol}" / "date=*" / "ticks.parquet").as_posix()
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            arrow_tbl = con.execute(
                "SELECT * FROM read_parquet(?, hive_partitioning = true) "
                "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
                [pattern, start, end],
            ).arrow()
        finally:
            con.close()
        df = pl.from_arrow(arrow_tbl)
        assert isinstance(df, pl.DataFrame)
        return df

    # ----- bars -----------------------------------------------------

    def load_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
    ) -> pl.DataFrame:
        tf = str(timeframe)
        cached = self._load_cached_bars(symbol, start, end, tf)
        if cached is not None:
            return cached
        return self._miss_build_and_return(symbol, start, end, tf)

    def rebuild_bar_cache(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
    ) -> pl.DataFrame:
        tf = str(timeframe)
        self._delete_cache(symbol, tf)
        return self._miss_build_and_return(symbol, start, end, tf)

    # ----- internals ------------------------------------------------

    def _cache_dir(self, symbol: str, timeframe: str) -> Path:
        return self._bars_root / f"timeframe={timeframe}" / f"symbol={symbol}"

    def _load_cached_bars(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pl.DataFrame | None:
        cache_dir = self._cache_dir(symbol, timeframe)
        files = sorted(cache_dir.glob("date=*/bars.parquet"))
        if not files:
            return None
        pattern = (cache_dir / "date=*" / "bars.parquet").as_posix()
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            arrow_tbl = con.execute(
                "SELECT * FROM read_parquet(?, hive_partitioning = true) "
                "WHERE bucket_start BETWEEN ? AND ? ORDER BY bucket_start",
                [pattern, start, end],
            ).arrow()
        finally:
            con.close()
        df = pl.from_arrow(arrow_tbl)
        assert isinstance(df, pl.DataFrame)
        if df.height == 0:
            return None
        return df

    def _miss_build_and_return(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pl.DataFrame:
        df = self._resampler.resample(symbol, start, end, timeframe)
        if df.height == 0 and timeframe != "1m":
            # Tick-level source is unavailable for this symbol — roll up
            # cached 1-minute bars instead. Common for index symbols ($TICK,
            # $ADD, $VOLD …) whose only persisted tier is the live 1m cache.
            df = self._resampler.resample_from_bars(
                symbol, start, end, timeframe, source_timeframe="1m"
            )
            if df.height > 0:
                log.info(
                    "bar_cache_built_from_1m_bars",
                    extra={"symbol": symbol, "timeframe": timeframe},
                )
        if df.height == 0:
            return df
        self._persist_cache(symbol, timeframe, df)
        return df

    def _persist_cache(self, symbol: str, timeframe: str, df: pl.DataFrame) -> None:
        dates = (
            df.select(pl.col("bucket_start").dt.date().alias("day")).unique().to_series().to_list()
        )
        for day in dates:
            day_df = df.filter(pl.col("bucket_start").dt.date() == day)
            out_dir = self._cache_dir(symbol, timeframe) / f"date={day.isoformat()}"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "bars.parquet"

            # Never let a computed bar replace one that came off the wire.
            # pq.write_table overwrites, and a partly-covered range takes the
            # miss path for the whole span, so without this a single missing
            # day would rebuild — and overwrite — every native day beside it.
            #
            # Daily is where the loss would be real rather than cosmetic:
            # TradeStation's daily bar carries the exchange's official OHLC
            # and is split/dividend adjusted. Summing ticks cannot reproduce
            # either, so the replacement would look plausible and be wrong.
            if _partition_holds_native(out_path):
                log.warning(
                    "bar_cache_write_skipped_native_present",
                    extra={
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "date": day.isoformat(),
                        "path": str(out_path),
                    },
                )
                continue

            table = _polars_bars_to_arrow(day_df)
            pq.write_table(table, out_path, compression="zstd")

    def _delete_cache(self, symbol: str, timeframe: str) -> None:
        cache_dir = self._cache_dir(symbol, timeframe)
        if not cache_dir.exists():
            return
        for parquet_file in cache_dir.rglob("bars.parquet"):
            parquet_file.unlink()
        for date_dir in sorted(cache_dir.glob("date=*"), reverse=True):
            if date_dir.is_dir() and not any(date_dir.iterdir()):
                date_dir.rmdir()


def _polars_bars_to_arrow(df: pl.DataFrame) -> pa.Table:
    """Convert a resampled Polars DataFrame to the canonical BAR_SCHEMA.

    BAR_SCHEMA carries both ``bucket_start`` (UTC) and ``bucket_start_et``
    (America/New_York) — older resampler pipelines emit only the UTC
    column, so derive the ET view here to keep the cache layer honest.
    """
    if "bucket_start_et" not in df.columns:
        df = df.with_columns(
            pl.col("bucket_start").dt.convert_time_zone("America/New_York").alias("bucket_start_et")
        )
    table = df.select(
        [
            "bucket_start",
            "bucket_start_et",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "tick_count",
            "source",
        ]
    ).to_arrow()
    return table.cast(BAR_SCHEMA)


def _partition_holds_native(path: Path) -> bool:
    """True if this partition file contains any bar that did not come from us.

    Provenance lives in the ``source`` column: computed bars are stamped
    ``derived:<origin>`` by the resampler, anything else arrived over the
    wire. A file we cannot read is treated as native — refusing to overwrite
    something unreadable is the safer failure.
    """
    if not path.exists():
        return False
    try:
        sources = pq.read_table(path, columns=["source"])["source"].to_pylist()
    except Exception:
        log.warning("bar_cache_partition_unreadable", extra={"path": str(path)})
        return True
    return any(not is_derived(str(s)) for s in sources)
