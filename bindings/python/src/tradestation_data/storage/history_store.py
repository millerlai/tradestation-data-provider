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
    TIMEFRAME_MINUTES,
    Timeframe,
    align_bucket_start,
)
from tradestation_data.storage.bar_coverage import CoverageRecord, Entry, SourceFingerprint
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
    (`TickWriter.write`, `BarWriter.write`) — a 19:00 EST bar belongs to the day
    it traded, not to the UTC day it rolled into. Naive input is read as UTC, to
    agree with the DuckDB reads, which bind it under `SET TimeZone='UTC'`.
    """
    first = _as_utc(start).astimezone(_ET_TZ).date()
    last = _as_utc(end).astimezone(_ET_TZ).date()
    if last < first:
        return []
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _days_in(df: pl.DataFrame) -> set[date]:
    if df.height == 0:
        return set()
    return set(df.select(pl.col("bucket_start_et").dt.date()).to_series().to_list())


def _is_derived_tier(timeframe: str) -> bool:
    """Whether the coverage record governs this timeframe. §2.6 covers Tier 3.

    `1m` is Tier 2 — the live `BarWriter` owns it, and a day missing from it is
    missing *data*, not a cold cache. Running the day builder over it filled
    the gaps with `derived:ticks` bars, which the native guard cannot refuse
    because there is no file there yet to recognise as native. `1d` is
    published outright and never computed at all.
    """
    return timeframe not in NATIVE_ONLY_TIMEFRAMES and timeframe != "1m"


def _day_from_partition(dirname: str) -> date | None:
    try:
        return date.fromisoformat(dirname.removeprefix("date="))
    except ValueError:
        return None


def _et_day_bounds(day: date) -> tuple[datetime, datetime]:
    """One whole ET calendar day as UTC instants — 23 or 25 hours twice a year.

    Built from ET midnights rather than by adding 24 hours, so the DST days come
    out the right length instead of losing or repeating an hour.
    """
    lo = datetime.combine(day, time.min, tzinfo=_ET_TZ)
    hi = datetime.combine(day + timedelta(days=1), time.min, tzinfo=_ET_TZ)
    return lo.astimezone(UTC), hi.astimezone(UTC)


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
        if _is_derived_tier(tf):
            # From the *bucket* containing `start`, not from `start`. The
            # intraday grid is anchored to the session, so a 1h bucket runs
            # HH:30 to HH:30 and the one covering 00:10 ET began at 23:30 the
            # ET day before. Asking only for the days the window names would
            # leave that bucket unbuilt.
            first = align_bucket_start(_as_utc(start), tf)
            self._build_uncovered_days(symbol, tf, _et_days(first, end))
        cached = self._load_cached_bars(symbol, start, end, tf)
        if cached is not None and cached.height > 0:
            return cached
        if tf not in NATIVE_ONLY_TIMEFRAMES:
            built = self._build_window(symbol, start, end, tf)
            if built.height > 0:
                return built
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
            return pl.DataFrame(schema=_EMPTY_BAR_SCHEMA).with_columns(
                pl.lit(symbol).alias("symbol")
            )
        # Every day in range is covered and none of them held rows.
        return _canonical_bars(_empty_bars(symbol))

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
        first = align_bucket_start(_as_utc(start), tf)
        scope = _et_days(first, end)
        # Delete and forget exactly the requested days. Wiping the whole tree
        # and rebuilding only the window destroyed every other cached day, and
        # once the Tier-1 ticks are pruned those are unrecoverable.
        self._delete_cache(symbol, tf, days=scope)
        self._coverage(symbol, tf).forget(scope)
        self._build_uncovered_days(symbol, tf, scope)
        cached = self._load_cached_bars(symbol, start, end, tf)
        if cached is not None and cached.height > 0:
            return cached
        return self._build_window(symbol, start, end, tf)

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
        # `_with_bucket_start_et` before the select, not after: this is a read
        # of whatever is on disk, and `scripts/aggregate_parquet.py` — the other
        # Tier-3 producer named in CLAUDE.md's storage table — writes eight
        # columns with no `bucket_start_et`. Selecting it blind turns every read
        # of such a day into ColumnNotFoundError.
        return _canonical_bars(_restore_et_zone(df))

    def _build_window(self, symbol: str, start: datetime, end: datetime, tf: str) -> pl.DataFrame:
        """Resample exactly the requested window and return it, persisting what
        the native guard allows.

        The last resort, and the behaviour this facade has always had when the
        store held nothing for a window: the coverage pass rebuilds whole days,
        but it deliberately leaves partitions written by other producers alone,
        so a window they do not cover still has to be answered from the source.
        """
        df = self._resampler.resample(symbol, start, end, tf)
        if df.height == 0 and tf != "1m":
            df = self._resampler.resample_from_bars(symbol, start, end, tf, source_timeframe="1m")
        df = _with_bucket_start_et(df)
        if df.height == 0:
            return _canonical_bars(df)
        self._persist_cache(symbol, tf, df)
        return _canonical_bars(df)

    def _coverage(self, symbol: str, timeframe: str) -> CoverageRecord:
        return CoverageRecord(self._cache_dir(symbol, timeframe))

    @staticmethod
    def _is_covered(
        record: CoverageRecord, cache_dir: Path, day: date, fingerprint: SourceFingerprint
    ) -> bool:
        """Whether `day` may be left alone.

        A partition this builder did not write is left alone too. It belongs to
        the live `BarWriter`, to the batch aggregation tool, or to an older
        version — all of which know things this builder does not, and none of
        which can be reproduced by resampling. Rebuilding one overwrote 24 bars
        with 6 and put `derived:ticks` rows into the native Tier-2 tree, so the
        record governs only the days this builder owns and the days that have
        no partition at all. That is still the case the record was added for:
        a range whose middle days were never built.
        """
        partition = cache_dir / f"date={day.isoformat()}" / "bars.parquet"
        if record.knows(day):
            return record.covers(day, fingerprint, partition_exists=partition.exists())
        return partition.exists()

    def _source_index(self, symbol: str, timeframe: str) -> dict[date, list[Path]]:
        """Every source partition this symbol has, keyed by ET day. §2.6 rule 2.

        Tier 1 normally; the live 1m cache as well for every coarser frame,
        because that is the fallback source for index symbols ($TICK, $ADD,
        $VOLD …) which never have ticks. Both are stamped, so a build stops
        matching whichever of them changes.

        Built by globbing rather than probing each requested day, so the cost
        tracks what is on disk instead of how wide the question was: a five-year
        window over a symbol with 500 recorded days costs 500 stats, not 3,650
        `exists()` calls that mostly answer no.
        """
        index: dict[date, list[Path]] = {}
        patterns = [self._ticks_root.glob(f"symbol={symbol}/date=*/ticks.parquet")]
        if timeframe != "1m":
            patterns.append(
                self._bars_root.glob(f"timeframe=1m/symbol={symbol}/date=*/bars.parquet")
            )
        for found in patterns:
            for path in found:
                day = _day_from_partition(path.parent.name)
                if day is not None:
                    index.setdefault(day, []).append(path)
        return index

    def _build_uncovered_days(self, symbol: str, timeframe: str, days: list[date]) -> pl.DataFrame:
        """Build every ET day in `days` this binding has not already built.

        Coverage is the record's answer, never the partition's presence: the
        same path is written by the live `BarWriter`, by the batch aggregation
        tool, and by older versions of this binding, none of which leave a whole
        day behind. §2.6.

        Returns the rows that were built but could **not** be stored, which
        happens when the day's partition holds native bars: §2.3 rule 3 refuses
        the write, and reading the answer back off disk would then lose them.
        """
        record = self._coverage(symbol, timeframe)
        cache_dir = self._cache_dir(symbol, timeframe)
        index = self._source_index(symbol, timeframe)
        if not index:
            # No source for this symbol at all. There is nothing to build and
            # nothing worth recording — writing one anyway created a
            # `symbol=<X>/` directory for a symbol that has no data, which
            # `verify_parquet` then discovers and reports on forever.
            return _empty_bars(symbol)
        wanted = {day: SourceFingerprint.of(index.get(day, [])) for day in days}
        stale: list[date] = [
            day
            for day, fingerprint in wanted.items()
            if not self._is_covered(record, cache_dir, day, fingerprint)
        ]
        if not stale:
            return _empty_bars(symbol)
        # A bucket may straddle ET midnight — the 1h grid runs HH:30 to HH:30 —
        # so rebuilding a day means rebuilding whichever day owns the bucket its
        # first instant falls in, or the straddling bucket keeps the stale side.
        owners = {
            align_bucket_start(_et_day_bounds(day)[0], timeframe).astimezone(_ET_TZ).date()
            for day in stale
        }
        stale = sorted(set(stale) | {d for d in owners if d in wanted})

        # One resample for the whole run rather than one per day: the resampler
        # filters on `timestamp`, not on the `date` hive key, so it cannot prune
        # partitions and a per-day loop would re-read the symbol's entire tick
        # tree once per day.
        #
        # Widened by one interval on each side because the intraday grid is
        # anchored to the session, not to midnight: a 1h bucket runs HH:30 to
        # HH:30, so the last bucket of an ET day extends into the next one.
        # Cutting the window at ET midnight truncated it, and the bucket that
        # covers the first half hour after midnight belongs to the previous day.
        pad = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        start, _ = _et_day_bounds(stale[0])
        _, end = _et_day_bounds(stale[-1])
        df = self._resampler.resample(symbol, start - pad, end + pad, timeframe)
        if df.height > 0:
            df = _with_bucket_start_et(df).filter(pl.col("bucket_start_et").dt.date().is_in(stale))

        # Whether to fall back to the 1m cache is a per-day question, not a
        # per-span one. Tick-level source is unavailable for index symbols
        # ($TICK, $ADD, $VOLD …) — but also for any ordinary day whose ticks
        # were pruned while its 1m bars were kept. Asking "did the whole span
        # produce nothing?" answers no as soon as one other day has ticks, and
        # the tick-less day would then be recorded as empty with its 1m bars
        # sitting right there.
        gaps = [day for day in stale if day not in _days_in(df)]
        if gaps and timeframe != "1m":
            lo, _ = _et_day_bounds(gaps[0])
            _, hi = _et_day_bounds(gaps[-1])
            rolled = self._resampler.resample_from_bars(
                symbol, lo - pad, hi + pad, timeframe, source_timeframe="1m"
            )
            if rolled.height > 0:
                rolled = _with_bucket_start_et(rolled).filter(
                    pl.col("bucket_start_et").dt.date().is_in(gaps)
                )
            if rolled.height > 0:
                df = rolled if df.height == 0 else pl.concat([df, rolled], how="vertical_relaxed")
                log.info(
                    "bar_cache_built_from_1m_bars",
                    extra={"symbol": symbol, "timeframe": timeframe, "days": len(gaps)},
                )
        written = self._persist_cache(symbol, timeframe, df) if df.height > 0 else set()
        # A day whose write the native guard refused is *not* covered: the
        # derived bars were computed and dropped, so recording it would hide
        # them for good behind a record that says the day is done. Only what
        # actually reached disk, plus the days that genuinely produced nothing,
        # go in — and the empty ones go in the record *only*, never as a 0-row
        # partition, which for `1m` would land in the native Tier-2 directory
        # `clear_bar_cache` deliberately never touches. §2.6 rule 3.
        produced = _days_in(df)
        # Refused days are recorded too, as producing nothing *of ours*: the
        # answer for them comes from the native partition already on disk and
        # the rows we built are returned to the caller instead. Leaving them
        # unrecorded made every later call re-resample the whole day.
        recorded = {day: Entry(wanted[day], produced_rows=day in written) for day in stale}
        record.record(recorded)
        log.info(
            "bar_cache_days_built",
            extra={"symbol": symbol, "timeframe": timeframe, "days": len(recorded)},
        )
        refused = produced - written
        if not refused:
            return _empty_bars(symbol)
        return _canonical_bars(df.filter(pl.col("bucket_start_et").dt.date().is_in(list(refused))))

    def _persist_cache(self, symbol: str, timeframe: str, df: pl.DataFrame) -> set[date]:
        """Write each ET day of `df`; return the days that actually landed.

        The native guard below can refuse a day, and the caller has to know:
        recording a refused day as covered would hide its derived bars behind
        a record claiming the day was done."""
        # Partition on the ET calendar date, exactly as BarWriter.write does.
        # Splitting on the UTC date instead would put an evening EST bar in a
        # different date= directory than the writer did, so the native-guard
        # below would be checking the wrong file — and _load_cached_bars
        # globs date=*, so a second copy would come back as a duplicate row.
        df = _with_bucket_start_et(df)
        written: set[date] = set()
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
            if table is None:
                # The file is there but not ours to rewrite. Skipping keeps it
                # whole; the caller still gets the rows from the built frame.
                continue
            pq.write_table(table, out_path, compression="zstd")
            written.add(day)
        return written

    def _delete_cache(self, symbol: str, timeframe: str, days: list[date] | None = None) -> None:
        """Evict computed partitions. Native ones are not cache — they are data.

        The write-side guard in `_persist_cache` is worthless on its own here:
        `rebuild_bar_cache` deletes before it rebuilds, so without this check
        the guard would look at an empty directory and wave the derived bar
        through. §2.3 rule 3 is "derived must not overwrite native", and
        unlink-then-write is an overwrite spelled differently.

        `days` bounds the eviction. Without it `rebuild_bar_cache` deleted every
        cached day for the symbol while rebuilding only the window it was asked
        about, and once the Tier-1 ticks behind the others are pruned that is
        not recoverable.
        """
        cache_dir = self._cache_dir(symbol, timeframe)
        if not cache_dir.exists():
            return
        wanted = None if days is None else {d.isoformat() for d in days}
        for parquet_file in cache_dir.rglob("bars.parquet"):
            if wanted is not None:
                day = _day_from_partition(parquet_file.parent.name)
                if day is None or day.isoformat() not in wanted:
                    continue
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


def _canonical_bars(df: pl.DataFrame) -> pl.DataFrame:
    """One column list and order for every `load_bars` answer.

    The build path returns what the resampler produced; the read path returns
    that plus whatever `hive_partitioning` recovered from the path (`date`,
    `timeframe`). Twelve columns on a hit and ten on a miss meant a caller
    stacking two results with `pl.concat` got a ShapeError or not depending
    purely on which ranges someone had warmed earlier.

    `date` and `timeframe` are dropped rather than added to both sides: they
    restate the path, the build path never had them, and so nothing could have
    been reading them without breaking on its first cold call.
    """
    return _with_bucket_start_et(df).select(_BAR_COLUMNS)


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


def _merge_with_existing_partition(path: Path, fresh: pa.Table) -> pa.Table | None:
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
        # Unreadable, or written to a schema this binding does not know — the
        # batch aggregation tool omits `bucket_start_et`, and the cast fails.
        # Either way we cannot see what we would be discarding, so we must not
        # discard it: overwriting turned a 24-bar day into the 6 bars the
        # current window happened to cover. `None` means "leave the file
        # alone", the same answer `partition_holds_native` gives.
        log.warning("bar_cache_partition_unreadable", extra={"path": str(path)})
        return None
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
