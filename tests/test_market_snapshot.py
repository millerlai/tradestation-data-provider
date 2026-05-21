from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tradestation_data.aggregation import MarketSnapshot, SessionPolicy, SymbolView
from tradestation_data.domain.bar import Bar
from tradestation_data.domain.position import Position
from tradestation_data.domain.tick import Tick

# 13:30 UTC == 09:30 ET on 2026-04-20 (EDT). Aligned to the regular session open.
T0 = datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)


def _tick(symbol: str, ts: datetime, price: float) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        volume=100,
        bid=None,
        ask=None,
        tick_count=1,
        source="tradestation_el",
    )


def _bar(symbol: str, bucket: datetime, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        bucket_start=bucket,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        tick_count=1,
        source="tradestation_el",
    )


def test_on_tick_creates_state_and_updates_last_tick() -> None:
    snap = MarketSnapshot()
    assert snap.state_of("SPY") is None

    snap.on_tick(_tick("SPY", T0, 450.0))
    st = snap.state_of("SPY")
    assert st is not None
    assert st.last_tick is not None
    assert st.last_tick.price == 450.0

    snap.on_tick(_tick("SPY", T0 + timedelta(seconds=5), 450.5))
    assert snap.state_of("SPY").last_tick.price == 450.5  # type: ignore[union-attr]


def test_on_bar_appends_and_updates_last_closed_bar() -> None:
    snap = MarketSnapshot()
    snap.on_bar(_bar("SPY", T0, 450.0))
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=1), 451.0))

    st = snap.state_of("SPY")
    assert st is not None
    assert st.last_closed_bar is not None
    assert st.last_closed_bar.close == 451.0
    assert len(st.recent_bars) == 2
    assert [b.close for b in st.recent_bars] == [450.0, 451.0]


def test_recent_bars_is_bounded_by_max() -> None:
    snap = MarketSnapshot(max_bars_per_symbol=3)
    for i in range(10):
        snap.on_bar(_bar("SPY", T0 + timedelta(minutes=i), 450.0 + i))

    st = snap.state_of("SPY")
    assert st is not None
    assert len(st.recent_bars) == 3
    assert [b.close for b in st.recent_bars] == [457.0, 458.0, 459.0]


def test_symbols_lists_all_seen() -> None:
    snap = MarketSnapshot()
    snap.on_tick(_tick("SPY", T0, 450.0))
    snap.on_bar(_bar("QQQ", T0, 400.0))
    assert sorted(snap.symbols()) == ["QQQ", "SPY"]


def test_position_round_trip() -> None:
    snap = MarketSnapshot()
    assert snap.position_of("SPY") is None
    pos = Position("SPY", 100, 450.0, 0.0, 10.0)
    snap.set_position(pos)
    assert snap.position_of("SPY") is pos

    snap.clear_position("SPY")
    assert snap.position_of("SPY") is None


def test_view_of_returns_none_for_unknown() -> None:
    snap = MarketSnapshot()
    assert snap.view_of("SPY") is None


def test_view_of_returns_immutable_snapshot_decoupled_from_live_state() -> None:
    snap = MarketSnapshot()
    snap.on_tick(_tick("SPY", T0, 450.0))
    snap.on_bar(_bar("SPY", T0, 450.0))
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=1), 451.0))

    view = snap.view_of("SPY")
    assert view is not None
    assert isinstance(view, SymbolView)
    assert view.last_tick is not None
    assert view.last_tick.price == 450.0
    assert view.last_closed_bar is not None
    assert view.last_closed_bar.close == 451.0
    assert isinstance(view.recent_bars, tuple)
    assert len(view.recent_bars) == 2
    assert [b.close for b in view.recent_bars] == [450.0, 451.0]

    # Mutate live state *after* snapshotting — view must stay stable.
    snap.on_tick(_tick("SPY", T0 + timedelta(seconds=30), 452.0))
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=2), 453.0))
    assert view.last_tick.price == 450.0
    assert len(view.recent_bars) == 2
    assert view.last_closed_bar.close == 451.0


def test_views_batch_returns_immutable_copies_for_all_known_symbols() -> None:
    snap = MarketSnapshot()
    snap.on_bar(_bar("SPY", T0, 450.0))
    snap.on_bar(_bar("QQQ", T0, 400.0))

    views = snap.views()
    assert set(views.keys()) == {"SPY", "QQQ"}
    assert all(isinstance(v, SymbolView) for v in views.values())
    assert views["SPY"].last_closed_bar.close == 450.0  # type: ignore[union-attr]

    filtered = snap.views(["SPY"])
    assert set(filtered.keys()) == {"SPY"}


