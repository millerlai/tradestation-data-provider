from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tradestation_data.domain import (
    Bar,
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Tick,
)

_ET = ZoneInfo("America/New_York")


def test_tick_is_frozen_and_equatable() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    a = Tick("SPY", ts, 450.23, 100, 450.22, 450.24, 5, "tradestation_el")
    b = Tick("SPY", ts, 450.23, 100, 450.22, 450.24, 5, "tradestation_el")
    assert a == b
    with pytest.raises(FrozenInstanceError):
        a.price = 451.0  # type: ignore[misc]


def test_tick_allows_none_quote_for_index_symbols() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    vix = Tick("VXX", ts, 18.5, 0, None, None, 0, "tradestation_el")
    assert vix.bid is None
    assert vix.ask is None
    assert vix.volume == 0


def test_bar_zero_volume_index_symbol() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    bar = Bar(
        symbol="VXX",
        bucket_start=ts,
        open=18.5,
        high=18.7,
        low=18.4,
        close=18.6,
        volume=0,
        tick_count=3,
        source="tradestation_el",
    )
    assert bar.volume == 0
    assert bar.tick_count == 3


def test_order_intent_basic() -> None:
    intent = OrderIntent(
        symbol="SPY",
        side=Side.BUY,
        quantity=100,
        order_type=OrderType.MARKET,
        client_ref="advisor-001",
    )
    assert intent.side == "buy"
    assert intent.order_type == "market"
    assert intent.limit_price is None


def test_order_status_enum_values() -> None:
    assert OrderStatus.FILLED == "filled"
    assert OrderStatus.PENDING == "pending"


def test_fill_basic() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    fill = Fill(
        order_id="ord-1",
        symbol="SPY",
        side=Side.BUY,
        quantity=100,
        price=450.25,
        timestamp=ts,
    )
    assert fill.quantity == 100


def test_order_wraps_intent() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    intent = OrderIntent(symbol="SPY", side=Side.BUY, quantity=100, order_type=OrderType.MARKET)
    order = Order(
        order_id="ord-1",
        intent=intent,
        status=OrderStatus.ACCEPTED,
        submitted_at=ts,
        broker_ref="paper-1",
    )
    assert order.intent is intent
    assert order.status == "accepted"


def test_tick_timestamp_et_returns_et_view() -> None:
    # 13:30 UTC on 2026-04-18 is 09:30 EDT (DST in effect)
    utc = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    tick = Tick("SPY", utc, 450.0, 100, 449.99, 450.01, 1, "tradestation_el")
    et = tick.timestamp_et
    assert et.tzinfo is not None
    assert et.utcoffset() == utc.astimezone(_ET).utcoffset()
    assert et.hour == 9 and et.minute == 30
    # Same instant, different zone.
    assert et == utc


def test_tick_timestamp_et_handles_est_vs_edt() -> None:
    # 2026-02-15 is outside DST (EST, UTC-5); 14:30 UTC = 09:30 EST.
    est_utc = datetime(2026, 2, 15, 14, 30, tzinfo=UTC)
    tick_est = Tick("SPY", est_utc, 1.0, 0, None, None, 0, "x")
    assert tick_est.timestamp_et.hour == 9 and tick_est.timestamp_et.minute == 30

    # 2026-07-15 is inside DST (EDT, UTC-4); 13:30 UTC = 09:30 EDT.
    edt_utc = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    tick_edt = Tick("SPY", edt_utc, 1.0, 0, None, None, 0, "x")
    assert tick_edt.timestamp_et.hour == 9 and tick_edt.timestamp_et.minute == 30


def test_bar_bucket_start_et_returns_et_view() -> None:
    utc = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    bar = Bar(
        symbol="SPY",
        bucket_start=utc,
        open=450.0,
        high=450.5,
        low=449.5,
        close=450.2,
        volume=1000,
        tick_count=10,
        source="x",
    )
    et = bar.bucket_start_et
    assert et == utc
    assert et.hour == 9 and et.minute == 30
    # Property is derived, not a stored field — mutating bucket_start is
    # impossible (frozen), so the ET view is always consistent.


def test_bar_bucket_start_et_dst_fallback_preserves_distinct_instants() -> None:
    # 2026 DST ends 2026-11-01; the 01:xx ET hour repeats.
    # 01:30 EDT = 05:30 UTC, 01:30 EST = 06:30 UTC — two distinct instants.
    edt = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    est = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    bar_edt = Bar("SPY", edt, 1, 1, 1, 1, 0, 0, "x")
    bar_est = Bar("SPY", est, 1, 1, 1, 1, 0, 0, "x")
    assert bar_edt.bucket_start_et.hour == 1
    assert bar_est.bucket_start_et.hour == 1
    # UTC instants differ even though ET wall-clock reads the same.
    assert bar_edt.bucket_start != bar_est.bucket_start


def test_position_flat_long_short() -> None:
    long_pos = Position("SPY", 100, 450.0, 0.0, 25.0)
    short_pos = Position("SPY", -50, 450.0, 0.0, -10.0)
    assert long_pos.quantity > 0
    assert short_pos.quantity < 0


def test_side_buy_sell_classifiers() -> None:
    assert Side.BUY.is_buy_side and not Side.BUY.is_sell_side
    assert Side.BUY_TO_COVER.is_buy_side and not Side.BUY_TO_COVER.is_sell_side
    assert Side.SELL.is_sell_side and not Side.SELL.is_buy_side
    assert Side.SELL_SHORT.is_sell_side and not Side.SELL_SHORT.is_buy_side


def test_side_string_values_are_stable_wire_format() -> None:
    """Wire format depends on these literals — guard against accidental rename."""
    assert Side.BUY == "buy"
    assert Side.SELL == "sell"
    assert Side.SELL_SHORT == "sell_short"
    assert Side.BUY_TO_COVER == "buy_to_cover"
