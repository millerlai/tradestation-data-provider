from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from glob import glob
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Every read path materialises with `.pl()`, never `.arrow()`. DuckDB 1.5
# changed `.arrow()` to return a RecordBatchReader, and a reader over an empty
# result carries neither a batch nor a schema, so `pl.from_arrow` raised
# "Must pass schema, or at least one RecordBatch". The file-existence guards
# only rule out a missing partition — a window a symbol did not trade in is an
# ordinary question, and it was aborting the caller instead of answering 0 rows.
#
# The two schemas below are what the *no-partition* branches return, so that
# "this symbol was never recorded" and "this symbol was quiet" answer with the
# same columns and dtypes. They are pinned against the real non-empty output by
# test_empty_and_populated_answers_share_one_schema.
_EMPTY_TICK_SCHEMA: dict[str, type[pl.DataType] | pl.DataType] = {
    "timestamp": pl.Datetime("us", "UTC"),
    "timestamp_et": pl.Datetime("us", "America/New_York"),
    "price": pl.Float64,
    "volume": pl.Int64,
    "bid": pl.Float64,
    "ask": pl.Float64,
    "tick_count": pl.Int32,
    "source": pl.Utf8,
    "date": pl.Date,
    "symbol": pl.Utf8,
}

_EMPTY_BAR_SCHEMA: dict[str, type[pl.DataType] | pl.DataType] = {
    "bucket_start": pl.Datetime("us", "UTC"),
    "bucket_start_et": pl.Datetime("us", "America/New_York"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "tick_count": pl.Int32,
    "source": pl.Utf8,
    "symbol": pl.Utf8,
}

# The one shape `load_bars` answers in, whichever path produced it.
_BAR_COLUMNS = list(_EMPTY_BAR_SCHEMA)

_ET_TZ = ZoneInfo(_ET_ZONE)


def _empty_bars(symbol: str) -> pl.DataFrame:
    return pl.DataFrame(schema=_EMPTY_BAR_SCHEMA).with_columns(pl.lit(symbol).alias("symbol"))


def _et_days(start: datetime, end: datetime) -> list[date]:
    """Every ET calendar date the window touches.

    ET rather than UTC because that is what both writers partition on
    (`TickWriter.write`, `BarWriter.write`) — a 19:00 EST bar belongs to the
    day it traded, not to the UTC day it rolled into.
    """
    first = start.astimezone(_ET_TZ).date()
    last = end.astimezone(_ET_TZ).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _et_day_bounds(day: date) -> tuple[datetime, datetime]:
    """One whole ET calendar day, as UTC instants — 23 or 25 hours twice a year.

    Built from ET midnights rather than by adding 24 hours, so the DST days
    come out the right length instead of losing or repeating an hour.
    """
    lo = datetime.combine(day, time.min, tzinfo=_ET_TZ)
    hi = datetime.combine(day + timedelta(days=1), time.min, tzinfo=_ET_TZ)
    return lo.astimezone(UTC), hi.astimezone(UTC)


def _day_from_partition(dirname: str) -> date | None:
    try:
        return date.fromisoformat(dirname.removeprefix("date="))
    except ValueError:
        return None


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
            return pl.DataFrame(schema=_EMPTY_TICK_SCHEMA).with_columns(
                pl.lit(symbol).alias("symbol")
            )
        pattern = (self._ticks_root / f"symbol={symbol}" / "date=*" / "ticks.parquet").as_posix()
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            df = con.execute(
                "SELECT * FROM read_parquet(?, hive_partitioning = true) "
                "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
                [pattern, start, end],
            ).pl()
        finally:
            con.close()
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
        if tf in NATIVE_ONLY_TIMEFRAMES:
            # Not a cache miss — there is nothing to miss. A `1d` bar is only
            # ever what TradeStation published, so an empty answer is the
            # truthful one. Building it from minutes would return a frame
            # that looks right, carries neither the exchange's official close
            # nor the split/dividend adjustment, and is indistinguishable
            # from the real thing once persisted. §2.3.
            cached = self._load_cached_bars(symbol, start, end, tf)
            if cached is None:
                log.info(
                    "native_only_timeframe_not_cached",
                    extra={"symbol": symbol, "timeframe": tf},
                )
                return _empty_bars(symbol)
            return cached
        self._build_missing_days(symbol, tf, _et_days(start, end))
        cached = self._load_cached_bars(symbol, start, end, tf)
        return _empty_bars(symbol) if cached is None else cached

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
        self._build_missing_days(symbol, tf, _et_days(start, end))
        cached = self._load_cached_bars(symbol, start, end, tf)
        return _empty_bars(symbol) if cached is None else cached

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
            df = con.execute(
                "SELECT * FROM read_parquet(?, hive_partitioning = true) "
                "WHERE bucket_start BETWEEN ? AND ? ORDER BY bucket_start",
                [pattern, start, end],
            ).pl()
        finally:
            con.close()
        # Select the canonical columns, in order. `hive_partitioning` bolts the
        # path keys (`date`, `timeframe`) onto the read, which the build path
        # never had: a hit answered 12 columns and a miss 10, so whether a
        # caller could stack two results depended only on which ranges someone
        # had warmed before.
        return _restore_et_zone(df).select(_BAR_COLUMNS)

    def _ingested_days(self, symbol: str, timeframe: str) -> list[date]:
        """ET days for which the *source* tier holds a partition.

        Tier 1 for everything that has ticks; the live 1m cache for the index
        symbols that never get any (`$TICK`, `$ADD`, `$VOLD` …), which is the
        same fallback `_build_missing_days` uses.
        """
        days = {
            _day_from_partition(p.parent.name)
            for p in self._ticks_root.glob(f"symbol={symbol}/date=*/ticks.parquet")
        }
        if timeframe != "1m":
            days |= {
                _day_from_partition(p.parent.name)
                for p in self._bars_root.glob(f"timeframe=1m/symbol={symbol}/date=*/bars.parquet")
            }
        return sorted(d for d in days if d is not None)

    def _build_missing_days(self, symbol: str, timeframe: str, days: list[date]) -> None:
        """Build whole ET days, so "the file exists" means "the day is done".

        Building only the requested window is what made a partition's presence
        say nothing about its completeness — the reason a partly-warm range
        used to come back short.

        One resample covers every missing day rather than one per day. The
        resampler filters on `timestamp`, not on the `date` hive key, so it
        cannot prune partitions: a per-day loop re-reads the symbol's whole
        tick tree once per day, which would make warming a year 365 full
        scans.
        """
        cache_dir = self._cache_dir(symbol, timeframe)
        missing = [
            d for d in days if not (cache_dir / f"date={d.isoformat()}" / "bars.parquet").exists()
        ]
        if not missing:
            return
        start, _ = _et_day_bounds(missing[0])
        _, end = _et_day_bounds(missing[-1])
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
        built: set[date] = set()
        if df.height > 0:
            # Only the missing days: the others are already on disk, and a
            # rewrite would put them through the native guard again for nothing.
            df = _with_bucket_start_et(df).filter(
                pl.col("bucket_start_et").dt.date().is_in(missing)
            )
            built = set(df.select(pl.col("bucket_start_et").dt.date()).to_series().to_list())
            if df.height > 0:
                self._persist_cache(symbol, timeframe, df)

        # Days the build produced nothing for. Recording one as an empty
        # partition turns every future query for it into a hit instead of
        # another full scan of the tick tree — but only when the emptiness is a
        # fact rather than a gap. Ticks on both sides mean ingestion was running
        # across this day, so "no ticks" is "no session". Past the last ingested
        # day it means "not ingested yet", and a file claiming otherwise would
        # stop the bars from ever being built once the ticks arrive.
        ingested = self._ingested_days(symbol, timeframe)
        if not ingested:
            return
        for day in missing:
            if day in built or not (ingested[0] < day < ingested[-1]):
                continue
            out_path = cache_dir / f"date={day.isoformat()}" / "bars.parquet"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(_polars_bars_to_arrow(_empty_bars(symbol)), out_path, compression="zstd")
            log.info(
                "bar_cache_empty_day_recorded",
                extra={"symbol": symbol, "timeframe": timeframe, "date": day.isoformat()},
            )

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

            day_df = df.filter(pl.col("bucket_start_et").dt.date() == day)
            table = _merge_with_existing_partition(out_path, _polars_bars_to_arrow(day_df))
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


def _merge_with_existing_partition(path: Path, fresh: pa.Table) -> pa.Table:
    """Union a rebuilt window with the derived rows already in the partition.

    A partition is a whole ET day, but a build only covers the window that was
    asked for, and ``pq.write_table`` overwrites. Rebuilding the RTH window of
    a day whose pre-market bars were cached by an earlier call would otherwise
    drop them: the row count on disk falls with nothing raised, and the bars
    are unrecoverable once the Tier-1 ticks are pruned.

    Rebuilt buckets win — they are the fresher computation of the same input.
    Buckets outside the rebuilt window survive. Only derived partitions reach
    here; ``_persist_cache`` skips native ones before calling this.
    """
    if not path.exists():
        return fresh
    try:
        # ParquetFile, not read_table: given a path under `timeframe=/symbol=/
        # date=`, read_table runs hive discovery and hands back three extra
        # dictionary columns, which then fail the cast and silently drop us
        # into the overwrite this function exists to prevent.
        existing = pq.ParquetFile(path).read().cast(BAR_SCHEMA)
    except Exception:
        # Unreadable means we cannot know what we would be discarding, so we
        # cannot claim to be preserving it either. Writing the rebuilt window
        # over a file nothing can read is still the better of two bad options.
        log.warning("bar_cache_partition_unreadable", extra={"path": str(path)})
        return fresh
    fresh_df = pl.from_arrow(fresh)
    existing_df = pl.from_arrow(existing)
    assert isinstance(fresh_df, pl.DataFrame)
    assert isinstance(existing_df, pl.DataFrame)
    keep = existing_df.filter(~pl.col("bucket_start").is_in(fresh_df["bucket_start"]))
    if keep.height == 0:
        return fresh
    log.info(
        "bar_cache_partition_merged",
        extra={"path": str(path), "kept_rows": keep.height, "new_rows": fresh.num_rows},
    )
    return pl.concat([keep, fresh_df]).sort("bucket_start").to_arrow().cast(BAR_SCHEMA)


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
