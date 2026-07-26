from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tradestation_data.aggregation.bar_aggregator import BarAggregator
from tradestation_data.aggregation.snapshot import MarketSnapshot
from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.domain.timeframe import timeframe_to_minutes
from tradestation_data.sinks.pipeline import SinkPipeline
from tradestation_data.wire.base import MarketDataProvider

log = logging.getLogger(__name__)


# Wall-clock grace past bucket_end before we flush a buffered direct bar
# whose next-bucket signal has not arrived (e.g. a quiet index symbol).
# Small enough to feel live, large enough to swallow ordinary publish jitter.
_DIRECT_BAR_CLOSE_GRACE = timedelta(seconds=2)


@dataclass(slots=True)
class _Counters:
    ticks_in: int = 0
    bars_out: int = 0
    bars_direct_in: int = 0  # bars emitted via the direct (EL_PublishTickEx) path
    bars_direct_updated: int = 0  # intra-bar updates to a buffered direct bar
    bars_duplicate_dropped: int = 0  # stale/out-of-order direct bar (bucket_start < buffered)
    ticks_dropped: int = 0
    last_report_monotonic: float = field(default_factory=time.monotonic)
    last_report_ticks: int = 0
    last_report_bars: int = 0


class IngestionRuntime:
    """
    Wires Provider → Snapshot/BarAggregator → SinkPipeline.

    Also runs a background:
      - flush task (drives any buffered sink's flush() when its
        should_flush() returns True)
      - wall-clock advance task (BarAggregator.advance_time every second)
      - heartbeat task (structured log of msg/sec, bar count, etc.)

    Shutdown: `stop()` signals all loops to exit; `run()` awaits clean
    teardown. The class is designed to be driven from an asyncio main,
    where SIGINT/SIGTERM handlers call `stop()`.

    Optional `on_bar` callback lets downstream (Backtester / analytics)
    react to each closed bar without coupling this module to them
    directly. For more general consumption, declare a CallbackSink in
    sinks.yaml instead — that path supports per-symbol filtering and
    multiple subscribers.
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        symbols: list[str],
        snapshot: MarketSnapshot,
        aggregator: BarAggregator,
        sinks: SinkPipeline | None = None,
        *,
        on_bar: Callable[[Bar], None] | None = None,
        heartbeat_interval: float = 60.0,
        flush_poll_interval: float = 1.0,
        advance_interval: float = 1.0,
    ) -> None:
        self._provider = provider
        self._symbols = symbols
        self._snapshot = snapshot
        self._aggregator = aggregator
        self._sinks = sinks if sinks is not None else SinkPipeline()
        self._on_bar = on_bar
        self._heartbeat_interval = heartbeat_interval
        self._flush_poll_interval = flush_poll_interval
        self._advance_interval = advance_interval

        self._stop = asyncio.Event()
        self._counters = _Counters()
        # In-progress direct bar per (symbol, timeframe). EL's "Update every
        # tick" mode resends the same bucket many times per interval with a
        # refined OHLC each time — we replace-last so only the final bar
        # reaches the sinks. A new bucket_start closes the previous buffered
        # bar and emits it. Latest bucket_start already emitted is tracked
        # separately so a TS chart reload that replays historical bars can't
        # re-fire on already-closed buckets.
        #
        # The key includes the timeframe because one DLL, one PUB socket and
        # one topic now carry every interval the user has a chart open on.
        # Keyed on symbol alone, a 1m bar arriving mid-5m-bucket would evict
        # the 5m bar and emit it early, then park _last_emitted at the 1m
        # bucket so the real 5m updates were dropped as duplicates — with the
        # 1m partition looking perfectly healthy the whole time.
        self._current_direct_bars: dict[tuple[str, str], Bar] = {}
        self._last_emitted_direct_bucket: dict[tuple[str, str], datetime] = {}

    # ---- lifecycle --------------------------------------------------

    async def run(self) -> None:
        await self._provider.connect()
        await self._provider.subscribe(self._symbols)
        log.info("ingestion_started", extra={"symbols": self._symbols})

        tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._advance_loop(), name="advance"),
            asyncio.create_task(self._flush_loop(), name="flush"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]
        try:
            try:
                await self._stop.wait()
            finally:
                # Cancel loops first so they stop producing. We used to
                # close the provider first, but on Windows zmq ctx.term()
                # can block long enough for a second Ctrl+C to interrupt
                # the finally, which skipped _shutdown() and left
                # bars.parquet without a footer. Order now: cancel → close
                # provider → await tasks → _shutdown() (sinks), with
                # _shutdown() in an outer finally so it runs even if any
                # step above throws or hangs-then-gets-interrupted.
                for t in tasks:
                    t.cancel()
                try:
                    await self._provider.close()
                except Exception:
                    log.exception("provider_close_failed")
                for t in tasks:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception("task_failed", extra={"task": t.get_name()})
        finally:
            await self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    async def _shutdown(self) -> None:
        # Emit any still-open bars so nothing is lost.
        now = datetime.now(tz=UTC)
        for bar in self._aggregator.advance_time(now):
            await self._on_closed_bar(bar)
        for bar in self._drain_direct_bars():
            await self._on_closed_bar(bar)
        # Sinks close last — closing earlier would mean any final bar
        # the aggregator/direct-buffer drain emits hits a closed parquet
        # writer and gets lost. SinkPipeline.close() swallows per-sink
        # errors so one bad sink doesn't block the rest.
        self._sinks.close()
        log.info(
            "ingestion_stopped",
            extra={
                "ticks_in": self._counters.ticks_in,
                "bars_out": self._counters.bars_out,
                "ticks_dropped": self._counters.ticks_dropped,
            },
        )

    def _drain_direct_bars(self) -> list[Bar]:
        """Return every buffered direct bar and clear the buffer."""
        if not self._current_direct_bars:
            return []
        drained = list(self._current_direct_bars.values())
        self._current_direct_bars.clear()
        for bar in drained:
            self._counters.bars_direct_in += 1
            self._last_emitted_direct_bucket[(bar.symbol, bar.timeframe)] = bar.bucket_start
        return drained

    def _advance_direct_bars(self, now: datetime) -> list[Bar]:
        """Close buffered direct bars whose own interval has fully elapsed.

        Handles the case where a symbol publishes once and then goes
        quiet (no next-bucket signal arrives to trigger emission). Bars
        are released ``_DIRECT_BAR_CLOSE_GRACE`` after bucket_end so
        ordinary publish jitter doesn't prematurely finalize a bar that
        is still receiving intra-bar updates.

        The deadline follows ``bar.timeframe``. Assuming one minute would
        close a 5m bucket at 09:31, publishing OHLC that covers only the
        first of its five minutes and then discarding every later update as
        a duplicate — a `timeframe=5m` partition full of one-minute bars,
        with no error anywhere.
        """
        ready: list[Bar] = []
        for key in list(self._current_direct_bars):
            bar = self._current_direct_bars[key]
            tf_delta = timedelta(minutes=timeframe_to_minutes(bar.timeframe))
            if bar.bucket_start + tf_delta + _DIRECT_BAR_CLOSE_GRACE <= now:
                ready.append(bar)
                del self._current_direct_bars[key]
                self._counters.bars_direct_in += 1
                self._last_emitted_direct_bucket[key] = bar.bucket_start
        return ready

    # ---- loops ------------------------------------------------------

    async def _ingest_loop(self) -> None:
        # `events()` yields Tick for trade prints and Bar for already-formed
        # OHLC bars (EL_PublishTickEx). Bars bypass the aggregator because
        # running a single-price "tick" through it would collapse OHLC to
        # O=H=L=C=close and defeat the whole point of the Ex path.
        async for event in self._provider.events():
            if isinstance(event, Bar):
                await self._handle_provider_bar(event)
            else:
                await self._handle_tick(event)
            if self._stop.is_set():
                return

    async def _advance_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._advance_interval)
            now = datetime.now(tz=UTC)
            for bar in self._aggregator.advance_time(now):
                await self._on_closed_bar(bar)
            for bar in self._advance_direct_bars(now):
                await self._on_closed_bar(bar)

    async def _flush_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._flush_poll_interval)
            if self._sinks.has_pending_flush():
                self._sinks.flush_pending()

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._heartbeat_interval)
            self._emit_heartbeat()

    # ---- tick / bar handling ---------------------------------------

    async def _handle_tick(self, tick: Tick) -> None:
        self._counters.ticks_in += 1
        self._snapshot.on_tick(tick)
        self._sinks.on_tick(tick)
        for bar in self._aggregator.ingest(tick):
            await self._on_closed_bar(bar)

    async def _handle_provider_bar(self, bar: Bar) -> None:
        """Whole-bar path for OHLC shipped by the provider (EL_PublishTickEx).

        Skips the aggregator on purpose — the bar is already final in
        bar-close mode, or will be after a few more intra-bar updates in
        "Update every tick" mode; rebuilding it through ingest() would
        collapse OHLC to the close price either way.

        EL's "Update every tick" mode re-emits the same (symbol,
        bucket_start) many times per minute with a refined OHLC each
        time. We buffer the latest for each symbol and emit it only
        when the next bucket arrives (or on shutdown / wall-clock
        advance). Stale (<= already-emitted) buckets — e.g. a TS chart
        reload replaying history — are dropped so the sinks never
        see the same minute twice.
        """
        key = (bar.symbol, bar.timeframe)
        last_emitted = self._last_emitted_direct_bucket.get(key)
        if last_emitted is not None and bar.bucket_start <= last_emitted:
            self._counters.bars_duplicate_dropped += 1
            return

        current = self._current_direct_bars.get(key)
        if current is None:
            self._current_direct_bars[key] = bar
            return

        if bar.bucket_start == current.bucket_start:
            # Intra-bar refresh — replace so the final emit carries the
            # complete OHLC / volume / tick_count for the minute.
            self._current_direct_bars[key] = bar
            self._counters.bars_direct_updated += 1
            return

        if bar.bucket_start > current.bucket_start:
            self._current_direct_bars[key] = bar
            self._counters.bars_direct_in += 1
            self._last_emitted_direct_bucket[key] = current.bucket_start
            await self._on_closed_bar(current)
            return

        # bar.bucket_start < current.bucket_start — reorder / reload.
        self._counters.bars_duplicate_dropped += 1

    async def _on_closed_bar(self, bar: Bar) -> None:
        self._counters.bars_out += 1
        self._snapshot.on_bar(bar)
        self._sinks.on_bar(bar)
        if self._on_bar is not None:
            try:
                self._on_bar(bar)
            except Exception:
                log.exception("on_bar_callback_failed", extra={"symbol": bar.symbol})

    # ---- observability ---------------------------------------------

    def _emit_heartbeat(self) -> None:
        now = time.monotonic()
        dt = max(now - self._counters.last_report_monotonic, 1e-9)
        ticks_since = self._counters.ticks_in - self._counters.last_report_ticks
        bars_since = self._counters.bars_out - self._counters.last_report_bars
        log.info(
            "heartbeat",
            extra={
                "ticks_in": self._counters.ticks_in,
                "bars_out": self._counters.bars_out,
                "bars_direct_in": self._counters.bars_direct_in,
                "bars_direct_updated": self._counters.bars_direct_updated,
                "bars_duplicate_dropped": self._counters.bars_duplicate_dropped,
                "ticks_per_sec": round(ticks_since / dt, 2),
                "bars_per_sec": round(bars_since / dt, 2),
                "symbols_seen": len(self._snapshot.symbols()),
            },
        )
        self._counters.last_report_monotonic = now
        self._counters.last_report_ticks = self._counters.ticks_in
        self._counters.last_report_bars = self._counters.bars_out
