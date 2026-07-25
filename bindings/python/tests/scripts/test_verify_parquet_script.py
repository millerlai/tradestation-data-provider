from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import verify_parquet as vp


def test_parse_timeframe_minute():
    assert vp._parse_timeframe("1m") == ("1m", 60)
    assert vp._parse_timeframe("5m") == ("5m", 300)


def test_parse_timeframe_second_and_hour():
    assert vp._parse_timeframe("30s") == ("30s", 30)
    assert vp._parse_timeframe("1h") == ("1h", 3600)


def test_parse_timeframe_invalid():
    with pytest.raises(ValueError):
        vp._parse_timeframe("5x")
    with pytest.raises(ValueError):
        vp._parse_timeframe("abc")
    with pytest.raises(ValueError):
        vp._parse_timeframe("-5m")


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


def test_expected_bars_1m_full_session():
    tz = vp._resolve_tz("ET")
    bars = vp._expected_bars(date(2026, 4, 17), time(9, 30), time(16, 0), 60, tz)
    # 09:30..15:59 inclusive left-labeled (bucket_start) = 390 bars
    assert len(bars) == 390
    assert bars[0].astimezone(tz).strftime("%H:%M") == "09:30"
    assert bars[-1].astimezone(tz).strftime("%H:%M") == "15:59"


def test_expected_bars_5m_session():
    tz = vp._resolve_tz("ET")
    bars = vp._expected_bars(date(2026, 4, 17), time(9, 30), time(16, 0), 300, tz)
    assert len(bars) == 78  # 6.5h * 12


def test_cluster_empty():
    assert vp._cluster([], 60) == []


def test_cluster_contiguous_and_gaps():
    base = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    missing = [
        base,
        base + timedelta(minutes=1),
        base + timedelta(minutes=2),
        base + timedelta(minutes=10),  # gap
        base + timedelta(minutes=11),
    ]
    runs = vp._cluster(missing, 60)
    assert len(runs) == 2
    assert runs[0][2] == 3
    assert runs[1][2] == 2


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


def _write_day_bars(root: Path, symbol: str, day: date, bucket_starts_utc):
    p = root / "timeframe=1m" / f"symbol={symbol}" / f"date={day.isoformat()}" / "bars.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "bucket_start": pa.array(bucket_starts_utc, type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, p, compression="zstd")


def test_verify_end_to_end(tmp_path):
    tz = vp._resolve_tz("ET")
    day = date(2026, 4, 17)  # Friday
    expected = vp._expected_bars(day, time(9, 30), time(10, 0), 60, tz)
    # Write all but last bar -> INCOMPLETE with 1 missing
    _write_day_bars(tmp_path, "SPY", day, expected[:-1])

    reports = vp.verify(
        root=tmp_path,
        symbol="SPY",
        start_date=day,
        end_date=day + timedelta(days=3),  # covers Fri, Sat, Sun, Mon
        tf_label="1m",
        tf_sec=60,
        start_time=time(9, 30),
        end_time=time(10, 0),
        tz=tz,
        holidays=set(),
        include_weekends=False,
    )

    by_status = {r.status for r in reports}
    # Fri INCOMPLETE, Sat+Sun WEEKEND, Mon FILE_MISSING
    assert "INCOMPLETE" in by_status
    assert "WEEKEND" in by_status
    assert "FILE_MISSING" in by_status

    fri = next(r for r in reports if r.day == day)
    assert fri.status == "INCOMPLETE"
    assert len(fri.missing) == 1
    assert fri.rows == len(expected) - 1
