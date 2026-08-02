from __future__ import annotations

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import zmq
import zmq.asyncio

from tradestation_data.aggregation import MarketSnapshot
from tradestation_data.runtime import IngestionRuntime
from tradestation_data.sinks import SinkPipeline
from tradestation_data.sinks.parquet import ParquetBarSink, ParquetTickSink
from tradestation_data.wire.el_subscriber import TradeStationELProvider


async def _publish(pub: zmq.asyncio.Socket, topic: str, payload: dict) -> None:
    await pub.send_multipart([topic.encode(), json.dumps(payload).encode()])


# `seq` is required by both wire schemas and enforced by the parser, so every
# frame here needs one. A shared monotonic counter keeps them unique and rising
# across a test, which is what the sequence tracker expects — reusing a value
# would log a regression and muddy the assertions with noise unrelated to what
# is under test.
_seq = itertools.count(1)


def _tick_payload(ts: float, px: float) -> dict:
    return {
        "proto": 1,
        "kind": "tick",
        "seq": next(_seq),
        "sid": 7001,
        "ts": ts,
        "px": px,
        "el_volume": 100,
        "el_ticks": 180,
        "el_upticks": 100,
        "el_downticks": 80,
        "el_open_interest": 0,
        "bid": None,
        "ask": None,
    }


def _bar_payload(ts: float, ohlc: tuple[float, float, float, float], el_volume: int) -> dict:
    """A proto-1 bar frame. `ts` is right-labelled — EL stamps the close."""
    o, h, low, c = ohlc
    return {
        "proto": 1,
        "kind": "bar",
        "tf": "1m",
        "seq": next(_seq),
        "sid": 7001,
        "ts": ts,
        "o": o,
        "h": h,
        "l": low,
        "c": c,
        "el_volume": el_volume,
        # Mutually underivable on purpose — see the note in test_bar_writer.py.
        "el_ticks": el_volume * 2 + 7,
        "el_upticks": el_volume + 3,
        "el_downticks": el_volume + 5,
        "el_open_interest": 0,
    }


@pytest.mark.asyncio
async def test_runtime_pipes_ticks_to_snapshot_and_storage(
    zmq_inproc_bus,
    tmp_path: Path,
) -> None:
    """Ticks reach the snapshot and the tick sink — and produce no bar.

    A bar sink is wired up precisely so the absence is asserted rather than
    assumed: nothing in this binding builds a bar from ticks, so a bars
    partition appearing here would mean a derivation crept back in.
    """
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    snap = MarketSnapshot()
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
    for ts, px in ((bucket1 + 1, 450.0), (bucket1 + 30, 450.5), (bucket2 + 1, 451.0)):
        await _publish(pub, "SPY", _tick_payload(ts, px))

    for _ in range(200):
        state = snap.state_of("SPY")
        if state is not None and state.last_tick is not None:
            break
        await asyncio.sleep(0.01)

    # Snapshot sees the ticks; no bar was ever published, so it has none.
    state = snap.state_of("SPY")
    assert state is not None
    assert state.last_tick is not None
    assert state.last_tick.price == pytest.approx(451.0)
    assert state.last_closed_bar is None
    assert observed_bars == []

    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # Tick parquet written
    tick_file = tmp_path / "ticks" / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert tick_file.exists()
    assert pq.read_table(tick_file).num_rows == 3

    # Nothing derived a bar from them.
    assert not list((tmp_path / "bars").rglob("bars.parquet"))


