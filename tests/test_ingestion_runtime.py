from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import zmq
import zmq.asyncio

from tradestation_data.aggregation import BarAggregator, MarketSnapshot
from tradestation_data.providers.tradestation_el import TradeStationELProvider
from tradestation_data.runtime import IngestionRuntime
from tradestation_data.sinks import SinkPipeline
from tradestation_data.sinks.parquet import ParquetBarSink, ParquetTickSink


async def _publish(pub: zmq.asyncio.Socket, topic: str, payload: dict) -> None:
    await pub.send_multipart([topic.encode(), json.dumps(payload).encode()])


@pytest.mark.asyncio
async def test_runtime_pipes_ticks_to_snapshot_and_storage(
    zmq_inproc_bus,
    tmp_path: Path,
) -> None:
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    snap = MarketSnapshot()
    agg = BarAggregator()
    pipeline = SinkPipeline(
        [
            ParquetTickSink(
                name="ticks_parquet",
                root=tmp_path / "ticks",
                max_buffered_ticks=1,
                max_flush_seconds=3600,
            ),
            ParquetBarSink(name="bars_parquet", root=tmp_path / "bars"),
        ]
    )

    observed_bars: list = []
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=snap,
        aggregator=agg,
        sinks=pipeline,
        on_bar=lambda b: observed_bars.append(b),
        heartbeat_interval=3600,
        flush_poll_interval=0.02,
        advance_interval=0.02,
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)  # let subscription settle

    bucket1 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC).timestamp()
    bucket2 = datetime(2026, 4, 18, 13, 31, 0, tzinfo=UTC).timestamp()
    await _publish(pub, "SPY", {"v": 1, "ts": bucket1 + 1, "px": 450.0, "vol": 100, "tc": 1})
    await _publish(pub, "SPY", {"v": 1, "ts": bucket1 + 30, "px": 450.5, "vol": 100, "tc": 1})
    # Crossing into next bucket closes the first bar
    await _publish(pub, "SPY", {"v": 1, "ts": bucket2 + 1, "px": 451.0, "vol": 100, "tc": 1})

    # Wait for the bar to emerge
    for _ in range(200):
        if observed_bars:
            break
        await asyncio.sleep(0.01)

    assert len(observed_bars) >= 1
    bar = observed_bars[0]
    assert bar.symbol == "SPY"
    assert bar.volume == 200
    assert bar.close == pytest.approx(450.5)

    # Snapshot sees it
    state = snap.state_of("SPY")
    assert state is not None
    assert state.last_tick is not None
    assert state.last_closed_bar is not None

    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # Tick parquet written
    tick_file = tmp_path / "ticks" / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert tick_file.exists()
    assert pq.read_table(tick_file).num_rows == 3

    # Bar parquet written
    bar_file = (
        tmp_path / "bars" / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    )
    assert bar_file.exists()
    assert pq.read_table(bar_file).num_rows >= 1


