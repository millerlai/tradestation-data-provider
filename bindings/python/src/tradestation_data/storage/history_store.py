from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.timeframe import (
    SINGLE_FILE_TIMEFRAMES,
    SUPPORTED_TIMEFRAMES,
    Timeframe,
)
from tradestation_data.storage.bar_writer import BAR_SCHEMA
from tradestation_data.storage.tick_writer import TICK_SCHEMA

log = logging.getLogger(__name__)

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
    API. An aware input already says which instant it means, so only its zone
    is normalised — polars compares a timestamp column against a literal of
    the same time unit AND zone or it refuses the filter outright, so handing
    an ET-aware bound straight through raises instead of answering.
    See contract/semantics.md §2.3.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_ET_TZ)
        # `replace(tzinfo=...)` pins fold=0, so a naive bound inside the
        # repeated hour on the fall-back date silently means its FIRST
        # occurrence, and one inside the skipped hour on the spring-forward
        # date means an instant that never happened. A query bound is the
        # caller's to disambiguate — they can pass an aware datetime and say
        # exactly which they meant — so this reports rather than guesses
        # differently. One day a year, and the numbers look ordinary.
        if value.utcoffset() != value.replace(fold=1).utcoffset():
            log.warning(
                "query_bound_dst_ambiguous",
                extra={
                    "bound": value.isoformat(),
                    "note": "naive local time maps to two instants (or none); "
                    "took fold=0 — pass an aware datetime to be explicit",
                },
            )
    return value.astimezone(UTC)


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
            raise ValueError(
                f"Unsupported timeframe: {tf!r}. Valid: {sorted(SUPPORTED_TIMEFRAMES)}"
            )

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
        lo, hi = _as_utc(start), _as_utc(end)

        # Select the files by their `date=` directory before opening any of
        # them. Filtering on `time_column` alone cannot do this: it is a
        # column *inside* every file, so polars had to open all of them —
        # including the current day's, which the writer still holds open and
        # which therefore has no footer yet. One open partition made every
        # read of that symbol raise, including reads of days that were sealed
        # and complete, which is precisely the promise bar_writer's docstring
        # makes. Selecting on the path first means a query for yesterday
        # never touches today's file.
        paths = _files_in_range(pattern, lo, hi)

        # scan_parquet raises rather than returning nothing when nothing
        # matches, so this is what turns "never recorded" into the same
        # zero-row answer as "recorded, but quiet". Both are ordinary
        # questions and neither is an error — §2.4.
        if not paths:
            return _empty(schema, hive)

        try:
            return _collect(paths, time_column, lo, hi, schema, hive)
        except _ForeignStoreError:
            # Not a partition problem — the caller is pointed at something
            # that is not this store. Let it out.
            raise
        except Exception:
            # A partition inside the requested range is still being written,
            # so it has no footer. Answer with the readable ones rather than
            # nothing: an operator asking for a range that reaches into the
            # live session should get the sealed days, not an exception about
            # magic bytes.
            readable = [p for p in paths if _is_readable(p)]
            skipped = [p for p in paths if p not in readable]
            if not readable:
                log.warning(
                    "history_no_readable_partition",
                    extra={"pattern": str(pattern), "skipped": len(skipped)},
                )
                return _empty(schema, hive)
            log.warning(
                "history_partition_unreadable_skipped",
                extra={"skipped": [str(p) for p in skipped]},
            )
            return _collect(readable, time_column, lo, hi, schema, hive)


class _ForeignStoreError(ValueError):
    """The path holds Parquet, but not this store's Parquet."""


def _collect(
    paths: list[Path],
    time_column: str,
    lo: datetime,
    hi: datetime,
    schema: pa.Schema,
    hive: dict[str, _Dtype],
) -> pl.DataFrame:
    """Read the given partitions, refusing anything that is not this store.

    The column check is the guard CHANGELOG.md and imputation_parquet.py both
    claimed already existed. It did not: `_read` scanned and filtered whatever
    the path held, and `BAR_SCHEMA` was used only to shape the *empty* frame.
    Two consequences, both silent.

    Point this at an imputation output root and every invented bar — flat
    O=H=L=C with all five quantities zero — comes back as an ordinary row,
    indistinguishable from a published one to any consumer selecting OHLC.
    That is the whole reason imputed output was given a schema of its own.

    And the two answers disagreed on width: a populated day returned the
    file's columns (plus `imputed`), while a quiet day returned BAR_SCHEMA
    without it, so stacking a symbol loop with `pl.concat` raised on the first
    quiet day — the exact pattern `test_empty_and_populated_answers_share_one_schema`
    exists to protect.
    """
    lf = pl.scan_parquet([p.as_posix() for p in paths], hive_partitioning=True)

    want = [f.name for f in schema] + list(hive)
    got = lf.collect_schema().names()
    extra = [c for c in got if c not in want]
    missing = [c for c in want if c not in got]
    if extra or missing:
        detail = "imputed" in extra and (
            " The `imputed` column means this is an imputation output root, "
            "which holds invented bars and must not be read as collected data."
        )
        raise _ForeignStoreError(
            f"{paths[0].parent} does not hold this store's schema: "
            f"unexpected {extra}, missing {missing}."
            f"{detail or ''}"
        )

    return (
        lf.select(want).filter(pl.col(time_column).is_between(lo, hi)).sort(time_column).collect()
    )


def _files_in_range(pattern: Path, lo: datetime, hi: datetime) -> list[Path]:
    """The partition files that can hold rows in [lo, hi], by path alone.

    Both layouts have at most one wildcard and it is always the `date=`
    directory, so this needs no general glob expansion.

    The `date=` key is the ET calendar date the writer partitioned on, while
    the bounds are UTC instants. A day is kept when its ET date falls within
    the ET dates of the bounds, widened by one day on each side: a UTC
    instant can sit on either side of the ET date boundary depending on the
    offset, and a partition wrongly kept costs one file open, while one
    wrongly dropped costs data. The row-level filter is still applied
    afterwards, so the widening cannot let an out-of-range row through.
    """
    if "*" not in pattern.as_posix():
        return [pattern] if pattern.is_file() else []

    symbol_dir = pattern.parent.parent
    if not symbol_dir.is_dir():
        return []

    lo_day = (lo.astimezone(_ET_TZ) - timedelta(days=1)).date()
    hi_day = (hi.astimezone(_ET_TZ) + timedelta(days=1)).date()

    kept: list[Path] = []
    for path in sorted(symbol_dir.glob(f"{pattern.parent.name}/{pattern.name}")):
        try:
            day = date.fromisoformat(path.parent.name.removeprefix("date="))
        except ValueError:
            # Not a date= directory we wrote. Keep it and let the row filter
            # decide rather than silently dropping something unexpected.
            kept.append(path)
            continue
        if lo_day <= day <= hi_day:
            kept.append(path)
    return kept


def _is_readable(path: Path) -> bool:
    """Whether this Parquet file has a footer yet.

    The writer holds a `pq.ParquetWriter` open on the current day, and a
    Parquet file has no footer until it is closed, so reading one raises.
    """
    try:
        # Opening is the check: ParquetFile reads the footer eagerly, and a
        # file the writer still holds open does not have one yet.
        return pq.ParquetFile(path).metadata is not None
    except Exception:
        return False
