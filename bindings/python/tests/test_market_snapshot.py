from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tradestation_data.aggregation import MarketSnapshot, SessionPolicy, SymbolView
from tradestation_data.domain.bar import Bar

# 13:30 UTC == 09:30 ET on 2026-04-20 (EDT). Aligned to the regular session open.
T0 = datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)


def _bar(symbol: str, bucket: datetime, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        bar_time=bucket,
        open=close,
        high=close,
        low=close,
        close=close,
        el_volume=100,
        el_ticks=200,
        el_upticks=100,
        el_downticks=100,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )


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


def test_view_of_returns_none_for_unknown() -> None:
    snap = MarketSnapshot()
    assert snap.view_of("SPY") is None


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


# ---- session-aware retention ---------------------------------------------

_BREADTH_POLICY = SessionPolicy(session_reset=True, pre_market_window_minutes=None)
_CONTINUOUS_POLICY = SessionPolicy(session_reset=False, pre_market_window_minutes=60)


def test_session_reset_clears_deque_across_session_boundary() -> None:
    snap = MarketSnapshot(symbol_policies={"$ADD": _BREADTH_POLICY})
    # Three RTH bars from the 2026-04-20 session. They start at 09:31, not at
    # T0 (09:30): T0 is a CLOSE of exactly the open, i.e. the last PRE-market
    # bar, which a breadth symbol now drops when the session opens. This test
    # is about the day boundary, so it stays clear of the open boundary —
    # test_breadth_bar_closing_exactly_at_the_open_is_pre_market covers that.
    for i in range(1, 4):
        snap.on_bar(_bar("$ADD", T0 + timedelta(minutes=i), 100.0 + i))
    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 3
    assert st.session_date == date(2026, 4, 20)

    # Next bar belongs to 2026-04-21 session → deque must reset.
    # bar_time is the bar's CLOSE, so the first RTH bar closes 09:31 —
    # a close of exactly 09:30 would be the last pre-market bar.
    next_open = T0 + timedelta(days=1, minutes=1)  # closes 09:31 ET next day
    snap.on_bar(_bar("$ADD", next_open, 500.0))

    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 1
    assert st.recent_bars[0].close == 500.0
    assert st.session_date == date(2026, 4, 21)
    assert st.session_open_bar is not None
    assert st.session_open_bar.bar_time == next_open


def test_session_reset_within_same_session_keeps_history() -> None:
    # RTH bars only (09:31 onward) — a `session_reset` symbol clears once at
    # the open, and nothing after it, which is what this asserts.
    snap = MarketSnapshot(symbol_policies={"$ADD": _BREADTH_POLICY})
    for i in range(1, 6):
        snap.on_bar(_bar("$ADD", T0 + timedelta(minutes=i), 100.0 + i))
    st = snap.state_of("$ADD")
    assert st is not None
    assert len(st.recent_bars) == 5


def test_breadth_drops_pre_market_history_when_the_session_opens() -> None:
    """A breadth symbol keeps no pre-market bars once RTH starts.

    This pins the 04:00-vs-09:30 distinction, which the session-boundary test
    above cannot see because its two bars are a whole day apart.
    ``session_date_of()`` rolls at 04:00 ET, so a reset keyed on the session
    date alone leaves every pre-market bar sitting in the deque after the open
    — and because the eviction path is skipped for ``session_reset`` symbols,
    breadth would retain MORE pre-market history than SPY does, inverting the
    reason the policy exists. contract/semantics.md §4.1 requires none of it to
    survive.
    """
    snap = MarketSnapshot(symbol_policies={"$TICK": _BREADTH_POLICY})
    snap.on_bar(_bar("$TICK", T0 - timedelta(hours=5), 100.0))  # 04:30 ET
    snap.on_bar(_bar("$TICK", T0 - timedelta(minutes=30), 101.0))  # 09:00 ET

    st = snap.state_of("$TICK")
    assert st is not None
    # Before the open, pre-market bars are all there is — they accumulate.
    assert [b.close for b in st.recent_bars] == [100.0, 101.0]

    snap.on_bar(_bar("$TICK", T0 + timedelta(minutes=1), 102.0))  # first RTH bar

    st = snap.state_of("$TICK")
    assert st is not None
    assert [b.close for b in st.recent_bars] == [102.0]


def test_breadth_bar_closing_exactly_at_the_open_is_pre_market() -> None:
    """``bar_time`` is a CLOSE, so a 09:30 close is the LAST pre-market bar.

    Same convention ``session_open_bar`` uses (strictly greater than the open),
    so it must be dropped with the rest rather than surviving as the session's
    first bar.
    """
    snap = MarketSnapshot(symbol_policies={"$ADD": _BREADTH_POLICY})
    snap.on_bar(_bar("$ADD", T0, 100.0))  # closes 09:30 ET — pre-market
    snap.on_bar(_bar("$ADD", T0 + timedelta(minutes=1), 101.0))  # closes 09:31 — RTH

    st = snap.state_of("$ADD")
    assert st is not None
    assert [b.close for b in st.recent_bars] == [101.0]


def test_breadth_open_reset_applies_to_every_breadth_symbol() -> None:
    """The policy is keyed on category, not on a hard-coded symbol list.

    $TICK / $ADD / $VOLD / $TRIN / $PCVA are all `category: breadth` in
    symbols.yaml, so one rule covers them and a new breadth symbol inherits it
    without an edit here.
    """
    breadth = ["$TICK", "$ADD", "$VOLD", "$TRIN", "$PCVA"]
    snap = MarketSnapshot(symbol_policies=dict.fromkeys(breadth, _BREADTH_POLICY))
    for sym in breadth:
        snap.on_bar(_bar(sym, T0 - timedelta(hours=4), 1.0))  # 05:30 ET
        snap.on_bar(_bar(sym, T0 + timedelta(minutes=1), 2.0))  # first RTH bar

    for sym in breadth:
        st = snap.state_of(sym)
        assert st is not None, sym
        assert [b.close for b in st.recent_bars] == [2.0], sym


def test_continuous_policy_still_keeps_its_pre_market_window() -> None:
    """The open-crossing clear must not leak onto non-breadth symbols.

    SPY keeps 60 minutes of pre-market by design; only `session_reset` symbols
    lose theirs at the open.
    """
    snap = MarketSnapshot(symbol_policies={"SPY": _CONTINUOUS_POLICY})
    snap.on_bar(_bar("SPY", T0 - timedelta(minutes=45), 449.0))  # 08:45 ET
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=1), 450.0))  # first RTH bar

    st = snap.state_of("SPY")
    assert st is not None
    assert [b.close for b in st.recent_bars] == [449.0, 450.0]


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
    # Closes exactly 09:30 ET — the LAST pre-market minute, not the open.
    snap.on_bar(_bar("SPY", T0, 449.5))
    # Closes 09:31 ET — the first bar of the regular session.
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=1), 450.0))
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=5), 451.0))

    st = snap.state_of("SPY")
    assert st is not None
    assert st.session_open_bar is not None
    assert st.session_open_bar.bar_time == T0 + timedelta(minutes=1)
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
    # Closes 09:31 ET — a close of exactly 09:30 would be pre-market.
    snap.on_bar(_bar("SPY", T0 + timedelta(minutes=1), 450.0))

    view = snap.view_of("SPY")
    assert view is not None
    assert view.session_date == date(2026, 4, 20)
    assert view.session_open_bar is not None
    assert view.session_open_bar.open == 450.0
