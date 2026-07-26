from __future__ import annotations

import logging
from datetime import datetime
from glob import glob
from pathlib import Path

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.bar import is_derived
from tradestation_data.domain.timeframe import (
    NATIVE_ONLY_TIMEFRAMES,
    SINGLE_FILE_TIMEFRAMES,
    Timeframe,
)
from tradestation_data.storage.bar_writer import BAR_SCHEMA
from tradestation_data.storage.resampler import Resampler

log = logging.getLogger(__name__)

# The zone BAR_SCHEMA / TICK_SCHEMA declare for their `*_et` columns. Read
# paths have to restore it explicitly; see _restore_et_zone.
_ET_ZONE = "America/New_York"


class HistoryStore:
    """
    Unified read/write facade over the tiered storage.

    Layout (see docs/design.md §3.6.3):
      {root}/ticks/symbol=.../date=.../ticks.parquet         — Tier 1 raw
      {root}/bars/timeframe=1m/symbol=.../date=.../bars.parquet  — Tier 2 live
      {root}/bars/timeframe=<tf>/symbol=.../date=.../bars.parquet — Tier 3 lazy
      {root}/bars/timeframe=1d/symbol=.../bars.parquet        — published, one file

    Public API (§3.6.6):
      - load_ticks(symbol, start, end)                 → Polars DataFrame
      - load_bars(symbol, start, end, timeframe)       → Polars DataFrame
          · cache hit  → read {root}/bars/timeframe=<tf>/...
          · cache miss → resample from Tier 1 → persist → return
          · NATIVE_ONLY_TIMEFRAMES → whatever is on disk, or empty. Never
            computed: a derived daily is a plausible-looking wrong answer.
      - rebuild_bar_cache(symbol, start, end, timeframe) — force rebuild;
        refuses NATIVE_ONLY_TIMEFRAMES, which are data rather than cache.
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
        return _restore_et_zone(df)

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
        if tf in NATIVE_ONLY_TIMEFRAMES:
            # Not a cache miss — there is nothing to miss. A `1d` bar is only
            # ever what TradeStation published, so an empty answer is the
            # truthful one. Building it from minutes would return a frame
            # that looks right, carries neither the exchange's official close
            # nor the split/dividend adjustment, and is indistinguishable
            # from the real thing once persisted. §2.3.
            log.info(
                "native_only_timeframe_not_cached",
                extra={"symbol": symbol, "timeframe": tf},
            )
            return pl.DataFrame()
        return self._miss_build_and_return(symbol, start, end, tf)

    def load_cached_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
    ) -> pl.DataFrame | None:
        """Read what is on disk, and only that. None when nothing is cached.

        ``load_bars`` self-heals on a miss: it resamples and writes the result
        back. That makes it the wrong side of a comparison for anything
        auditing the cache, which would otherwise be diffing a freshly built
        frame against itself. This is the read-only view for those callers.
        """
        return self._load_cached_bars(symbol, start, end, str(timeframe))

    def rebuild_bar_cache(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str | Timeframe,
    ) -> pl.DataFrame:
        tf = str(timeframe)
        if tf in NATIVE_ONLY_TIMEFRAMES:
            # Rebuilding means deleting and recomputing, and neither half is
            # legal here: the file is the only copy, and the recomputed
            # replacement would be wrong. Raising beats a no-op — a caller
            # asking for this has the wrong model of the tier.
            raise ValueError(
                f"{tf!r} is published, not derived: it cannot be rebuilt. "
                "Re-export it from TradeStation instead."
            )
        self._delete_cache(symbol, tf)
        return self._miss_build_and_return(symbol, start, end, tf)

    # ----- internals ------------------------------------------------

    def _cache_dir(self, symbol: str, timeframe: str) -> Path:
        return self._bars_root / f"timeframe={timeframe}" / f"symbol={symbol}"

    def _bar_glob(self, symbol: str, timeframe: str) -> str:
        """Where this timeframe's files live, as one read_parquet pattern.

        `SINGLE_FILE_TIMEFRAMES` have no `date=` level — see
        :class:`~tradestation_data.storage.bar_writer.BarWriter`. Globbing
        `date=*` there matches nothing, which reads as "no data" rather than
        as an error, so this has to follow the same rule the writer does.
        """
        cache_dir = self._cache_dir(symbol, timeframe)
        if timeframe in SINGLE_FILE_TIMEFRAMES:
            return (cache_dir / "bars.parquet").as_posix()
        return (cache_dir / "date=*" / "bars.parquet").as_posix()

    def _load_cached_bars(
        self, symbol: str, start: datetime, end: datetime, timeframe: str
    ) -> pl.DataFrame | None:
        pattern = self._bar_glob(symbol, timeframe)
        if not glob(pattern):
            return None
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
        return _restore_et_zone(df)

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
        # Add the ET view before returning, not just before writing. A cache
        # hit reads BAR_SCHEMA back off disk and always carries
        # ``bucket_start_et``; without this the very first call — the one that
        # builds the cache — would hand back a frame one column short, and the
        # same code would work or KeyError depending on whether someone had
        # asked for that range before.
        df = _with_bucket_start_et(df)
        self._persist_cache(symbol, timeframe, df)
        return df

    def _persist_cache(self, symbol: str, timeframe: str, df: pl.DataFrame) -> None:
        # Partition on the ET calendar date, exactly as BarWriter.write does.
        # Splitting on the UTC date instead would put an evening EST bar in a
        # different date= directory than the writer did, so the native-guard
        # below would be checking the wrong file — and _load_cached_bars
        # globs date=*, so a second copy would come back as a duplicate row.
        df = _with_bucket_start_et(df)
        dates = (
            df.select(pl.col("bucket_start_et").dt.date().alias("day"))
            .unique()
            .to_series()
            .to_list()
        )
        for day in dates:
            day_df = df.filter(pl.col("bucket_start_et").dt.date() == day)
            out_dir = self._cache_dir(symbol, timeframe) / f"date={day.isoformat()}"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "bars.parquet"

            # Never let a computed bar replace one that came off the wire.
            # pq.write_table overwrites, and a partly-covered range takes the
            # miss path for the whole span, so without this a single missing
            # day would rebuild — and overwrite — every native day beside it.
            # Any charted interval can be native, not just 1m: a 5-minute
            # chart publishes native 5m bars into this same directory.
            #
            # Daily never reaches here at all — NATIVE_ONLY_TIMEFRAMES is
            # refused before the miss path — but the reason is the sharpest
            # statement of why this guard exists at all:
            # TradeStation's daily bar carries the exchange's official OHLC
            # and is split/dividend adjusted. Summing ticks cannot reproduce
            # either, so the replacement would look plausible and be wrong.
            if partition_holds_native(out_path):
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
        """Evict computed partitions. Native ones are not cache — they are data.

        The write-side guard in `_persist_cache` is worthless on its own here:
        `rebuild_bar_cache` deletes before it rebuilds, so without this check
        the guard would look at an empty directory and wave the derived bar
        through. §2.3 rule 3 is "derived must not overwrite native", and
        unlink-then-write is an overwrite spelled differently.
        """
        cache_dir = self._cache_dir(symbol, timeframe)
        if not cache_dir.exists():
            return
        for parquet_file in cache_dir.rglob("bars.parquet"):
            if partition_holds_native(parquet_file):
                log.warning(
                    "bar_cache_delete_skipped_native_present",
                    extra={"path": str(parquet_file)},
                )
                continue
            parquet_file.unlink()
        for date_dir in sorted(cache_dir.glob("date=*"), reverse=True):
            if date_dir.is_dir() and not any(date_dir.iterdir()):
                date_dir.rmdir()


def _restore_et_zone(df: pl.DataFrame) -> pl.DataFrame:
    """Re-label every ``*_et`` column as America/New_York after a DuckDB read.

    ``SET TimeZone='UTC'`` is deliberate — without it DuckDB renders
    TIMESTAMPTZ in whatever zone the session inherits, so the same query
    would describe its own output differently on two machines. But it
    applies to *every* column, including the ones persisted as ET, and
    what comes back is labelled UTC.

    The instant survives (TIMESTAMPTZ is absolute; only the rendering
    zone changes), so this conversion is exact. The label does not
    survive, and it is the whole point of storing an ET column:
    ``bucket_start_et.dt.hour()`` returned 13 instead of 9 for an 09:30
    ET bar. Worse, it disagreed with itself — the cache-miss path builds
    the column with ``convert_time_zone`` and got the label right, so a
    caller saw 9 or 13 depending on whether anyone had warmed that range
    before.
    """
    et_cols = [
        name
        for name, dtype in df.schema.items()
        if name.endswith("_et") and isinstance(dtype, pl.Datetime) and dtype.time_zone
    ]
    if not et_cols:
        return df
    return df.with_columns(
        [pl.col(c).dt.convert_time_zone(_ET_ZONE) for c in et_cols],
    )


def _with_bucket_start_et(df: pl.DataFrame) -> pl.DataFrame:
    """Add ``bucket_start_et`` if the frame only carries the UTC column.

    BAR_SCHEMA persists both views so downstream tooling never converts at
    query time; the resampler emits only the UTC one.
    """
    if "bucket_start_et" in df.columns:
        return df
    return df.with_columns(
        pl.col("bucket_start").dt.convert_time_zone(_ET_ZONE).alias("bucket_start_et")
    )


def _polars_bars_to_arrow(df: pl.DataFrame) -> pa.Table:
    """Convert a resampled Polars DataFrame to the canonical BAR_SCHEMA."""
    df = _with_bucket_start_et(df)
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


def partition_holds_native(path: Path) -> bool:
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
