from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar

log = logging.getLogger(__name__)

_ET_TZ = ZoneInfo("America/New_York")


def _today_et() -> date:
    """The current US trading calendar day, which is what `date=` names."""
    return datetime.now(_ET_TZ).date()


BAR_SCHEMA: pa.Schema = pa.schema(
    [
        # bar_time (UTC) and bar_time_et (America/New_York) describe
        # the same instant in different zones; both are persisted so tooling
        # can filter on either without a runtime conversion. ET is the
        # authoritative basis for session-time decisions downstream.
        pa.field("bar_time", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field(
            "bar_time_et",
            pa.timestamp("us", tz="America/New_York"),
            nullable=False,
        ),
        # Prices are the only floating-point values here.
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        # EasyLanguage's reserved words, verbatim and integral. The el_
        # prefix is part of the contract, not decoration: on an intraday bar
        # `el_volume` is up-tick share volume alone and `el_ticks` is the
        # total, and a column called plain `volume` invites exactly the
        # misreading that cost this repo a systematically halved volume
        # column. See contract/semantics.md §3.4.
        pa.field("el_volume", pa.int64(), nullable=False),
        pa.field("el_ticks", pa.int64(), nullable=False),
        pa.field("el_upticks", pa.int64(), nullable=False),
        pa.field("el_downticks", pa.int64(), nullable=False),
        pa.field("el_open_interest", pa.int64(), nullable=False),
        # EL's Category, and the quote pair. category is what makes the
        # el_* words readable at all (semantics.md 3.4/3.5); bid/ask arrive
        # on every point and are stored on every point. bar_type and
        # bar_interval are not columns because they ARE the partition path.
        pa.field("category", pa.int64(), nullable=False),
        pa.field("bid", pa.float64(), nullable=True),
        pa.field("ask", pa.float64(), nullable=True),
        # The DLL's receive clock, verbatim. On a tick chart ts_str has
        # minute resolution, so this is the only sub-minute ordering the
        # stored rows have. Nullable only for rows synthesised off-wire.
        pa.field("ts", pa.float64(), nullable=True),
    ]
)


@dataclass(slots=True)
class _Partition:
    bar_type: int
    bar_interval: int
    symbol: str
    # None for a SINGLE_FILE_TIMEFRAMES series: one file per symbol, with no
    # date= level and therefore no day boundary to seal on.
    day: date | None
    buffer: list[Bar] = field(default_factory=list)
    writer: pq.ParquetWriter | None = None
    # Its day has rolled over: the file is closed and will take no more
    # bars. Kept in the table rather than dropped so a late bar is refused
    # instead of reopening — pq.ParquetWriter truncates on open.
    sealed: bool = False
    # This partition cannot be written and never will be during this run —
    # most often a file on disk under a superseded schema. Poisoning it
    # confines the damage to one series: without this, one such file takes
    # down every partition ordered after it, forever.
    poisoned: bool = False
    # When the last bar arrived, for the elapsed-day seal. A replay of five
    # days delivers all of them within seconds, so "this day is over by the
    # wall clock" alone would seal a partition the burst is still filling
    # and the rest of its bars would be refused.
    last_write_monotonic: float | None = None

    @property
    def rewrites(self) -> bool:
        return self.day is None

    def path(self, root: Path) -> Path:
        base = (
            root
            / f"bartype={self.bar_type}"
            / f"interval={self.bar_interval}"
            / f"symbol={self.symbol}"
        )
        if self.day is None:
            return base / "bars.parquet"
        return base / f"date={self.day.isoformat()}" / "bars.parquet"


class BarWriter:
    """
    Tier 2 bar cache writer.

    Layout: `{root}/bartype={N}/interval={M}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet`,
    except for `SINGLE_FILE_TIMEFRAMES` (`1d`), which drop the `date=` level
    and keep one `{root}/bartype=2/interval={M}/symbol={SYM}/bars.parquet` per symbol.
    A day partition of daily bars holds exactly one row, and a closed Parquet
    file costs ~2.9 KB of schema and footer regardless — 2,903 bytes to carry
    about 60. Those files are **rewritten whole on every flush**: the rows
    already on disk are read back, merged with the new ones (later wins on a
    repeated `bar_time`, which is what a chart reload sends), sorted, and
    written to a temporary file that replaces the old one atomically. That is
    affordable because the file is small — twenty years of one symbol is
    ~5,000 rows — and it means the file is complete and readable after every
    flush rather than only after `close()`.

    **The partition follows `bar.timeframe`.** Routing on the bar is what
    keeps a 5-minute bar out of the 1-minute partition once the wire can
    say which interval it is; before that, mislabelled bars were
    indistinguishable from real 1-minute data downstream.

    Buffered, with two flush triggers:

      - `max_buffered_bars`  : total buffered bars across all partitions
      - `max_flush_seconds`  : time since the oldest buffered bar arrived

    Bars used to be written one at a time, which cost **one Parquet row
    group per bar** — every row carrying a full set of per-column headers
    and statistics. Measured on a real session: 78 five-minute bars
    occupied 145,977 bytes as 78 row groups and 5,936 as one. Chart
    reloads make it worse, because a whole session arrives as a burst.
    The flush interval bounds crash-loss to the same one minute of bar
    cache the unbuffered version promised (and Tier-1 ticks can rebuild
    intraday bars anyway).

    A day partition is **sealed** — buffer flushed, file closed — on either
    of two signals, and it needs both:

      1. a bar for a **later day** of the same (timeframe, symbol) arrives;
      2. the day is **over in ET** and nothing has arrived for it in a whole
         `max_flush_seconds`.

    Without sealing, a `ParquetWriter` stays open until `close()` and its
    file has no footer — unreadable to every reader, however long the
    process runs. A daily chart replaying two years used to leave 499 such
    files behind, all of them 943 bytes of unreadable prefix.

    (1) alone leaves the **newest** day of a finished replay open forever,
    because no later day is ever coming: a chart loaded with five days
    published all five, sealed four, and the fifth only became readable on
    Ctrl+C. (2) alone would seal a day mid-burst — a replay delivers five
    already-past days within seconds — and `write` refuses a sealed
    partition, so a readability problem would become lost bars. The quiet
    period is what separates "this day is over" from "this day has stopped
    arriving".

    Today's partition is never sealed by (2): more bars are coming, and
    `pq.ParquetWriter` truncates on open, so it cannot be closed and
    resumed. Rewritten partitions never need sealing at all — every flush
    leaves a complete file.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        max_buffered_bars: int = 1_000,
        max_flush_seconds: float = 60.0,
        compression: str = "zstd",
        today_et: Callable[[], date] = _today_et,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_bars = max_buffered_bars
        self._max_seconds = max_flush_seconds
        self._compression = compression
        # Injected so the elapsed-day seal can be tested without waiting for
        # midnight. Nothing else reads a clock here.
        self._today_et = today_et
        self._partitions: dict[tuple[int, int, str, date | None], _Partition] = {}
        self._buffered_bars = 0
        self._oldest_buffer_monotonic: float | None = None
        self._closed = False

    def write(self, bar: Bar) -> None:
        if self._closed:
            raise RuntimeError("BarWriter is closed")
        # Partition on EasyLanguage's own BarType/BarInterval, not on a name
        # this binding derived from them. The DLL used to map the pair to
        # "5m"/"1d"/... and refuse anything it could not name, so a 2-minute
        # chart published nothing at all. The raw pair files everything and
        # names nothing.
        #
        # The ET calendar date keeps one US trading session in one date=
        # directory, regardless of the UTC rollover (which would split a
        # session at 19:00/20:00 ET). BarType 2 is the daily chart: one row
        # per day, so a date= level would be one row per file.
        partition_day = None if bar.bar_type == 2 else bar.bar_time_et.date()
        key = (bar.bar_type, bar.bar_interval, bar.symbol, partition_day)
        part = self._partitions.get(key)
        if part is None:
            if partition_day is not None:
                self._seal_earlier_days(bar.bar_type, bar.bar_interval, bar.symbol, partition_day)
            part = _Partition(
                bar_type=bar.bar_type,
                bar_interval=bar.bar_interval,
                symbol=bar.symbol,
                day=partition_day,
            )
            self._partitions[key] = part
        elif part.sealed:
            # Reopening would truncate a finished day. Losing one late bar
            # beats losing the session it belongs to.
            log.warning(
                "bar_partition_sealed",
                extra={
                    "symbol": bar.symbol,
                    "bar_type": bar.bar_type,
                    "bar_interval": bar.bar_interval,
                    "date": part.day.isoformat() if part.day else "",
                },
            )
            return
        elif part.poisoned:
            # Already reported once by _flush_partition with the reason and
            # the path. Buffering more would only grow memory for a write
            # that cannot happen.
            return
        part.buffer.append(bar)
        part.last_write_monotonic = time.monotonic()
        self._buffered_bars += 1
        if self._oldest_buffer_monotonic is None:
            self._oldest_buffer_monotonic = time.monotonic()

    def should_flush(self) -> bool:
        # A day that is over needs sealing even with nothing buffered — its
        # bars may all have been written already, and what is missing is the
        # footer, which only close() writes.
        if self._has_elapsed_open_day():
            return True
        if self._buffered_bars == 0:
            return False
        if self._buffered_bars >= self._max_bars:
            return True
        if self._oldest_buffer_monotonic is None:
            return False
        return (time.monotonic() - self._oldest_buffer_monotonic) >= self._max_seconds

    def flush(self) -> int:
        total = self._seal_elapsed_days()
        for part in self._partitions.values():
            total += self._flush_partition(part)
        return total

    def _flush_partition(self, part: _Partition) -> int:
        if not part.buffer or part.poisoned:
            return 0
        path = part.path(self._root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if part.rewrites:
                self._rewrite(path, part.buffer)
            else:
                if part.writer is None:
                    part.writer = pq.ParquetWriter(path, BAR_SCHEMA, compression=self._compression)
                part.writer.write_table(_bars_to_table(part.buffer))
        except Exception as exc:
            # One partition must not be able to starve the others. Without
            # this, a single unwritable file — a store left over from a
            # superseded schema is the realistic case — aborts the loop in
            # flush() for every partition after it, and because the buffer
            # is only cleared on success it is retried every cycle forever
            # while memory grows. The process reports healthy throughout,
            # because nothing here raises where anyone is watching.
            #
            # So: report once, loudly, naming the file; give up on this
            # series for the rest of the run; drop its buffer so memory
            # stays bounded. Those bars are lost, which is bad — but they
            # were already lost, and this way the other series survive.
            part.poisoned = True
            n = len(part.buffer)
            part.buffer.clear()
            self._buffered_bars -= n
            if self._buffered_bars == 0:
                self._oldest_buffer_monotonic = None
            log.error(
                "bar_partition_unwritable",
                extra={
                    "symbol": part.symbol,
                    "bar_type": part.bar_type,
                    "bar_interval": part.bar_interval,
                    "date": part.day.isoformat() if part.day else "",
                    "path": str(path),
                    "bars_dropped": n,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return 0
        n = len(part.buffer)
        part.buffer.clear()
        self._buffered_bars -= n
        if self._buffered_bars == 0:
            self._oldest_buffer_monotonic = None
        return n

    def _rewrite(self, path: Path, bars: list[Bar]) -> None:
        """Merge `bars` into the whole file at `path` and replace it.

        Reading the existing rows back is what makes a restart safe: this
        file is the only copy of a native daily bar, and pq.write_table
        truncates. A repeated `bar_time` keeps the later row — that is
        a chart reload re-sending days we already have, and the fresher
        copy is the one TradeStation just adjusted.
        """
        incoming = pl.from_arrow(_bars_to_table(bars))
        assert isinstance(incoming, pl.DataFrame)
        frames = [incoming]
        if path.exists():
            # ParquetFile, not read_table: this path sits under
            # bartype=/interval=/symbol=, and read_table runs hive discovery on those
            # and hands back two extra dictionary columns, which
            # pl.concat(how="vertical") rejects as a width mismatch.
            existing = pl.from_arrow(pq.ParquetFile(path).read())
            assert isinstance(existing, pl.DataFrame)
            # Check the shape before concatenating, so the failure names the
            # cause. `publisher_version` took `union_by_name` and the
            # `with_publisher_version` pad with it when the schema stopped
            # evolving, and nothing replaced them — so a store written by a
            # superseded release (columns `volume`, `tick_count`, `source`)
            # reaches here and pl.concat raises something that reads like a
            # polars bug rather than "your data root is from an old
            # version". Old and new bars cannot be merged: the intraday
            # `volume` there is up-tick volume, not the total, so there is
            # no correct column mapping to attempt.
            missing = [f.name for f in BAR_SCHEMA if f.name not in existing.columns]
            if missing:
                raise ValueError(
                    f"{path} was written under a different schema and cannot be "
                    f"merged: missing {missing}. This is a store from a release "
                    f"before the el_* quantity columns. Point --data-root (or "
                    f"the per-sink `root` in sinks.yaml) at a fresh directory "
                    f"and keep the old one for reading with an older release; "
                    f"the two conventions cannot be mixed in one file."
                )
            frames = [existing, incoming]
        merged = (
            pl.concat(frames, how="vertical")
            .unique(subset=["bar_time"], keep="last", maintain_order=True)
            .sort("bar_time")
        )
        # Write beside the target and rename over it: a crash mid-write
        # would otherwise leave a truncated file where the only copy was.
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(merged.to_arrow().cast(BAR_SCHEMA), tmp, compression=self._compression)
        os.replace(tmp, path)

    def _seal_earlier_days(self, bar_type: int, bar_interval: int, symbol: str, day: date) -> None:
        """Finish every earlier day of the same (timeframe, symbol).

        A partition whose day has rolled over will never take another bar,
        so holding its writer open only keeps its file footerless. Sealing
        here is what makes a finished session readable while the process
        keeps running.
        """
        for part in self._partitions.values():
            if part.sealed or part.day is None:
                continue
            if (
                part.bar_type != bar_type
                or part.bar_interval != bar_interval
                or part.symbol != symbol
                or part.day >= day
            ):
                continue
            self._seal(part)

    def _has_elapsed_open_day(self) -> bool:
        today = self._today_et()
        return any(self._is_finished(part, today) for part in self._partitions.values())

    def _is_finished(self, part: _Partition, today: date) -> bool:
        """Whether this day is over AND has stopped arriving.

        Both halves are load-bearing. The wall clock alone is not enough: a
        chart replaying five days publishes all of them within seconds, so
        sealing on "the date is in the past" would close a partition the
        burst is still filling, and `write` refuses a sealed partition —
        turning a readability problem into lost bars.

        Quiet for one whole flush interval is the same bound the buffer
        already runs on, and a replay burst finishes orders of magnitude
        inside it.
        """
        if part.sealed or part.day is None or part.day >= today:
            return False
        if part.last_write_monotonic is None:
            return False
        return (time.monotonic() - part.last_write_monotonic) >= self._max_seconds

    def _seal_elapsed_days(self) -> int:
        """Finish every `date=` partition whose ET day is already over.

        `_seal_earlier_days` only fires when a bar for a LATER day arrives,
        so the newest day of a finished replay never seals. A chart loaded
        with five days of history publishes all five, seals four, and leaves
        the fifth holding an open `pq.ParquetWriter` — whose file has no
        footer, and therefore reads as "Parquet magic bytes not found in
        footer" to every reader, for as long as the process runs. Ctrl+C was
        the only thing that finished it, which is how it was found.

        The flush loop already runs; this rides on it. A day strictly before
        today in ET will take no more bars, so closing it costs nothing.

        Today's partition is deliberately left open: more bars are coming,
        and `pq.ParquetWriter` truncates on open, so it cannot be closed now
        and resumed later.
        """
        today = self._today_et()
        written = 0
        for part in self._partitions.values():
            if self._is_finished(part, today):
                written += self._seal(part)
        return written

    def _seal(self, part: _Partition) -> int:
        """Flush a partition, close its file, and refuse it any more bars."""
        written = self._flush_partition(part)
        if part.writer is not None:
            part.writer.close()
            part.writer = None
        part.sealed = True
        return written

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        finally:
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
            "bar_time": [b.bar_time for b in bars],
            "bar_time_et": [b.bar_time_et for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "el_volume": [b.el_volume for b in bars],
            "el_ticks": [b.el_ticks for b in bars],
            "el_upticks": [b.el_upticks for b in bars],
            "el_downticks": [b.el_downticks for b in bars],
            "el_open_interest": [b.el_open_interest for b in bars],
            "category": [b.category for b in bars],
            "bid": [b.bid for b in bars],
            "ask": [b.ask for b in bars],
            "ts": [b.ts for b in bars],
        },
        schema=BAR_SCHEMA,
    )
