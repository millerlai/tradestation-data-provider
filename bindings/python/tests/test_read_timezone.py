"""Read-side timezone semantics — contract/semantics.md §2.4.

This is a US-equity API: sessions, holidays, and `date=` partitions are all
defined in `America/New_York`. A naive datetime therefore means ET. UTC is the
internal absolute time, not something a caller should have to convert to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.storage import BarWriter, HistoryStore, TickWriter

ET = ZoneInfo("America/New_York")
OPEN_ET = datetime(2026, 4, 20, 9, 30, tzinfo=ET)  # 13:30 UTC


def _store(tmp_path: Path, n: int = 30) -> HistoryStore:
    """A store holding both series the read API exposes.

    The 5m bars are written, not derived: nothing in this binding computes a
    bar, so a query for an interval nobody published answers zero rows and the
    timezone assertions below would pass vacuously.
    """
    with TickWriter(tmp_path / "ticks") as tw:
        for i in range(n):
            tw.write(
                Tick(
                    symbol="SPY",
                    timestamp=OPEN_ET.astimezone(UTC) + timedelta(minutes=i),
                    price=450.0 + i,
                    el_volume=100,
                    el_ticks=200,
                    el_upticks=100,
                    el_downticks=100,
                    el_open_interest=0,
                    bid=None,
                    ask=None,
                )
            )
    with BarWriter(tmp_path / "bars") as bw:
        for i in range(0, n, 5):
            bw.write(
                Bar(
                    symbol="SPY",
                    bucket_start=OPEN_ET.astimezone(UTC) + timedelta(minutes=i),
                    open=450.0 + i,
                    high=450.0 + i + 4,
                    low=450.0 + i,
                    close=450.0 + i + 4,
                    el_volume=500,
                    el_ticks=1000,
                    el_upticks=500,
                    el_downticks=500,
                    el_open_interest=0,
                    timeframe="5m",
                )
            )
    return HistoryStore(tmp_path)


def test_naive_datetimes_are_eastern_not_utc(tmp_path: Path) -> None:
    """`datetime(2026, 4, 20, 9, 30)` is the open, not 05:30 ET.

    Interpreting it as UTC was never a decision — it leaked out of the query
    engine's `SET TimeZone='UTC'` and silently shifted every naive query by
    four or five hours.
    """
    store = _store(tmp_path)
    naive = store.load_bars("SPY", datetime(2026, 4, 20, 9, 30), datetime(2026, 4, 20, 10, 0), "5m")
    aware = store.load_bars("SPY", OPEN_ET, OPEN_ET + timedelta(minutes=30), "5m")

    assert naive.height > 0, "a naive query for the open must not answer silence"
    assert naive["bucket_start"].to_list() == aware["bucket_start"].to_list()
    assert naive["bucket_start_et"].dt.hour().to_list()[0] == 9


def test_aware_datetimes_keep_their_own_zone(tmp_path: Path) -> None:
    """An aware input is unambiguous, so the ET default must not touch it."""
    store = _store(tmp_path)
    in_utc = store.load_bars(
        "SPY", OPEN_ET.astimezone(UTC), (OPEN_ET + timedelta(minutes=30)).astimezone(UTC), "5m"
    )
    in_et = store.load_bars("SPY", OPEN_ET, OPEN_ET + timedelta(minutes=30), "5m")
    assert in_utc["bucket_start"].to_list() == in_et["bucket_start"].to_list()


def test_naive_ticks_query_is_eastern_too(tmp_path: Path) -> None:
    """`load_ticks` is the same API surface and must not disagree."""
    store = _store(tmp_path)
    naive = store.load_ticks("SPY", datetime(2026, 4, 20, 9, 30), datetime(2026, 4, 20, 10, 0))
    aware = store.load_ticks("SPY", OPEN_ET, OPEN_ET + timedelta(minutes=30))
    assert naive.height == aware.height > 0
    assert naive["timestamp_et"].dt.hour().to_list()[0] == 9
