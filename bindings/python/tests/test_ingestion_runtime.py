from __future__ import annotations

import asyncio
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest
import zmq
import zmq.asyncio

from tradestation_data.aggregation import MarketSnapshot
from tradestation_data.domain.bar import Bar
from tradestation_data.runtime import IngestionRuntime
from tradestation_data.sinks import SinkPipeline
from tradestation_data.sinks.parquet import ParquetBarSink
from tradestation_data.wire.el_subscriber import TradeStationELProvider


async def _publish(pub: zmq.asyncio.Socket, topic: str, payload: dict) -> None:
    await pub.send_multipart([topic.encode(), json.dumps(payload).encode()])


# `seq` is required by both wire schemas and enforced by the parser, so every
# frame here needs one. A shared monotonic counter keeps them unique and rising
# across a test, which is what the sequence tracker expects — reusing a value
# would log a regression and muddy the assertions with noise unrelated to what
# is under test.
_ET = ZoneInfo("America/New_York")
_seq = itertools.count(1)


def _bar_payload(
    ts: float,
    ohlc: tuple[float, float, float, float],
    el_volume: int,
    *,
    ts_str: str | None = None,
    bar_type: int = 1,
    bar_interval: int = 1,
) -> dict:
    """A proto-2 frame. `ts_str` is EL's close time and lands verbatim."""
    o, h, low, c = ohlc
    if ts_str is None:
        ts_str = datetime.fromtimestamp(ts, UTC).astimezone(_ET).strftime("%Y-%m/%d-%H:%M:%S")
    return {
        "proto": 2,
        "seq": next(_seq),
        "sid": 7001,
        "ts": ts,
        "ts_str": ts_str,
        "bar_type": bar_type,
        "bar_interval": bar_interval,
        "category": 2,
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
        "bid": None,
        "ask": None,
    }


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
    # ts_str "09:30:00" ET lands verbatim as 13:30:00 UTC — bar_time is the
    # wire's close time, with no shift, no grid snap and no second dropped.
    assert bar.bar_time == datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)

    # Snapshot accepted the bar.
    state = snap.state_of("SPY")
    assert state is not None
    assert state.last_closed_bar is not None
    assert state.last_closed_bar.high == pytest.approx(450.75)

    # Counter advanced on the direct-bar path.
    assert runtime._counters.bars_direct_in == 1

    bar_file = (
        tmp_path
        / "bars"
        / "bartype=1"
        / "interval=1"
        / "symbol=SPY"
        / "date=2026-04-20"
        / "bars.parquet"
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
    # Three intra-bar refreshes of ONE forming bar — close/high/volume grow.
    #
    # All three carry the SAME ts_str, because that is what EL does: measured
    # live (semantics.md §1.3), BarDateTime holds steady across every "Update
    # Every Tick" refire of a bar that has not closed. Only `ts`, the DLL's
    # receive clock, advances. The refreshes used to be written with three
    # different ts_str seconds and relied on the binding flooring them
    # together — that flooring is gone, and with it a test that only passed
    # by sending something the publisher never sends.
    bar_1_close = "2026-04/20-09:30:00"  # ET; 13:30 UTC
    await _publish(
        pub,
        "SPY",
        _bar_payload(ts_el_1 + 5, (450.10, 450.20, 450.05, 450.15), 3000, ts_str=bar_1_close),
    )
    await _publish(
        pub,
        "SPY",
        _bar_payload(ts_el_1 + 30, (450.10, 450.50, 450.05, 450.45), 8000, ts_str=bar_1_close),
    )
    await _publish(
        pub,
        "SPY",
        _bar_payload(ts_el_1 + 55, (450.10, 450.75, 449.80, 450.40), 12000, ts_str=bar_1_close),
    )
    # A new bar_time (13:31) closes and emits the buffered 13:30 bar, then
    # buffers itself.
    ts_el_2 = datetime(2026, 4, 20, 13, 31, 0, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        _bar_payload(
            ts_el_2 + 1, (450.40, 450.60, 450.30, 450.55), 5000, ts_str="2026-04/20-09:31:00"
        ),
    )

    for _ in range(200):
        if len(observed) >= 1 and runtime._counters.bars_direct_updated >= 2:
            break
        await asyncio.sleep(0.01)

    # First bucket emitted carries the last refresh's OHLC (replace-last).
    assert len(observed) == 1, f"intra-bar updates not collapsed: observed={len(observed)}"
    first = observed[0]
    assert first.bar_time == datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)
    assert first.high == pytest.approx(450.75)
    assert first.close == pytest.approx(450.40)
    assert first.el_volume == 12000
    assert runtime._counters.bars_direct_in == 1
    assert runtime._counters.bars_direct_updated == 2

    # Now replay bucket 13:30 — it is stale (<= last_emitted) and must be dropped.
    await _publish(
        pub,
        "SPY",
        _bar_payload(ts_el_1 + 55, (450.10, 450.75, 449.80, 450.40), 12000, ts_str=bar_1_close),
    )

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
    assert second.bar_time == datetime(2026, 4, 20, 13, 31, 0, tzinfo=UTC)
    assert second.close == pytest.approx(450.55)
    assert runtime._counters.bars_direct_in == 2

    bar_file = (
        tmp_path
        / "bars"
        / "bartype=1"
        / "interval=1"
        / "symbol=SPY"
        / "date=2026-04-20"
        / "bars.parquet"
    )
    assert bar_file.exists()
    assert pq.read_table(bar_file).num_rows == 2


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


