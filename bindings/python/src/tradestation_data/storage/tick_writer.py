from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.tick import Tick

log = logging.getLogger(__name__)

TICK_SCHEMA: pa.Schema = pa.schema(
    [
        # timestamp (UTC) and timestamp_et (America/New_York) describe the
        # same instant in different zones; both are persisted so tooling
        # can filter on either without a runtime conversion. ET is the
        # authoritative basis for session-time decisions downstream.
        pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field(
            "timestamp_et",
            pa.timestamp("us", tz="America/New_York"),
            nullable=False,
        ),
        pa.field("price", pa.float64(), nullable=False),
        # EasyLanguage's reserved words, verbatim and integral — see
        # BAR_SCHEMA for why they keep the el_ prefix.
        pa.field("el_volume", pa.int64(), nullable=False),
        pa.field("el_ticks", pa.int64(), nullable=False),
        pa.field("el_upticks", pa.int64(), nullable=False),
        pa.field("el_downticks", pa.int64(), nullable=False),
        pa.field("el_open_interest", pa.int64(), nullable=False),
        # Null when there is no quote: historical replay, or a symbol that
        # never carries one. contract/semantics.md §3.
        pa.field("bid", pa.float64(), nullable=True),
        pa.field("ask", pa.float64(), nullable=True),
    ]
)


@dataclass(slots=True)
class _DayPartition:
    symbol: str
    day: date
    buffer: list[Tick] = field(default_factory=list)
    writer: pq.ParquetWriter | None = None
    # Its day has rolled over: the file is closed and will take no more
    # ticks. Kept in the table rather than dropped so a late tick is
    # refused instead of reopening — pq.ParquetWriter truncates on open.
    sealed: bool = False

    def path(self, root: Path) -> Path:
        return root / f"symbol={self.symbol}" / f"date={self.day.isoformat()}" / "ticks.parquet"


class TickWriter:
    """
    Tier 1 raw tick writer. Hive-partitioned Parquet.

    Layout:  `{root}/symbol={SYM}/date={YYYY-MM-DD}/ticks.parquet`

    See docs/design.md §3.6.3, §3.6.5. Buffered with two flush triggers
    (either one fires a flush):

      - `max_buffered_ticks`  : total buffered ticks across all partitions
      - `max_flush_seconds`   : time since the oldest buffered tick arrived

    Design caveat (§3.6.7): one `ParquetWriter` is held open per
    (symbol, day) so multiple flushes land in a single file. The file's
    footer is only written on `close()` — crashing loses the entire
    in-progress file for that partition. This is accepted: the live
    system treats tick loss on crash as tolerable.

    That exposure is bounded to the *current* day: a partition is sealed
    (flushed and closed) as soon as a tick for a later day of the same
    symbol arrives, so finished sessions are readable while the process
    keeps running instead of only after it stops.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        max_buffered_ticks: int = 10_000,
        max_flush_seconds: float = 30.0,
        compression: str = "zstd",
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_ticks = max_buffered_ticks
        self._max_seconds = max_flush_seconds
        self._compression = compression
        self._partitions: dict[tuple[str, date], _DayPartition] = {}
        self._buffered_ticks = 0
        self._oldest_buffer_monotonic: float | None = None
        self._closed = False

    def write(self, tick: Tick) -> None:
        if self._closed:
            raise RuntimeError("TickWriter is closed")
        # Partition on the ET calendar date so all ticks from one US
        # trading session land in a single date= directory, regardless of
        # the UTC rollover mid-session.
        partition_day = tick.timestamp_et.date()
        key = (tick.symbol, partition_day)
        part = self._partitions.get(key)
        if part is None:
            self._seal_earlier_days(tick.symbol, partition_day)
            part = _DayPartition(symbol=tick.symbol, day=partition_day)
            self._partitions[key] = part
        elif part.sealed:
            # Reopening would truncate a finished day. Losing one late tick
            # beats losing the session it belongs to.
            log.warning(
                "tick_partition_sealed",
                extra={"symbol": tick.symbol, "date": partition_day.isoformat()},
            )
            return
        part.buffer.append(tick)
        self._buffered_ticks += 1
        if self._oldest_buffer_monotonic is None:
            self._oldest_buffer_monotonic = time.monotonic()

    def should_flush(self) -> bool:
        if self._buffered_ticks == 0:
            return False
        if self._buffered_ticks >= self._max_ticks:
            return True
        if self._oldest_buffer_monotonic is None:
            return False
        return (time.monotonic() - self._oldest_buffer_monotonic) >= self._max_seconds

    def flush(self) -> int:
        total = 0
        for part in self._partitions.values():
            total += self._flush_partition(part)
        return total

    def _flush_partition(self, part: _DayPartition) -> int:
        # The running totals are maintained here rather than reset in
        # flush(), because sealing flushes one partition on its own.
        if not part.buffer:
            return 0
        path = part.path(self._root)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = _ticks_to_table(part.buffer)
        if part.writer is None:
            part.writer = pq.ParquetWriter(path, TICK_SCHEMA, compression=self._compression)
        part.writer.write_table(table)
        n = len(part.buffer)
        part.buffer.clear()
        self._buffered_ticks -= n
        if self._buffered_ticks == 0:
            self._oldest_buffer_monotonic = None
        return n

    def _seal_earlier_days(self, symbol: str, day: date) -> None:
        """Finish every earlier day of the same symbol.

        A partition whose day has rolled over will never take another
        tick, so holding its writer open only keeps its file footerless —
        unreadable to every reader until close(), however long the process
        runs.
        """
        for key, part in self._partitions.items():
            if part.sealed or key[0] != symbol or key[1] >= day:
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

    def __enter__(self) -> TickWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _ticks_to_table(ticks: list[Tick]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "timestamp": [t.timestamp for t in ticks],
            "timestamp_et": [t.timestamp_et for t in ticks],
            "price": [t.price for t in ticks],
            "el_volume": [t.el_volume for t in ticks],
            "el_ticks": [t.el_ticks for t in ticks],
            "el_upticks": [t.el_upticks for t in ticks],
            "el_downticks": [t.el_downticks for t in ticks],
            "el_open_interest": [t.el_open_interest for t in ticks],
            "bid": [t.bid for t in ticks],
            "ask": [t.ask for t in ticks],
        },
        schema=TICK_SCHEMA,
    )