@pytest.mark.asyncio
async def test_runtime_direct_bar_bypasses_aggregator(
    zmq_inproc_bus,
    tmp_path: Path,
) -> None:
    """Bar events from EL_PublishTickEx land on the snapshot + bar_writer
    directly without being re-ingested through the aggregator. We prove
    it by checking OHLC is preserved (the aggregator would collapse
    O=H=L=C to the close price if it were involved)."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    snap = MarketSnapshot()
    agg = BarAggregator()
    pipeline = SinkPipeline([ParquetBarSink(name="bars_parquet", root=tmp_path / "bars")])

    observed: list = []
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=snap,
        aggregator=agg,
        sinks=pipeline,
        on_bar=lambda b: observed.append(b),
        heartbeat_interval=3600,
        flush_poll_interval=0.02,
        advance_interval=3600,  # keep wall-clock advance quiet
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)

    ts_el = datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el + 0.5,
            "ts_el": ts_el,
            "o": 450.10,
            "h": 450.75,
            "l": 449.80,
            "c": 450.40,
            "vol": 12000,
            "bid": 450.39,
            "ask": 450.41,
            "tc": 140,
        },
    )

    # The bar is buffered (replace-last semantics); drain via stop().
    await asyncio.sleep(0.05)
    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(observed) == 1
    bar = observed[0]
    # OHLC preserved — proves aggregator was bypassed.
    assert bar.open == pytest.approx(450.10)
    assert bar.high == pytest.approx(450.75)
    assert bar.low == pytest.approx(449.80)
    assert bar.close == pytest.approx(450.40)
    assert bar.volume == 12000
    assert bar.bucket_start == datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)

    # Snapshot accepted the bar.
    state = snap.state_of("SPY")
    assert state is not None
    assert state.last_closed_bar is not None
    assert state.last_closed_bar.high == pytest.approx(450.75)

    # Counter advanced on the direct-bar path, not the aggregator path.
    assert runtime._counters.bars_direct_in == 1
    assert runtime._counters.ticks_in == 0

    bar_file = (
        tmp_path / "bars" / "timeframe=1m" / "symbol=SPY" / "date=2026-04-20" / "bars.parquet"
    )
    assert bar_file.exists()
    assert pq.read_table(bar_file).num_rows == 1


@pytest.mark.asyncio
async def test_runtime_replaces_intra_bar_updates_and_drops_stale_bars(
    zmq_inproc_bus,
    tmp_path: Path,
) -> None:
    """EL's 'Update every tick' mode re-emits the same (symbol, bucket_start)
    many times per minute with a refined OHLC — the runtime must replace
    the buffered bar so only the final OHLC reaches disk. When a newer
    bucket arrives, the previous bucket emits. A later replay of an
    already-emitted bucket (e.g. TS chart reload replaying history) must
    be dropped so the strategy never re-fires on a minute it has already
    seen."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    snap = MarketSnapshot()
    agg = BarAggregator()
    pipeline = SinkPipeline([ParquetBarSink(name="bars_parquet", root=tmp_path / "bars")])

    observed: list = []
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=snap,
        aggregator=agg,
        sinks=pipeline,
        on_bar=lambda b: observed.append(b),
        heartbeat_interval=3600,
        flush_poll_interval=0.02,
        advance_interval=3600,
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)

    ts_el_1 = datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC).timestamp()
    # Three intra-bar refreshes for bucket 13:30 — close/high/volume grow.
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el_1 + 5,
            "ts_el": ts_el_1,
            "o": 450.10,
            "h": 450.20,
            "l": 450.05,
            "c": 450.15,
            "vol": 3000,
            "bid": 450.14,
            "ask": 450.16,
            "tc": 40,
        },
    )
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el_1 + 30,
            "ts_el": ts_el_1,
            "o": 450.10,
            "h": 450.50,
            "l": 450.05,
            "c": 450.45,
            "vol": 8000,
            "bid": 450.44,
            "ask": 450.46,
            "tc": 90,
        },
    )
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el_1 + 55,
            "ts_el": ts_el_1,
            "o": 450.10,
            "h": 450.75,
            "l": 449.80,
            "c": 450.40,
            "vol": 12000,
            "bid": 450.39,
            "ask": 450.41,
            "tc": 140,
        },
    )
    # New bucket 13:31 closes 13:30 and buffers itself.
    ts_el_2 = datetime(2026, 4, 20, 13, 31, 0, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el_2 + 1,
            "ts_el": ts_el_2,
            "o": 450.40,
            "h": 450.60,
            "l": 450.30,
            "c": 450.55,
            "vol": 5000,
            "bid": 450.54,
            "ask": 450.56,
            "tc": 70,
        },
    )

    for _ in range(200):
        if len(observed) >= 1 and runtime._counters.bars_direct_updated >= 2:
            break
        await asyncio.sleep(0.01)

    # First bucket emitted carries the last refresh's OHLC (replace-last).
    assert len(observed) == 1, f"intra-bar updates not collapsed: observed={len(observed)}"
    first = observed[0]
    assert first.bucket_start == datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)
    assert first.high == pytest.approx(450.75)
    assert first.close == pytest.approx(450.40)
    assert first.volume == 12000
    assert runtime._counters.bars_direct_in == 1
    assert runtime._counters.bars_direct_updated == 2

    # Now replay bucket 13:30 — it is stale (<= last_emitted) and must be dropped.
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el_1 + 55,
            "ts_el": ts_el_1,
            "o": 450.10,
            "h": 450.75,
            "l": 449.80,
            "c": 450.40,
            "vol": 12000,
            "bid": 450.39,
            "ask": 450.41,
            "tc": 140,
        },
    )

    for _ in range(200):
        if runtime._counters.bars_duplicate_dropped >= 1:
            break
        await asyncio.sleep(0.01)
    assert runtime._counters.bars_duplicate_dropped == 1

    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # stop() drains the still-buffered 13:31 bar via _drain_direct_bars().
    assert len(observed) == 2
    second = observed[1]
    assert second.bucket_start == datetime(2026, 4, 20, 13, 31, 0, tzinfo=UTC)
    assert second.close == pytest.approx(450.55)
    assert runtime._counters.bars_direct_in == 2

    bar_file = (
        tmp_path / "bars" / "timeframe=1m" / "symbol=SPY" / "date=2026-04-20" / "bars.parquet"
    )
    assert bar_file.exists()
    assert pq.read_table(bar_file).num_rows == 2