def _bar(
    symbol: str,
    ts: datetime,
    close: float = 450.0,
    *,
    bar_type: int = 1,
    bar_interval: int = 1,
) -> Bar:
    from tradestation_data.domain.bar import Bar

    return Bar(
        symbol=symbol,
        bar_time=ts,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        el_volume=100,
        el_ticks=180,
        el_upticks=100,
        el_downticks=80,
        el_open_interest=0,
        bar_type=bar_type,
        bar_interval=bar_interval,
        category=2,
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
async def test_direct_bar_release_deadline_is_close_plus_grace() -> None:
    """bar_time IS the close, so the point releases at close + grace.

    The old formula added the chart's interval on top — right when the label
    was the bar's START, one full interval late now that it is the close. A
    5-minute point closing 13:30 must be out by 13:30:02, not 13:35:02; a
    daily point must not wait an extra day.
    """
    runtime = _make_runtime()
    ts = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    await runtime._handle_provider_bar(_bar("SPY", ts, bar_interval=5))

    # Inside the grace window the point may still be refreshed.
    assert runtime._advance_direct_bars(ts + timedelta(seconds=1)) == []
    ready = runtime._advance_direct_bars(ts + timedelta(seconds=5))
    assert len(ready) == 1
    assert ready[0].bar_interval == 5

    # Daily: same rule, no per-type duration table.
    d = datetime(2026, 4, 20, 20, 0, tzinfo=UTC)
    await runtime._handle_provider_bar(_bar("SPY", d, bar_type=2, bar_interval=1))
    assert runtime._advance_direct_bars(d + timedelta(seconds=1)) == []
    assert len(runtime._advance_direct_bars(d + timedelta(seconds=5))) == 1


@pytest.mark.asyncio
async def test_tick_chart_frames_bypass_the_buffer_entirely() -> None:
    """Every bar_type-0 frame is forwarded the moment it arrives.

    ts_str has minute resolution, so every print inside one minute parses to
    the same bar_time. Routed through the intra-bar buffer, each print
    replaced the previous one and — once the minute was emitted — the
    `<= last_emitted` gate dropped the rest: a live 1-tick chart lost nearly
    its whole stream, silently, where proto 1's tick path forwarded every
    print. The buffer's precondition is that bar_time names the bar
    uniquely, and on a tick chart it does not.
    """
    runtime = _make_runtime()
    emitted: list = []
    runtime._on_bar = emitted.append

    ts = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    # Five prints inside one minute — same bar_time on every frame.
    for i in range(5):
        await runtime._handle_provider_bar(
            _bar("SPY", ts, close=450.0 + i * 0.01, bar_type=0, bar_interval=1)
        )

    assert len(emitted) == 5, "a tick chart's prints must all land, not collapse"
    assert [b.close for b in emitted] == [450.0, 450.01, 450.02, 450.03, 450.04]
    assert runtime._counters.bars_direct_in == 5
    assert runtime._counters.bars_duplicate_dropped == 0

    # And a replayed print (same minute again) still lands: with no unique
    # name there is nothing safe to dedupe on. Offline dedupe is the
    # consumer's call.
    await runtime._handle_provider_bar(_bar("SPY", ts, close=450.0, bar_type=0, bar_interval=1))
    assert len(emitted) == 6


@pytest.mark.asyncio
async def test_direct_bars_are_buffered_per_symbol_and_chart() -> None:
    """One topic now carries every interval the user has a chart open on.

    Keyed on symbol alone, the 1m bar below would evict the buffered 5m bar,
    emit it a minute early, and then park _last_emitted at the 1m bucket so
    the real 5m updates were dropped as duplicates.
    """
    runtime = _make_runtime()
    open_5m = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    emitted: list = []
    runtime._on_bar = emitted.append

    await runtime._handle_provider_bar(_bar("SPY", open_5m, close=1.0, bar_interval=5))
    # A 1-minute bar for the *next* minute on the same topic.
    await runtime._handle_provider_bar(
        _bar("SPY", open_5m + timedelta(minutes=1), close=2.0, bar_interval=1)
    )
    assert emitted == [], "the 5m bucket must not be closed by 1m traffic"

    # The 5m bucket keeps taking intra-bar refreshes.
    await runtime._handle_provider_bar(_bar("SPY", open_5m, close=9.0, bar_interval=5))
    assert runtime._counters.bars_duplicate_dropped == 0
    assert runtime._counters.bars_direct_updated == 1

    ready = runtime._advance_direct_bars(open_5m + timedelta(minutes=5, seconds=5))
    by_interval = {b.bar_interval: b for b in ready}
    assert by_interval[5].close == 9.0, "final 5-minute OHLC must be the last refresh"
    assert by_interval[1].close == 2.0


def test_emit_heartbeat_updates_counters() -> None:
    """Covers lines 405-424."""
    runtime = _make_runtime()
    runtime._counters.bars_out = 2
    runtime._emit_heartbeat()
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
            while True:
                # After the first yield, set stop so the post-handle check fires.
                yield Bar(
                    symbol="SPY",
                    bar_time=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
                    bar_type=1,
                    bar_interval=1,
                    category=2,
                    open=450.0,
                    high=450.0,
                    low=450.0,
                    close=450.0,
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
    assert runtime._counters.bars_direct_in >= 1


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
