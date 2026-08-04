from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import verify_parquet as vp

# What a 60-minute chart on a 06:00-20:00 session really publishes: fifteen
# bars a day, two of them stubs, because TradeStation restarts its intraday
# grid at the RTH open and close (contract/semantics.md §2). A uniform hourly
# grid expects 10:00/11:00/12:00/13:00 — positions that never arrive — and
# calls the 09:30 and 16:00 stubs "extra".
_H1_SESSION_CLOSES = [
    (7, 0),
    (8, 0),
    (9, 0),
    (9, 30),
    (10, 30),
    (11, 30),
    (12, 30),
    (13, 30),
    (14, 30),
    (15, 30),
    (16, 0),
    (17, 0),
    (18, 0),
    (19, 0),
    (20, 0),
]


def test_parse_hhmm_variants():
    assert vp._parse_hhmm("09:30") == time(9, 30)
    assert vp._parse_hhmm("16:00:30") == time(16, 0, 30)
    with pytest.raises(ValueError):
        vp._parse_hhmm("9h30m")


def test_parse_date():
    assert vp._parse_date("2026-04-18") == date(2026, 4, 18)
    with pytest.raises(ValueError):
        vp._parse_date("not-a-date")


def test_resolve_tz_aliases_and_unknown():
    tz = vp._resolve_tz("ET")
    assert "New_York" in str(tz)
    with pytest.raises(ValueError):
        vp._resolve_tz("Mars/Olympus")


def test_expected_bars_places_the_profile_on_one_day():
    tz = vp._resolve_tz("ET")
    bars = vp._expected_bars(date(2026, 4, 17), [time(9, 31), time(16, 0)], tz)
    assert [b.astimezone(tz).strftime("%H:%M") for b in bars] == ["09:31", "16:00"]
    assert all(b.utcoffset() == timedelta(0) for b in bars)


def test_cluster_empty():
    assert vp._cluster([], []) == []


def test_cluster_uses_position_in_expected_not_a_fixed_step():
    """Adjacent in the series, not "one interval apart".

    A stub bar puts two consecutive expected bars half an interval apart. On
    the old fixed-step rule that read as a break and split one gap into three.
    """
    tz = vp._resolve_tz("ET")
    expected = vp._expected_bars(date(2026, 4, 17), [time(h, m) for h, m in _H1_SESSION_CLOSES], tz)

    # 09:00, 09:30, 10:30 — consecutive in the series, 30 and 60 minutes apart.
    runs = vp._cluster(expected[2:5], expected)
    assert len(runs) == 1
    assert runs[0][2] == 3

    # A real break in the series still splits.
    runs = vp._cluster([expected[2], expected[8]], expected)
    assert len(runs) == 2


def test_fmt_range_truncates_with_more():
    tz = vp._resolve_tz("UTC")
    base = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    runs = [
        (base + timedelta(minutes=5 * i), base + timedelta(minutes=5 * i + 1), 2) for i in range(5)
    ]
    s = vp._fmt_range(runs, tz, max_shown=2)
    assert "+3 more" in s


def test_fmt_range_empty():
    assert vp._fmt_range([], vp._resolve_tz("UTC"), 3) == ""


def _write_day_bars(root: Path, symbol: str, day: date, bar_times_utc, interval: int = 1):
    p = (
        root
        / "bartype=1"
        / f"interval={interval}"
        / f"symbol={symbol}"
        / f"date={day.isoformat()}"
        / "bars.parquet"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "bar_time": pa.array(bar_times_utc, type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, p, compression="zstd")


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _session_day(day: date, tz, closes=None) -> list[datetime]:
    closes = closes if closes is not None else _H1_SESSION_CLOSES
    return [datetime.combine(day, time(h, m), tzinfo=tz).astimezone(UTC) for h, m in closes]


def _profile(root: Path, days: list[date], tz, *, end=time(20, 0), min_days: int = 5):
    return vp._observed_profile(
        root=root,
        bar_type=1,
        bar_interval=60,
        symbol="SPY",
        start_date=days[0],
        end_date=days[-1],
        start=time(6, 0),
        end=end,
        tz=tz,
        holidays=set(),
        include_weekends=False,
        min_days=min_days,
    )


def test_profile_learns_the_bars_tradestation_actually_publishes(tmp_path):
    tz = vp._resolve_tz("ET")
    days = _weekdays(date(2026, 4, 6), 10)
    for d in days:
        _write_day_bars(tmp_path, "SPY", d, _session_day(d, tz), interval=60)

    profile, n = _profile(tmp_path, days, tz)

    assert n == 10
    assert profile == [time(h, m) for h, m in _H1_SESSION_CLOSES]
    # The stubs the session restart produces are expected...
    assert time(9, 30) in profile
    assert time(16, 0) in profile
    # ...and the uniform-grid positions that never arrive are not. That pair is
    # the whole defect: on a grid these read MISSING every single day, and
    # imputation_parquet.py wrote invented OHLC rows at them.
    for never_published in (time(10, 0), time(11, 0), time(12, 0), time(13, 0)):
        assert never_published not in profile


def test_profile_ignores_a_minority_oddity(tmp_path):
    """One odd day must not teach the profile a bar the series does not have."""
    tz = vp._resolve_tz("ET")
    days = _weekdays(date(2026, 4, 6), 10)
    for i, d in enumerate(days):
        closes = [*_H1_SESSION_CLOSES, (20, 30)] if i == 0 else _H1_SESSION_CLOSES
        _write_day_bars(tmp_path, "SPY", d, _session_day(d, tz, closes), interval=60)

    profile, _n = _profile(tmp_path, days, tz, end=time(21, 0))

    assert time(20, 30) not in profile
    assert time(20, 0) in profile


def test_profile_refuses_when_there_is_too_little_history(tmp_path):
    """A profile learned from one day says only that the day matches itself."""
    tz = vp._resolve_tz("ET")
    days = _weekdays(date(2026, 4, 6), 2)
    for d in days:
        _write_day_bars(tmp_path, "SPY", d, _session_day(d, tz), interval=60)

    with pytest.raises(ValueError, match="min-reference-days"):
        _profile(tmp_path, days, tz)


def test_verify_end_to_end(tmp_path):
    tz = vp._resolve_tz("ET")
    days = _weekdays(date(2026, 4, 6), 10)
    for d in days[:-1]:
        _write_day_bars(tmp_path, "SPY", d, _session_day(d, tz), interval=60)
    # The last day loses its closing bar.
    last = days[-1]
    _write_day_bars(tmp_path, "SPY", last, _session_day(last, tz)[:-1], interval=60)

    profile, _n = _profile(tmp_path, days, tz)
    reports = vp.verify(
        root=tmp_path,
        symbol="SPY",
        start_date=days[0],
        end_date=last + timedelta(days=5),
        bar_type=1,
        bar_interval=60,
        profile=profile,
        tz=tz,
        holidays=set(),
        include_weekends=False,
    )

    by_day = {r.day: r for r in reports}
    first = by_day[days[0]]
    assert first.status == "OK"
    # No "extra bars outside session": the two stubs are part of the profile.
    assert first.note == ""
    assert by_day[last].status == "INCOMPLETE"
    assert len(by_day[last].missing) == 1

    statuses = {r.status for r in reports}
    assert "WEEKEND" in statuses
    assert "FILE_MISSING" in statuses
