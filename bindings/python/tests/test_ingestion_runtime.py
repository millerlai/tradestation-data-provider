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
    # ts 13:30:30 floors to 13:30 and lands verbatim — bar_time is the
    # wire's close time, with no shift and no grid snap.
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
    # Three intra-bar refreshes for bucket 13:30 — close/high/volume grow.
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 5, (450.10, 450.20, 450.05, 450.15), 3000))
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 30, (450.10, 450.50, 450.05, 450.45), 8000))
    await _publish(pub, "SPY", _bar_payload(ts_el_1 + 55, (450.10, 450.75, 449.80, 450.40), 12000))
    # bar_time is the verbatim close, so these three refreshes all land on
    # 13:30. The next payload's bar_time (13:31) is a new bucket: it closes
    # and emits the buffered 13:30 bar, then buffers itself.
    ts_el_2 = datetime(2026, 4, 20, 13, 31, 0, tzinfo=UTC).timestamp()
    await _publish(pub, "SPY", _bar_payload(ts_el_2 + 1, (450.40, 450.60, 450.30, 450.55), 5000))

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
async def test_subminute_chart_is_reported_not_silently_coalesced(caplog) -> None:
    """Two points sharing a bar_time whose high/low cannot be one bar.

    A 1-second chart reads bar_type 1 / bar_interval 1 on the wire exactly as a
    1-minute chart does, so the buffer cannot be skipped the way bar_type 0
    skips it, and ~59 of every 60 prints are replaced away. EL sees the
    condition and latches SubMinuteChart, but that flag never reaches the wire.
    Saying so is the part this side can do.
    """
    import logging

    from tradestation_data.domain.bar import Bar

    def _b(high: float, low: float) -> Bar:
        return Bar(
            symbol="SPY",
            bar_time=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
            open=450.0,
            high=high,
            low=low,
            close=450.0,
            el_volume=100,
            el_ticks=180,
            el_upticks=100,
            el_downticks=80,
            el_open_interest=0,
            bar_type=1,
            bar_interval=1,
            category=2,
        )

    def _warned(records) -> int:
        return sum(1 for r in records if "subminute_chart_suspected" in r.message)

    runtime = _make_runtime()
    with caplog.at_level(logging.WARNING):
        await runtime._handle_provider_bar(_b(450.5, 449.5))
        # A genuine intra-bar refresh only ever widens the range.
        await runtime._handle_provider_bar(_b(450.6, 449.4))
        assert runtime._counters.bars_subminute_suspected == 0
        assert _warned(caplog.records) == 0

        # A high that drops cannot have come from the same bar.
        await runtime._handle_provider_bar(_b(450.2, 449.4))
        assert runtime._counters.bars_subminute_suspected == 1
        assert _warned(caplog.records) == 1

        # Latched: the counter keeps moving, the warning does not repeat.
        await runtime._handle_provider_bar(_b(450.1, 449.4))
        assert runtime._counters.bars_subminute_suspected == 2
        assert _warned(caplog.records) == 1


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


