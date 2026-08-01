from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tradestation_data.domain import Bar, Tick

_ET = ZoneInfo("America/New_York")


def test_tick_is_frozen_and_equatable() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    a = Tick("SPY", ts, 450.23, 100, 812, 100, 712, 0, 450.22, 450.24)
    b = Tick("SPY", ts, 450.23, 100, 812, 100, 712, 0, 450.22, 450.24)
    assert a == b
    with pytest.raises(FrozenInstanceError):
        a.price = 451.0  # type: ignore[misc]


def test_tick_allows_none_quote_for_index_symbols() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    vix = Tick("VXX", ts, 18.5, 0, 0, 0, 0, 0, None, None)
    assert vix.bid is None
    assert vix.ask is None
    assert vix.el_volume == 0


def test_bar_carries_all_five_el_quantities_verbatim() -> None:
    ts = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    bar = Bar(
        symbol="VXX",
        bucket_start=ts,
        open=18.5,
        high=18.7,
        low=18.4,
        close=18.6,
        el_volume=0,
        el_ticks=0,
        el_upticks=0,
        el_downticks=0,
        el_open_interest=0,
    )
    assert (bar.el_volume, bar.el_ticks) == (0, 0)
    assert (bar.el_upticks, bar.el_downticks, bar.el_open_interest) == (0, 0, 0)
    # Nothing selects between the quantity words, so an intraday el_volume
    # stays the up-tick half it came off the wire as.
    intraday = Bar("SPY", ts, 450.1, 450.8, 449.8, 450.4, 6100, 12000, 6100, 5900, 0)
    assert intraday.el_volume == 6100
    assert intraday.el_ticks == 12000


def test_tick_timestamp_et_returns_et_view() -> None:
    # 13:30 UTC on 2026-04-18 is 09:30 EDT (DST in effect)
    utc = datetime(2026, 4, 18, 13, 30, tzinfo=UTC)
    tick = Tick("SPY", utc, 450.0, 100, 200, 100, 100, 0, 449.99, 450.01)
    et = tick.timestamp_et
    assert et.tzinfo is not None
    assert et.utcoffset() == utc.astimezone(_ET).utcoffset()
    assert et.hour == 9 and et.minute == 30
    # Same instant, different zone.
    assert et == utc


def test_tick_timestamp_et_handles_est_vs_edt() -> None:
    # 2026-02-15 is outside DST (EST, UTC-5); 14:30 UTC = 09:30 EST.
    est_utc = datetime(2026, 2, 15, 14, 30, tzinfo=UTC)
    tick_est = Tick("SPY", est_utc, 1.0, 0, 0, 0, 0, 0, None, None)
    assert tick_est.timestamp_et.hour == 9 and tick_est.timestamp_et.minute == 30

    # 2026-07-15 is inside DST (EDT, UTC-4); 13:30 UTC = 09:30 EDT.
    edt_utc = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    tick_edt = Tick("SPY", edt_utc, 1.0, 0, 0, 0, 0, 0, None, None)
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
        el_volume=1000,
        el_ticks=2000,
        el_upticks=1000,
        el_downticks=1000,
        el_open_interest=0,
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
    bar_edt = Bar("SPY", edt, 1, 1, 1, 1, 0, 0, 0, 0, 0)
    bar_est = Bar("SPY", est, 1, 1, 1, 1, 0, 0, 0, 0, 0)
    assert bar_edt.bucket_start_et.hour == 1
    assert bar_est.bucket_start_et.hour == 1
    # UTC instants differ even though ET wall-clock reads the same.
    assert bar_edt.bucket_start != bar_est.bucket_start