def test_positions_returns_decoupled_copy() -> None:
    snap = MarketSnapshot()
    pos = Position("SPY", 100, 450.0, 0.0, 10.0)
    snap.set_position(pos)

    copy1 = snap.positions()
    assert copy1 == {"SPY": pos}

    # Mutating the returned dict must not affect the live snapshot.
    copy1.clear()
    assert snap.position_of("SPY") is pos


# ---- session-aware retention ---------------------------------------------

_BREADTH_POLICY = SessionPolicy(session_reset=True, pre_market_window_minutes=None)
_CONTINUOUS_POLICY = SessionPolicy(session_reset=False, pre_market_window_minutes=60)


def test_session_reset_clears_deque_across_session_boundary() -> None:
    snap = MarketSnapshot(symbol_policies={"$ADD": _BREADTH_POLICY})
    # Three bars from 2026-04-20 session
    for i in range(3):
        snap.on_bar(_bar("$ADD", T0 + timedelta(minutes=i), 100.0 + i))
    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 3
    assert st.session_date == date(2026, 4, 20)

    # Next bar belongs to 2026-04-21 session → deque must reset.
    next_open = T0 + timedelta(days=1)  # 09:30 ET on the next day
    snap.on_bar(_bar("$ADD", next_open, 500.0))

    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 1
    assert st.recent_bars[0].close == 500.0
    assert st.session_date == date(2026, 4, 21)
    assert st.session_open_bar is not None
    assert st.session_open_bar.bucket_start == next_open


def test_session_reset_within_same_session_keeps_history() -> None:
    snap = MarketSnapshot(symbol_policies={"$ADD": _BREADTH_POLICY})
    for i in range(5):
        snap.on_bar(_bar("$ADD", T0 + timedelta(minutes=i), 100.0 + i))
    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 5


def test_continuous_policy_evicts_pre_market_outside_window() -> None:
    # pre_market_window_minutes=60 → cutoff is 08:30 ET (12:30 UTC in April)
    snap = MarketSnapshot(symbol_policies={"SPY": _CONTINUOUS_POLICY})
    pre_open = T0.replace(hour=12, minute=0)  # 08:00 ET — OUTSIDE 60-min window
    within_window = T0.replace(hour=12, minute=45)  # 08:45 ET — inside window
    snap.on_bar(_bar("SPY", pre_open, 449.0))
    snap.on_bar(_bar("SPY", within_window, 450.0))
    snap.on_bar(_bar("SPY", T0, 451.0))  # 09:30 ET open

    st = snap.state_of("SPY")
    assert st is not None
    # 08:00 bar must have been evicted; 08:45 + 09:30 kept.
    closes = [b.close for b in st.recent_bars]
    assert 449.0 not in closes
    assert closes == [450.0, 451.0]


def test_continuous_policy_keeps_intraday_bars() -> None:
    snap = MarketSnapshot(symbol_policies={"SPY": _CONTINUOUS_POLICY})
    for i in range(10):
        snap.on_bar(_bar("SPY", T0 + timedelta(minutes=i), 450.0 + i))
    st = snap.state_of("SPY")
    assert st is not None
    assert len(st.recent_bars) == 10


def test_session_open_bar_recorded_at_session_open() -> None:
    snap = MarketSnapshot(symbol_policies={"SPY": _CONTINUOUS_POLICY})
    snap.on_bar(_bar("SPY", T0.replace(hour=12, minute=45), 449.0))  # pre-market
    snap.on_bar(_bar("SPY", T0, 450.0))  # 09:30 ET open
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=5), 451.0))

    st = snap.state_of("SPY")
    assert st is not None
    assert st.session_open_bar is not None
    assert st.session_open_bar.bucket_start == T0
    assert st.session_open_bar.open == 450.0


def test_session_open_bar_skipped_when_only_pre_market_bars_seen() -> None:
    snap = MarketSnapshot(symbol_policies={"SPY": _CONTINUOUS_POLICY})
    snap.on_bar(_bar("SPY", T0.replace(hour=12, minute=45), 449.0))  # pre-market only
    st = snap.state_of("SPY")
    assert st is not None
    assert st.session_open_bar is None


def test_default_policy_none_preserves_legacy_behavior() -> None:
    # No policy provided → deque is a pure bar-count window; never resets.
    snap = MarketSnapshot()
    snap.on_bar(_bar("$ADD", T0, 100.0))
    snap.on_bar(_bar("$ADD", T0 + timedelta(days=1), 200.0))

    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 2


def test_view_of_exposes_session_fields() -> None:
    snap = MarketSnapshot(symbol_policies={"SPY": _CONTINUOUS_POLICY})
    snap.on_bar(_bar("SPY", T0, 450.0))

    view = snap.view_of("SPY")
    assert view is not None
    assert view.session_date == date(2026, 4, 20)
    assert view.session_open_bar is not None
    assert view.session_open_bar.open == 450.0
