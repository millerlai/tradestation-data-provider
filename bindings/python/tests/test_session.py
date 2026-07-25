"""Tests for aggregation.session — trading-session boundary helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from tradestation_data.aggregation.session import (
    NY,
    SessionPolicy,
    session_date_of,
    session_start_utc,
)

# ---- session_date_of ----------------------------------------------------


def test_session_date_of_daytime_et_is_same_date() -> None:
    # 10:30 ET on 2026-04-20
    ts = datetime(2026, 4, 20, 14, 30, tzinfo=UTC)
    assert session_date_of(ts) == date(2026, 4, 20)


def test_session_date_of_premarket_after_4am_is_same_date() -> None:
    # 05:00 ET on 2026-04-20 (pre-market session starts at 04:00 ET)
    ts = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    assert session_date_of(ts) == date(2026, 4, 20)


def test_session_date_of_overnight_before_4am_belongs_to_previous_day() -> None:
    # 02:00 ET on 2026-04-20 → previous session date 2026-04-19
    ts = datetime(2026, 4, 20, 6, 0, tzinfo=UTC)
    assert session_date_of(ts) == date(2026, 4, 19)


def test_session_date_of_accepts_non_utc_tz() -> None:
    # Same instant expressed in NY time
    ts_ny = datetime(2026, 4, 20, 10, 30, tzinfo=NY)
    assert session_date_of(ts_ny) == date(2026, 4, 20)


# ---- session_start_utc --------------------------------------------------


def test_session_start_utc_edt_april() -> None:
    # April → EDT (UTC-4) → 09:30 ET == 13:30 UTC
    start = session_start_utc(date(2026, 4, 20))
    assert start == datetime(2026, 4, 20, 13, 30, tzinfo=ZoneInfo("UTC"))


def test_session_start_utc_est_january() -> None:
    # January → EST (UTC-5) → 09:30 ET == 14:30 UTC
    start = session_start_utc(date(2026, 1, 15))
    assert start == datetime(2026, 1, 15, 14, 30, tzinfo=ZoneInfo("UTC"))


def test_session_start_utc_round_trip_via_session_date_of() -> None:
    d = date(2026, 4, 20)
    start = session_start_utc(d)
    # The session_date of the open itself is the same day
    assert session_date_of(start) == d
    # 1 minute before 09:30 ET still belongs to the same session (pre-market)
    assert session_date_of(start - timedelta(minutes=1)) == d


# ---- SessionPolicy.for_category ----------------------------------------


def test_policy_for_breadth_resets() -> None:
    p = SessionPolicy.for_category("breadth")
    assert p.session_reset is True
    assert p.pre_market_window_minutes is None


def test_policy_for_etf_keeps_premarket_window() -> None:
    p = SessionPolicy.for_category("etf")
    assert p.session_reset is False
    assert p.pre_market_window_minutes == 60


def test_policy_for_unknown_category_defaults_to_etf_like() -> None:
    p = SessionPolicy.for_category("something_else")
    assert p.session_reset is False
    assert p.pre_market_window_minutes == 60