@pytest.mark.asyncio
async def test_runtime_preserves_published_bar_ohlc(
    zmq_inproc_bus,
    tmp_path: Path,
) -> None:
    """Whole bars land on the snapshot and the bar sink unchanged.

    Proven by checking OHLC survives: anything that rebuilt the bar from its
    close price alone would collapse O=H=L=C onto the close.
    """
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    snap = MarketSnapshot()
    pipeline = SinkPipeline([ParquetBarSink(name="bars_parquet", root=tmp_path / "bars")])

    observed: list = []
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=snap,
        sinks=pipeline,
        on_bar=lambda b: observed.append(b),
        heartbeat_interval=3600,
        flush_poll_interval=0.02,
        advance_interval=3600,  # keep wall-clock advance quiet
    )
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0)

    ts_el = datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC).timestamp()
    await _publish(pub, "SPY", _bar_payload(ts_el + 0.5, (450.10, 450.75, 449.80, 450.40), 12000))

    # The bar is buffered (replace-last semantics); drain via stop().
    await asyncio.sleep(0.05)
    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(observed) == 1
    bar = observed[0]
    # OHLC preserved end to end: open/high/low are not the close.
    assert bar.open == pytest.approx(450.10)
    assert bar.high == pytest.approx(450.75)
    assert bar.low == pytest.approx(449.80)
    assert bar.close == pytest.approx(450.40)
    assert bar.el_volume == 12000
    # ts 13:30:30 floors to 13:30, then steps back one interval: §2 labels a
    # bar by its left edge, while the wire stamps it at the close.
    assert bar.bucket_start == datetime(2026, 4, 20, 13, 29, 0, tzinfo=UTC)

    # Snapshot accepted the bar.
    state = snap.state_of("SPY")
    assert state is not None
    assert state.last_closed_bar is not None
    assert state.last_closed_bar.high == pytest.approx(450.75)

    # Counter advanced on the direct-bar path.
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
    pipeline = SinkPipeline([ParquetBarSink(name="bars_parquet", root=tmp_path / "bars")])

    observed: list = []
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=snap,
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
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 5, (450.10, 450.20, 450.05, 450.15), 3000))
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 30, (450.10, 450.50, 450.05, 450.45), 8000))
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 55, (450.10, 450.75, 449.80, 450.40), 12000))
    # Wire stamps the close, so these land on the 13:29 / 13:30 left edges.
    # New bucket 13:30 closes 13:29 and buffers itself.
    ts_el_2 = datetime(2026, 4, 20, 13, 31, 0, tzinfo=UTC).timestamp()
    await _publish(pub, "SPY", _bar_payload(ts_el_2 + 1, (450.40, 450.60, 450.30, 450.55), 5000))

    for _ in range(200):
        if len(observed) >= 1 and runtime._counters.bars_direct_updated >= 2:
            break
        await asyncio.sleep(0.01)

    # First bucket emitted carries the last refresh's OHLC (replace-last).
    assert len(observed) == 1, f"intra-bar updates not collapsed: observed={len(observed)}"
    first = observed[0]
    assert first.bucket_start == datetime(2026, 4, 20, 13, 29, 0, tzinfo=UTC)
    assert first.high == pytest.approx(450.75)
    assert first.close == pytest.approx(450.40)
    assert first.el_volume == 12000
    assert runtime._counters.bars_direct_in == 1
    assert runtime._counters.bars_direct_updated == 2

    # Now replay bucket 13:29 — it is stale (<= last_emitted) and must be dropped.
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 55, (450.10, 450.75, 449.80, 450.40), 12000))

    for _ in range(200):
        if runtime._counters.bars_duplicate_dropped >= 1:
            break
        await asyncio.sleep(0.01)
    assert runtime._counters.bars_duplicate_dropped == 1

    runtime.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # stop() drains the still-buffered 13:30 bar via _drain_direct_bars().
    assert len(observed) == 2
    second = observed[1]
    assert second.bucket_start == datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)
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
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
        **kwargs,
    )