def test_wire_silent_fires_once_and_only_once_when_no_frames_have_arrived(caplog) -> None:
    """Say-once, like the DLL's own -7.

    Hub down, wrong port, or both ends bound instead of one bound one
    connected all look identical from here: silence. Repeating this every
    heartbeat for the rest of the process's life would bury the one line
    that matters, so it must survive a second heartbeat without repeating.
    """
    import logging

    class _SilentProvider(_StubProvider):
        frames_received = 0
        endpoint = "tcp://127.0.0.1:5556"

    runtime = IngestionRuntime(
        provider=_SilentProvider(),
        symbols=["SPY", "QQQ"],
        snapshot=MarketSnapshot(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
    )
    with caplog.at_level(logging.WARNING):
        runtime._emit_heartbeat()
        runtime._emit_heartbeat()

    silent = [r for r in caplog.records if r.message == "wire_silent"]
    assert len(silent) == 1, "must fire at most once per process, not once per heartbeat"
    assert silent[0].endpoint == "tcp://127.0.0.1:5556"
    assert silent[0].symbols_subscribed == 2


def test_wire_silent_does_not_fire_once_frames_have_arrived(caplog) -> None:
    """The precondition, not just the dedup: a transport that delivered even
    one frame (a hello counts) is proven alive and must never trip this,
    however quiet the market gets afterwards."""
    import logging

    class _LiveProvider(_StubProvider):
        frames_received = 5

    runtime = IngestionRuntime(
        provider=_LiveProvider(),
        symbols=["SPY"],
        snapshot=MarketSnapshot(),
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
    )
    with caplog.at_level(logging.WARNING):
        runtime._emit_heartbeat()

    assert not [r for r in caplog.records if r.message == "wire_silent"]


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


# ---- on_partial_bar ----------------------------------------------------
#
# The developing-bar channel. Its entire contract is "fires exactly where
# _handle_provider_bar installs a frame as the buffer's current bar for its
# series", so most of what follows is about the frames where it must NOT fire.


def _developing(
    bar_time: datetime,
    *,
    high: float,
    low: float,
    close: float,
    bar_type: int = 1,
    bar_interval: int = 5,
) -> Bar:
    """A bar whose high/low are set independently of close.

    `_bar` derives both from `close`, so a refreshed close raises the low too
    and trips the sub-minute detector — which is a different test's subject. A
    real intra-bar refresh only ever raises the high or lowers the low.
    """
    return Bar(
        symbol="SPY",
        bar_time=bar_time,
        open=450.0,
        high=high,
        low=low,
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
async def test_partial_fires_on_every_intra_bar_frame() -> None:
    """Three refinements of one 5m bucket, then the next bucket closes it."""
    partial: list[Bar] = []
    closed: list[Bar] = []
    runtime = _make_runtime(on_bar=closed.append, on_partial_bar=partial.append)

    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    await runtime._handle_provider_bar(_developing(t0, high=450.2, low=449.8, close=450.0))
    await runtime._handle_provider_bar(_developing(t0, high=450.9, low=449.8, close=450.7))
    await runtime._handle_provider_bar(_developing(t0, high=450.9, low=449.1, close=449.3))
    t1 = t0 + timedelta(minutes=5)
    await runtime._handle_provider_bar(_developing(t1, high=449.5, low=449.2, close=449.4))

    # 3 refinements plus the first frame of the next bucket.
    assert [b.close for b in partial] == [450.0, 450.7, 449.3, 449.4]
    # Only t0 closed; t1 is still developing in the buffer.
    assert [b.bar_time for b in closed] == [t0]
    # And it closed carrying the LAST refinement, not the first.
    assert closed[0].close == pytest.approx(449.3)
    assert runtime._counters.bars_partial_out == 4


@pytest.mark.asyncio
async def test_close_precedes_partial_on_rollover() -> None:
    """Order matters: "the previous bar ended" before "this one began"."""
    events: list[tuple[str, datetime]] = []
    runtime = _make_runtime(
        on_bar=lambda b: events.append(("closed", b.bar_time)),
        on_partial_bar=lambda b: events.append(("partial", b.bar_time)),
    )

    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    await runtime._handle_provider_bar(_developing(t0, high=450.2, low=449.8, close=450.0))
    await runtime._handle_provider_bar(_developing(t1, high=449.5, low=449.2, close=449.4))

    assert events == [("partial", t0), ("closed", t0), ("partial", t1)]


@pytest.mark.asyncio
async def test_tick_chart_never_fires_partial() -> None:
    """bar_type 0 has no developing state — every print is already final."""
    partial: list[Bar] = []
    closed: list[Bar] = []
    runtime = _make_runtime(on_bar=closed.append, on_partial_bar=partial.append)

    ts = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    for close in (450.0, 450.1, 450.2):
        await runtime._handle_provider_bar(_bar("SPY", ts, close=close, bar_type=0))

    assert partial == []
    assert len(closed) == 3
    assert runtime._counters.bars_partial_out == 0


@pytest.mark.asyncio
async def test_partial_not_fired_for_stale_or_reordered_frames() -> None:
    """A frame the buffer refuses is not the series' current state."""
    partial: list[Bar] = []
    runtime = _make_runtime(on_partial_bar=partial.append)

    newer = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    older = newer - timedelta(minutes=5)
    await runtime._handle_provider_bar(_bar("SPY", newer))
    assert len(partial) == 1

    # Out of order: older than what is buffered. Dropped, not reported.
    await runtime._handle_provider_bar(_bar("SPY", older))
    assert len(partial) == 1
    assert runtime._counters.bars_duplicate_dropped == 1

    # Rolling forward closes `newer` and reports the new bucket as partial.
    await runtime._handle_provider_bar(_bar("SPY", newer + timedelta(minutes=5)))
    assert len(partial) == 2

    # Stale: a chart reload replaying a bucket already closed and emitted.
    await runtime._handle_provider_bar(_bar("SPY", newer))
    assert len(partial) == 2
    assert runtime._counters.bars_duplicate_dropped == 2


@pytest.mark.asyncio
async def test_every_closed_bar_was_partial_first() -> None:
    """The invariant that makes the two channels safe to reason about.

    A bar can only close by leaving the buffer, and it can only enter the
    buffer through one of the three assignments that fire on_partial_bar. So
    for bar_type != 0 there is no such thing as a bar that closes without
    having been reported as developing at least once first.
    """
    partial: list[Bar] = []
    closed: list[Bar] = []
    runtime = _make_runtime(on_bar=closed.append, on_partial_bar=partial.append)

    t = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    for i in range(6):
        await runtime._handle_provider_bar(
            _bar("SPY", t + timedelta(minutes=5 * i), close=450.0 + i)
        )
    # Drain the last bucket so it counts as closed too.
    for bar in runtime._drain_direct_bars():
        await runtime._on_closed_bar(bar)

    assert len(closed) == 6
    assert len(partial) >= len(closed)
    assert {b.bar_time for b in closed} <= {b.bar_time for b in partial}


@pytest.mark.asyncio
async def test_partial_callback_exception_is_latched_and_swallowed(caplog) -> None:
    """A broken callback must not kill ingestion, nor write one line per frame."""
    import logging

    def _boom(bar: Bar) -> None:
        raise RuntimeError("cb failed")

    runtime = _make_runtime(on_partial_bar=_boom)
    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    with caplog.at_level(logging.ERROR):
        for close in (450.0, 450.1, 450.2, 450.3):
            await runtime._handle_provider_bar(_developing(t0, high=450.9, low=449.1, close=close))

    failures = [r for r in caplog.records if "on_partial_bar_callback_failed" in r.message]
    assert len(failures) == 1, "latched — a traceback per frame would bury the log"
    assert runtime._counters.partial_callback_failed == 4
    assert runtime._counters.bars_partial_out == 4
    # The buffer still advanced despite every callback raising.
    assert runtime._current_direct_bars[("SPY", 1, 5)].close == pytest.approx(450.3)


@pytest.mark.asyncio
async def test_partial_never_reaches_sinks_or_snapshot() -> None:
    """Makes "a partial cannot pollute storage" a fact rather than an intent."""
    from tradestation_data.sinks.memory import InMemorySink

    sink = InMemorySink(name="mem")
    snapshot = MarketSnapshot()
    partial: list[Bar] = []
    runtime = IngestionRuntime(
        provider=_StubProvider(),
        symbols=["SPY"],
        snapshot=snapshot,
        sinks=SinkPipeline([sink]),
        on_partial_bar=partial.append,
        heartbeat_interval=3600,
        flush_poll_interval=3600,
        advance_interval=3600,
    )

    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    for close in (450.0, 450.5, 451.0):
        await runtime._handle_provider_bar(_developing(t0, high=451.2, low=449.1, close=close))

    assert len(partial) == 3
    assert list(snapshot.symbols()) == []
    assert sink.bars() == []
    assert runtime._counters.bars_out == 0


@pytest.mark.asyncio
async def test_no_partial_callback_registered_is_a_no_op() -> None:
    """The default path: not passing on_partial_bar changes nothing."""
    closed: list[Bar] = []
    runtime = _make_runtime(on_bar=closed.append)

    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    await runtime._handle_provider_bar(_developing(t0, high=450.2, low=449.8, close=450.0))
    await runtime._handle_provider_bar(
        _developing(t0 + timedelta(minutes=5), high=449.5, low=449.2, close=449.4)
    )

    assert len(closed) == 1
    # Counted on delivery, so "nobody registered" reads as 0 rather than
    # "here is how many you would have received".
    assert runtime._counters.bars_partial_out == 0


def _partial_failures(caplog) -> int:
    return sum("on_partial_bar_callback_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_async_callbacks_are_refused_at_construction() -> None:
    """An `async def` would have its coroutine built and dropped unrun.

    Silently: the callback never executes, the counters still record every
    frame as delivered, and nothing is logged. The docstrings push people
    toward it by warning that a slow callback back-pressures the socket, so
    the constructor has to say no.
    """

    async def _acb(bar: Bar) -> None:  # pragma: no cover - never invoked
        pass

    with pytest.raises(TypeError, match="on_partial_bar must be a plain callable"):
        _make_runtime(on_partial_bar=_acb)
    with pytest.raises(TypeError, match="on_bar must be a plain callable"):
        _make_runtime(on_bar=_acb)


@pytest.mark.asyncio
async def test_partial_failure_latch_is_keyed_by_series_and_exception_type(caplog) -> None:
    """A benign first exception must not silence a different one hours later.

    A single process-wide bool would: one warm-up KeyError at 09:31 and the
    OSError that starts at 14:00 is never written down at all.
    """
    import logging

    raising: list[type[Exception]] = [RuntimeError]

    def _boom(bar: Bar) -> None:
        raise raising[0]("cb failed")

    runtime = _make_runtime(on_partial_bar=_boom)
    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)

    def _frame(close: float, *, bar_interval: int = 5) -> Bar:
        return _developing(t0, high=450.9, low=449.1, close=close, bar_interval=bar_interval)

    with caplog.at_level(logging.ERROR):
        # Same series, same exception type: one line however many frames.
        await runtime._handle_provider_bar(_frame(450.0))
        await runtime._handle_provider_bar(_frame(450.1))
        assert _partial_failures(caplog) == 1

        # Same series, a DIFFERENT exception type: said again.
        raising[0] = OSError
        await runtime._handle_provider_bar(_frame(450.2))
        assert _partial_failures(caplog) == 2

        # A different series, same exception type: said again.
        await runtime._handle_provider_bar(_frame(450.3, bar_interval=15))
        assert _partial_failures(caplog) == 3

    assert runtime._counters.partial_callback_failed == 4


@pytest.mark.asyncio
async def test_partial_precedes_close_on_the_wall_clock_path() -> None:
    """The invariant on the path a quiet symbol actually closes through.

    Rollover is not the only way a bar closes, and it is not the way a breadth
    index or a thin option closes — those only ever leave the buffer through
    `_advance_direct_bars`.
    """
    partial: list[Bar] = []
    closed: list[Bar] = []
    runtime = _make_runtime(on_bar=closed.append, on_partial_bar=partial.append)

    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    await runtime._handle_provider_bar(_developing(t0, high=450.2, low=449.8, close=450.0))
    assert [b.bar_time for b in partial] == [t0]
    assert closed == []

    for bar in runtime._advance_direct_bars(t0 + timedelta(seconds=3)):
        await runtime._on_closed_bar(bar)

    assert [b.bar_time for b in closed] == [t0]
    assert {b.bar_time for b in closed} <= {b.bar_time for b in partial}


@pytest.mark.asyncio
async def test_partial_precedes_close_on_the_shutdown_path() -> None:
    """Same invariant through `_shutdown()`, the third and last close path."""
    partial: list[Bar] = []
    closed: list[Bar] = []
    runtime = _make_runtime(on_bar=closed.append, on_partial_bar=partial.append)

    t0 = datetime(2026, 4, 20, 13, 35, tzinfo=UTC)
    await runtime._handle_provider_bar(_developing(t0, high=450.2, low=449.8, close=450.0))
    assert closed == []

    await runtime._shutdown()

    assert [b.bar_time for b in closed] == [t0]
    assert {b.bar_time for b in closed} <= {b.bar_time for b in partial}
