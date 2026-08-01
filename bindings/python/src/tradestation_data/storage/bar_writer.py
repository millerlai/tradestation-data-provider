from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.timeframe import SINGLE_FILE_TIMEFRAMES

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
    ]
)


@dataclass(slots=True)
class _Partition:
    timeframe: str
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

    @property
    def rewrites(self) -> bool:
        return self.day is None

    def path(self, root: Path) -> Path:
        base = root / f"timeframe={self.timeframe}" / f"symbol={self.symbol}"
        if self.day is None:
            return base / "bars.parquet"
        return base / f"date={self.day.isoformat()}" / "bars.parquet"


class BarWriter:
    """
    Tier 2 bar cache writer.

    Layout: `{root}/timeframe={tf}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet`,
    except for `SINGLE_FILE_TIMEFRAMES` (`1d`), which drop the `date=` level
    and keep one `{root}/timeframe=1d/symbol={SYM}/bars.parquet` per symbol.
    A day partition of daily bars holds exactly one row, and a closed Parquet
    file costs ~2.9 KB of schema and footer regardless — 2,903 bytes to carry
    about 60. Those files are **rewritten whole on every flush**: the rows
    already on disk are read back, merged with the new ones (later wins on a
    repeated `bucket_start`, which is what a chart reload sends), sorted, and
    written to a temporary file that replaces the old one atomically. That is
    affordable because the file is small — twenty years of one symbol is
    ~5,000 rows — and it means the file is complete and readable after every
    flush rather than only after `close()`.

    **The partition follows `bar.timeframe`.** Routing on the bar is what
    keeps a 5-minute bar out of the 1-minute partition once the wire can
    say which interval it is; before that, mislabelled bars were
    indistinguishable from real 1-minute data downstream.

    Buffered, with the same two flush triggers as `TickWriter`:

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

    A day partition is **sealed** when a bar for a later day of the same
    (timeframe, symbol) arrives: its buffer is flushed and its file
    closed. Without that, a `ParquetWriter` stays open until `close()` and
    its file has no footer — unreadable to every reader, however long the
    process runs. A daily chart replaying two years used to leave 499 such
    files behind, all of them 943 bytes of unreadable prefix. Rewritten
    partitions never need sealing: every flush leaves a complete file.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        max_buffered_bars: int = 1_000,
        max_flush_seconds: float = 60.0,
        compression: str = "zstd",
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_bars = max_buffered_bars
        self._max_seconds = max_flush_seconds
        self._compression = compression
        self._partitions: dict[tuple[str, str, date | None], _Partition] = {}
        self._buffered_bars = 0
        self._oldest_buffer_monotonic: float | None = None
        self._closed = False

    def write(self, bar: Bar) -> None:
        if self._closed:
            raise RuntimeError("BarWriter is closed")
        timeframe = bar.timeframe
        # Partition on the ET calendar date so all bars from one US trading
        # session land in a single date= directory, regardless of the UTC
        # rollover (which splits a session at 19:00/20:00 ET otherwise).
        # Coarse timeframes have no date= level at all.
        partition_day = None if timeframe in SINGLE_FILE_TIMEFRAMES else bar.bucket_start_et.date()
        key = (timeframe, bar.symbol, partition_day)
        part = self._partitions.get(key)
        if part is None:
            if partition_day is not None:
                self._seal_earlier_days(timeframe, bar.symbol, partition_day)
            part = _Partition(timeframe=timeframe, symbol=bar.symbol, day=partition_day)
            self._partitions[key] = part
        elif part.sealed:
            # Reopening would truncate a finished day. Losing one late bar
            # beats losing the session it belongs to.
            log.warning(
                "bar_partition_sealed",
                extra={
                    "symbol": bar.symbol,
                    "timeframe": timeframe,
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
        self._buffered_bars += 1
        if self._oldest_buffer_monotonic is None:
            self._oldest_buffer_monotonic = time.monotonic()

    def should_flush(self) -> bool:
        if self._buffered_bars == 0:
            return False
        if self._buffered_bars >= self._max_bars:
            return True
        if self._oldest_buffer_monotonic is None:
            return False
        return (time.monotonic() - self._oldest_buffer_monotonic) >= self._max_seconds

    def flush(self) -> int:
        total = 0
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
                    "timeframe": part.timeframe,
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
        truncates. A repeated `bucket_start` keeps the later row — that is
        a chart reload re-sending days we already have, and the fresher
        copy is the one TradeStation just adjusted.
        """
        incoming = pl.from_arrow(_bars_to_table(bars))
        assert isinstance(incoming, pl.DataFrame)
        frames = [incoming]
        if path.exists():
            # ParquetFile, not read_table: this path sits under
            # timeframe=/symbol=, and read_table runs hive discovery on those
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
            .unique(subset=["bucket_start"], keep="last", maintain_order=True)
            .sort("bucket_start")
        )
        # Write beside the target and rename over it: a crash mid-write
        # would otherwise leave a truncated file where the only copy was.
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(merged.to_arrow().cast(BAR_SCHEMA), tmp, compression=self._compression)
        os.replace(tmp, path)

    def _seal_earlier_days(self, timeframe: str, symbol: str, day: date) -> None:
        """Finish every earlier day of the same (timeframe, symbol).

        A partition whose day has rolled over will never take another bar,
        so holding its writer open only keeps its file footerless. Sealing
        here is what makes a finished session readable while the process
        keeps running.
        """
        for part in self._partitions.values():
            if part.sealed or part.day is None:
                continue
            if part.timeframe != timeframe or part.symbol != symbol or part.day >= day:
                continue
            self._flush_partition(part)
            if part.writer is not None:
                part.writer.close()
                part.writer = None
            part.sealed = True

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
            "bucket_start": [b.bucket_start for b in bars],
            "bucket_start_et": [b.bucket_start_et for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "el_volume": [b.el_volume for b in bars],
            "el_ticks": [b.el_ticks for b in bars],
            "el_upticks": [b.el_upticks for b in bars],
            "el_downticks": [b.el_downticks for b in bars],
            "el_open_interest": [b.el_open_interest for b in bars],
        },
        schema=BAR_SCHEMA,
    )
