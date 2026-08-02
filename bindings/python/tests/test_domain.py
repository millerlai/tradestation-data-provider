from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tradestation_data.domain import Bar

_ET = ZoneInfo("America/New_York")


def test_bar_carries_all_five_el_quantities_verbatim() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    bar = Bar(
        symbol="VXX",
        bar_time=ts,
        open=18.5,
        high=18.7,
        low=18.4,
        close=18.6,
        el_volume=0,
        el_ticks=0,
        el_upticks=0,
        el_downticks=0,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )
    assert (bar.el_volume, bar.el_ticks) == (0, 0)
    assert (bar.el_upticks, bar.el_downticks, bar.el_open_interest) == (0, 0, 0)
    # Nothing selects between the quantity words, so an intraday el_volume
    # stays the up-tick half it came off the wire as.
    intraday = Bar(
        symbol="SPY",
        bar_time=ts,
        bar_type=1,
        bar_interval=1,
        category=2,
        open=450.1,
        high=450.8,
        low=449.8,
        close=450.4,
        el_volume=6100,
        el_ticks=12000,
        el_upticks=6100,
        el_downticks=5900,
        el_open_interest=0,
    )
    assert intraday.el_volume == 6100
    assert intraday.el_ticks == 12000


def test_bar_bar_time_et_returns_et_view() -> None:
    utc = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    bar = Bar(
        symbol="SPY",
        bar_time=utc,
        open=450.0,
        high=450.5,
        low=449.5,
        close=450.2,
        el_volume=1000,
        el_ticks=2000,
        el_upticks=1000,
        el_downticks=1000,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )
    et = bar.bar_time_et
    assert et == utc
    assert et.hour == 9 and et.minute == 30
    # Property is derived, not a stored field — mutating bar_time is
    # impossible (frozen), so the ET view is always consistent.


def test_bar_bar_time_et_dst_fallback_preserves_distinct_instants() -> None:
    # 2026 DST ends 2026-11-01; the 01:xx ET hour repeats.
    # 01:30 EDT = 05:30 UTC, 01:30 EST = 06:30 UTC — two distinct instants.
    edt = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    est = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    bar_edt = Bar("SPY", edt, 1, 1, 2, 1.0, 1.0, 1.0, 1.0, 0, 0, 0, 0, 0)
    bar_est = Bar("SPY", est, 1, 1, 2, 1.0, 1.0, 1.0, 1.0, 0, 0, 0, 0, 0)
    assert bar_edt.bar_time_et.hour == 1
    assert bar_est.bar_time_et.hour == 1
    # UTC instants differ even though ET wall-clock reads the same.
    assert bar_edt.bar_time != bar_est.bar_time
