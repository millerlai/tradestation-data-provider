from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradestation_data.aggregation import BarAggregator
from tradestation_data.domain.tick import Tick


def _tick(
    symbol: str,
    ts: datetime,
    price: float,
    volume: int = 100,
    tick_count: int = 1,
    source: str = "tradestation_el",
) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        volume=volume,
        bid=None,
        ask=None,
        tick_count=tick_count,
        source=source,
    )


T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)


def test_ticks_in_same_bucket_do_not_emit() -> None:
    agg = BarAggregator()
    assert agg.ingest(_tick("SPY", T0 + timedelta(seconds=1), 450.0)) == []
    assert agg.ingest(_tick("SPY", T0 + timedelta(seconds=30), 450.5)) == []
    assert agg.ingest(_tick("SPY", T0 + timedelta(seconds=59), 450.2)) == []


def test_cross_bucket_emits_previous_bar() -> None:
    agg = BarAggregator()
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=1), 450.0, volume=100))
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=30), 451.0, volume=200))
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=45), 449.5, volume=100))

    # A tick in the next minute flushes the previous bar
    emitted = agg.ingest(_tick("SPY", T0 + timedelta(seconds=61), 450.2, volume=50))
    assert len(emitted) == 1
    bar = emitted[0]
    assert bar.symbol == "SPY"
    assert bar.bucket_start == T0
    assert bar.open == pytest.approx(450.0)
    assert bar.high == pytest.approx(451.0)
    assert bar.low == pytest.approx(449.5)
    assert bar.close == pytest.approx(449.5)
    assert bar.volume == 400
    assert bar.tick_count == 3
    # §2.3 — a bar this binding computed must not be mistakable for one the
    # wire delivered, or the two end up indistinguishable in the same
    # timeframe=1m partition.
    assert bar.source == "derived:ticks"


def test_gap_fills_with_empty_bars() -> None:
    agg = BarAggregator()
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=10), 450.0, volume=100))
    # Jump forward 3 minutes — previous bar closes, 2 empty bars fill the gap
    emitted = agg.ingest(_tick("SPY", T0 + timedelta(minutes=3, seconds=5), 451.0))
    assert [b.bucket_start for b in emitted] == [
        T0,
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=2),
    ]
    closed, e1, e2 = emitted
    assert closed.source == "derived:ticks"
    assert closed.volume == 100
    for empty in (e1, e2):
        assert empty.source == "derived:empty"
        assert empty.volume == 0
        assert empty.tick_count == 0
        # Empty bars carry forward the previous close
        assert empty.open == empty.high == empty.low == empty.close == pytest.approx(450.0)


def test_advance_time_flushes_in_progress_bar() -> None:
    agg = BarAggregator()
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=5), 450.0, volume=100))
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=55), 450.5, volume=100))

    # Still inside the bucket — nothing to flush
    assert agg.advance_time(T0 + timedelta(seconds=59)) == []

    emitted = agg.advance_time(T0 + timedelta(seconds=61))
    assert len(emitted) == 1
    assert emitted[0].bucket_start == T0
    assert emitted[0].close == pytest.approx(450.5)
    assert emitted[0].volume == 200

    # A second call must not re-emit
    assert agg.advance_time(T0 + timedelta(minutes=5)) == []


def test_out_of_order_tick_is_dropped() -> None:
    agg = BarAggregator()
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=30), 450.0, volume=100))
    # Earlier-than-last tick — dropped silently
    assert agg.ingest(_tick("SPY", T0 + timedelta(seconds=5), 999.0, volume=500)) == []

    # Flush and verify the bogus tick did not contaminate the bar
    emitted = agg.advance_time(T0 + timedelta(minutes=2))
    assert len(emitted) == 1
    bar = emitted[0]
    assert bar.volume == 100
    assert bar.high == pytest.approx(450.0)
    assert bar.low == pytest.approx(450.0)


def test_zero_volume_index_symbol_bar() -> None:
    agg = BarAggregator()
    # Index-style tick: vol=0
    agg.ingest(_tick("VXX", T0 + timedelta(seconds=10), 18.5, volume=0, tick_count=0))
    agg.ingest(_tick("VXX", T0 + timedelta(seconds=50), 18.6, volume=0, tick_count=0))
    emitted = agg.advance_time(T0 + timedelta(minutes=2))
    assert len(emitted) == 1
    bar = emitted[0]
    assert bar.volume == 0
    assert bar.open == pytest.approx(18.5)
    assert bar.close == pytest.approx(18.6)


def test_multiple_symbols_are_tracked_independently() -> None:
    agg = BarAggregator()
    agg.ingest(_tick("SPY", T0 + timedelta(seconds=5), 450.0, volume=100))
    agg.ingest(_tick("QQQ", T0 + timedelta(seconds=5), 400.0, volume=50))

    # SPY crosses into next bucket, QQQ does not
    emitted = agg.ingest(_tick("SPY", T0 + timedelta(seconds=65), 451.0, volume=100))
    assert len(emitted) == 1
    assert emitted[0].symbol == "SPY"

    flushed = agg.advance_time(T0 + timedelta(minutes=2))
    symbols = sorted(b.symbol for b in flushed)
    assert symbols == ["QQQ", "SPY"]