def _bar(symbol: str, ts: datetime, close: float = 450.0, timeframe: str = "1m") -> Bar:  # noqa: F821
    from tradestation_data.domain.bar import Bar

    return Bar(
        symbol=symbol,
        bucket_start=ts,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        el_volume=100,
        el_ticks=180,
        el_upticks=100,
        el_downticks=80,
        el_open_interest=0,
        timeframe=timeframe,
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
        el_volume=100,
        el_ticks=180,
        el_upticks=100,
        el_downticks=80,
        el_open_interest=0,
        bid=None,
        ask=None,
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


@pytest.mark.asyncio
async def test_direct_bar_close_deadline_follows_its_own_timeframe() -> None:
    """A 5m bucket must not be closed one minute in.

    A fixed one-minute deadline would emit OHLC covering only the first of
    the bucket's five minutes, then discard every later update as a
    duplicate — leaving bars/timeframe=5m/ full of minute bars with no error
    anywhere.
    """
    runtime = _make_runtime()
    ts = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    await runtime._handle_provider_bar(_bar("SPY", ts, timeframe="5m"))

    assert runtime._advance_direct_bars(ts + timedelta(minutes=1, seconds=5)) == []
    ready = runtime._advance_direct_bars(ts + timedelta(minutes=5, seconds=5))
    assert len(ready) == 1
    assert ready[0].timeframe == "5m"


@pytest.mark.asyncio
async def test_direct_bars_are_buffered_per_symbol_and_timeframe() -> None:
    """One topic now carries every interval the user has a chart open on.

    Keyed on symbol alone, the 1m bar below would evict the buffered 5m bar,
    emit it a minute early, and then park _last_emitted at the 1m bucket so
    the real 5m updates were dropped as duplicates.
    """
    runtime = _make_runtime()
    open_5m = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    emitted: list = []
    runtime._on_bar = emitted.append

    await runtime._handle_provider_bar(_bar("SPY", open_5m, close=1.0, timeframe="5m"))
    # A 1-minute bar for the *next* minute on the same topic.
    await runtime._handle_provider_bar(
        _bar("SPY", open_5m + timedelta(minutes=1), close=2.0, timeframe="1m")
    )
    assert emitted == [], "the 5m bucket must not be closed by 1m traffic"

    # The 5m bucket keeps taking intra-bar refreshes.
    await runtime._handle_provider_bar(_bar("SPY", open_5m, close=9.0, timeframe="5m"))
    assert runtime._counters.bars_duplicate_dropped == 0
    assert runtime._counters.bars_direct_updated == 1

    ready = runtime._advance_direct_bars(open_5m + timedelta(minutes=5, seconds=5))
    by_tf = {b.timeframe: b for b in ready}
    assert by_tf["5m"].close == 9.0, "final 5m OHLC must be the last refresh"
    assert by_tf["1m"].close == 2.0


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

    # advance_interval very short so _advance_loop raises before stop()
    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=0.01,
    )

    # Blow up from inside _advance_loop once, then behave. The aggregator used
    # to be what this test broke; the direct-bar sweep is the loop's only
    # remaining work, so it is what stands in now.
    blew_up = False

    def _boom(now):
        nonlocal blew_up
        if not blew_up:
            blew_up = True
            raise RuntimeError("advance-boom")
        return []

    runtime._advance_direct_bars = _boom  # type: ignore[method-assign]
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
                    el_volume=1,
                    el_ticks=2,
                    el_upticks=1,
                    el_downticks=1,
                    el_open_interest=0,
                    bid=None,
                    ask=None,
                )
                if stop_event_ref:
                    stop_event_ref[0].set()
                await asyncio.sleep(0)

    provider = _BurstProvider()
    runtime = IngestionRuntime(
        provider=provider,
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
    )
    stop_event_ref.append(runtime._stop)
    await asyncio.wait_for(runtime.run(), timeout=2.0)
    assert runtime._counters.ticks_in >= 1


@pytest.mark.asyncio
async def test_advance_loop_closes_a_bar_whose_interval_has_elapsed() -> None:
    """The wall-clock sweep is what stops a quiet symbol's last bar hanging.

    Without it a bar sits in the buffer until the next one for the same
    (symbol, timeframe) arrives -- which for a symbol that stops trading is
    never, so the final bar of the session would never reach a sink.
    """
    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=0.01,
    )
    # Seed a direct bar whose interval plus grace is well past.
    old_ts = datetime.now(tz=UTC) - timedelta(minutes=2)
    runtime._current_direct_bars[("NVDA", "1m")] = _bar("NVDA", old_ts, close=200.0)

    observed: list = []
    runtime._on_bar = lambda b: observed.append(b)

    task = asyncio.create_task(runtime._advance_loop())
    for _ in range(200):
        if observed:
            break
        await asyncio.sleep(0.01)
    runtime._stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert [b.symbol for b in observed] == ["NVDA"]


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