@pytest.mark.asyncio
async def test_runtime_stops_cleanly_without_ticks(zmq_inproc_bus, tmp_path: Path) -> None:
    ctx, _pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        heartbeat_interval=3600,
        flush_poll_interval=0.05,
        advance_interval=0.05,
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0.1)
    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)


# ---- unit tests on private handlers (no ZMQ) -------------------------------


class _StubProvider:
    async def connect(self) -> None:
        pass

    async def subscribe(self, symbols) -> None:
        pass

    async def close(self) -> None:
        pass

    async def events(self):
        if False:
            yield  # pragma: no cover


def _make_runtime(**kwargs) -> IngestionRuntime:
    return IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
        **kwargs,
    )


def _bar(symbol: str, ts: datetime, close: float = 450.0) -> Bar:  # noqa: F821
    from tradestation_data.domain.bar import Bar

    return Bar(
        symbol=symbol,
        bucket_start=ts,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        volume=100,
        vwap=close,
        tick_count=5,
        source="test",
    )


@pytest.mark.asyncio
async def test_on_bar_callback_exception_is_logged_and_swallowed(caplog) -> None:
    import logging

    def _boom(bar):
        raise RuntimeError("cb failed")

    runtime = _make_runtime(on_bar=_boom)
    bar = _bar("SPY", datetime(2026, 4, 20, 13, 30, tzinfo=UTC))
    with caplog.at_level(logging.ERROR):
        await runtime._on_closed_bar(bar)
    assert any("on_bar_callback_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_bar_sink_failure_logged(caplog) -> None:
    """A sink that raises in on_bar is caught at the pipeline level and logged."""
    import logging

    from tradestation_data.sinks.base import BaseSink

    class _BadBarSink(BaseSink):
        name = "bad_bar"

        def on_bar(self, bar):
            raise RuntimeError("disk full")

    runtime = _make_runtime(sinks=SinkPipeline([_BadBarSink()]))
    bar = _bar("SPY", datetime(2026, 4, 20, 13, 30, tzinfo=UTC))
    with caplog.at_level(logging.ERROR):
        await runtime._on_closed_bar(bar)
    assert any("sink_on_bar_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_tick_sink_failure_logged(caplog) -> None:
    """A sink that raises in on_tick is caught at the pipeline level and logged."""
    import logging

    from tradestation_data.domain.tick import Tick
    from tradestation_data.sinks.base import BaseSink

    class _BadTickSink(BaseSink):
        name = "bad_tick"

        def on_tick(self, tick):
            raise RuntimeError("write fail")

    runtime = _make_runtime(sinks=SinkPipeline([_BadTickSink()]))
    tick = Tick(
        symbol="SPY",
        timestamp=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
        price=450.0,
        volume=100,
        bid=None,
        ask=None,
        tick_count=1,
        source="test",
    )
    with caplog.at_level(logging.ERROR):
        await runtime._handle_tick(tick)
    assert any("sink_on_tick_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handle_provider_bar_drops_reordered_stale_bar() -> None:
    """Covers line 300-301: bar.bucket_start < current.bucket_start."""
    runtime = _make_runtime()
    ts_newer = datetime(2026, 4, 20, 13, 31, tzinfo=UTC)
    ts_older = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    # Buffer the newer bar first.
    await runtime._handle_provider_bar(_bar("SPY", ts_newer))
    # Then an older one arrives — it's out of order, must be dropped.
    await runtime._handle_provider_bar(_bar("SPY", ts_older))
    assert runtime._counters.bars_duplicate_dropped == 1


@pytest.mark.asyncio
async def test_advance_direct_bars_flushes_after_grace(tmp_path: Path) -> None:
    """Covers line 198-205: grace-window flush path."""
    runtime = _make_runtime()
    ts = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    await runtime._handle_provider_bar(_bar("SPY", ts))
    # Ask for bars ready at a wall-clock time past bucket_end + grace
    ready = runtime._advance_direct_bars(ts + timedelta(minutes=1, seconds=5))
    assert len(ready) == 1
    assert runtime._counters.bars_direct_in == 1
    # Buffer now empty
    assert runtime._advance_direct_bars(ts + timedelta(hours=1)) == []


def test_emit_heartbeat_updates_counters() -> None:
    """Covers lines 405-424."""
    runtime = _make_runtime()
    runtime._counters.ticks_in = 10
    runtime._counters.bars_out = 2
    runtime._emit_heartbeat()
    assert runtime._counters.last_report_ticks == 10
    assert runtime._counters.last_report_bars == 2


# ---- additional coverage: provider/task/loop/strategy-cycle edge cases -----


@pytest.mark.asyncio
async def test_run_logs_when_provider_close_raises(caplog) -> None:
    """Covers lines 142-143: provider.close() → exception → log & continue."""
    import logging

    class _BadCloseProvider(_StubProvider):
        async def close(self) -> None:
            raise RuntimeError("provider-close-boom")

    runtime = IngestionRuntime(
        provider=_BadCloseProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)
    runtime.stop()
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(task, timeout=2.0)
    assert any("provider_close_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_run_logs_when_background_task_raises(caplog) -> None:
    """Covers lines 149-150: awaited task raises non-CancelledError → log."""
    import logging

    class _KaboomAggregator(BarAggregator):
        def __init__(self) -> None:
            super().__init__()
            self._blew_up = False

        def advance_time(self, now):
            # Raise once from _advance_loop so `await t` surfaces it.
            # Subsequent calls (from _shutdown) must be benign.
            if not self._blew_up:
                self._blew_up = True
                raise RuntimeError("advance-boom")
            return []

    # advance_interval very short so _advance_loop raises before stop()
    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=_KaboomAggregator(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=0.01,
    )
    task = asyncio.create_task(runtime.run())
    # Give advance_loop a moment to fire and explode
    await asyncio.sleep(0.05)
    runtime.stop()
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(task, timeout=2.0)
    assert any("task_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_ingest_loop_returns_when_stop_set_mid_stream() -> None:
    """Covers line 220: stop check inside events() loop returns early."""
    stop_event_ref: list = []

    class _BurstProvider:
        def __init__(self) -> None:
            self._ticks_sent = 0

        async def connect(self) -> None:
            pass

        async def subscribe(self, symbols) -> None:
            pass

        async def close(self) -> None:
            pass

        async def events(self):
            from tradestation_data.domain.tick import Tick

            while True:
                # After first yield, set stop so the post-handle check fires.
                yield Tick(
                    symbol="SPY",
                    timestamp=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
                    price=450.0,
                    volume=1,
                    bid=None,
                    ask=None,
                    tick_count=1,
                    source="test",
                )
                if stop_event_ref:
                    stop_event_ref[0].set()
                await asyncio.sleep(0)

    provider = _BurstProvider()
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
    )
    stop_event_ref.append(runtime._stop)
    await asyncio.wait_for(runtime.run(), timeout=2.0)
    assert runtime._counters.ticks_in >= 1


@pytest.mark.asyncio
async def test_advance_loop_emits_aggregator_and_direct_bars() -> None:
    """Covers lines 227 and 229: advance_loop emits both kinds of closed bars."""

    class _FakeAggregator(BarAggregator):
        def __init__(self) -> None:
            super().__init__()
            self._called = False

        def advance_time(self, now):
            if self._called:
                return []
            self._called = True
            yield _bar("SPY", datetime(2026, 4, 20, 13, 30, tzinfo=UTC), close=451.0)

    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=_FakeAggregator(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=0.01,
    )
    # Seed a direct bar that is past the grace window.
    old_ts = datetime.now(tz=UTC) - timedelta(minutes=2)
    runtime._current_direct_bars["NVDA"] = _bar("NVDA", old_ts, close=200.0)

    observed: list = []
    runtime._on_bar = lambda b: observed.append(b)

    task = asyncio.create_task(runtime._advance_loop())
    for _ in range(200):
        if len(observed) >= 2:
            break
        await asyncio.sleep(0.01)
    runtime._stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    symbols_emitted = {b.symbol for b in observed}
    assert "SPY" in symbols_emitted
    assert "NVDA" in symbols_emitted


@pytest.mark.asyncio
async def test_flush_loop_logs_when_sink_flush_raises(caplog) -> None:
    """should_flush() True → flush() raises → SinkPipeline catches & logs."""
    import logging

    from tradestation_data.sinks.base import BaseSink

    class _FlushBoomSink(BaseSink):
        name = "flush_boom"

        def should_flush(self) -> bool:
            return True

        def flush(self) -> None:
            raise RuntimeError("flush-boom")

    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        sinks=SinkPipeline([_FlushBoomSink()]),
        heartbeat_interval=3600,
        flush_poll_interval=0.01,
        advance_interval=3600,
    )
    task = asyncio.create_task(runtime._flush_loop())
    with caplog.at_level(logging.ERROR):
        await asyncio.sleep(0.05)
        runtime._stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    assert any("sink_flush_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_heartbeat_loop_invokes_emit() -> None:
    """Covers line 243: heartbeat_loop invokes _emit_heartbeat."""
    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        heartbeat_interval=0.01,
        flush_poll_interval=3600,
        advance_interval=3600,
    )
    calls: list[int] = []
    runtime._emit_heartbeat = lambda: calls.append(1)  # type: ignore[method-assign]
    task = asyncio.create_task(runtime._heartbeat_loop())
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    runtime._stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert calls
