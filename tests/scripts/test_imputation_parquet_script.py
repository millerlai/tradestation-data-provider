from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import imputation_parquet as ip
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import verify_parquet as vp

BAR_SCHEMA = pa.schema(
    [
        pa.field("bucket_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.int64(), nullable=False),
        pa.field("vwap", pa.float64(), nullable=True),
        pa.field("tick_count", pa.int32(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)


def _row(ts, close, open_=None, volume=1000):
    return {
        "bucket_start": ts,
        "open": open_ if open_ is not None else close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "vwap": close,
        "tick_count": 5,
        "source": "live",
    }


def test_build_imputed_row_structure():
    ts = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    row = ip._build_imputed_row(ts, 123.45, "imputed_ffill")
    assert row["open"] == row["high"] == row["low"] == row["close"] == 123.45
    assert row["volume"] == 0
    assert row["tick_count"] == 0
    assert row["source"] == "imputed_ffill"
    assert row["vwap"] is None


def test_impute_value_ffill_uses_prev():
    prev = _row(datetime(2026, 4, 18, 13, 31, tzinfo=UTC), 100.0)
    nxt = _row(datetime(2026, 4, 18, 13, 33, tzinfo=UTC), 102.0)
    missing = datetime(2026, 4, 18, 13, 32, tzinfo=UTC)
    val, note = ip._impute_value(missing, prev, nxt, "ffill")
    assert val == 100.0
    assert note == ""


def test_impute_value_ffill_falls_back_to_next():
    nxt = _row(datetime(2026, 4, 18, 13, 33, tzinfo=UTC), 102.0, open_=101.5)
    val, note = ip._impute_value(datetime(2026, 4, 18, 13, 32, tzinfo=UTC), None, nxt, "ffill")
    assert val == 101.5
    assert "bfill" in note


def test_impute_value_bfill_uses_next():
    prev = _row(datetime(2026, 4, 18, 13, 31, tzinfo=UTC), 100.0)
    nxt = _row(datetime(2026, 4, 18, 13, 33, tzinfo=UTC), 102.0, open_=101.5)
    val, note = ip._impute_value(datetime(2026, 4, 18, 13, 32, tzinfo=UTC), prev, nxt, "bfill")
    assert val == 101.5
    assert note == ""


def test_impute_value_interpolate_midpoint():
    prev = _row(datetime(2026, 4, 18, 13, 31, tzinfo=UTC), 100.0)
    nxt = _row(datetime(2026, 4, 18, 13, 33, tzinfo=UTC), 102.0, open_=104.0)
    val, note = ip._impute_value(
        datetime(2026, 4, 18, 13, 32, tzinfo=UTC), prev, nxt, "interpolate"
    )
    # linear from prev.close (100) to nxt.open (104) at 50% -> 102
    assert val == pytest.approx(102.0)
    assert note == ""


def test_impute_value_no_reference_returns_none():
    res = ip._impute_value(
        datetime(2026, 4, 18, 13, 32, tzinfo=UTC),
        None,
        None,
        "ffill",
    )
    assert res is None


def test_impute_value_unknown_method_raises():
    with pytest.raises(ValueError):
        ip._impute_value(datetime(2026, 4, 18, 13, 32, tzinfo=UTC), None, None, "magic")


def _write_bars(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=BAR_SCHEMA)
    pq.write_table(table, path, compression="zstd")


def test_impute_day_fills_one_gap(tmp_path):
    path = tmp_path / "bars.parquet"
    t0 = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    rows = [
        _row(t0, 100.0),
        _row(t0 + timedelta(minutes=1), 101.0),
        # gap at t0+2
        _row(t0 + timedelta(minutes=3), 103.0),
    ]
    _write_bars(path, rows)

    tz = vp._resolve_tz("UTC")
    expected = [t0 + timedelta(minutes=i) for i in range(4)]

    before, added, _log, new_table = ip.impute_day(path, expected, "ffill", "imputed_ffill", 60, tz)
    assert before == 3
    assert added == 1
    assert new_table is not None
    assert new_table.num_rows == 4

    sources = new_table.column("source").to_pylist()
    assert sources.count("imputed_ffill") == 1


def test_impute_day_no_gap_returns_none(tmp_path):
    path = tmp_path / "bars.parquet"
    t0 = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    rows = [_row(t0 + timedelta(minutes=i), 100.0 + i) for i in range(3)]
    _write_bars(path, rows)

    tz = vp._resolve_tz("UTC")
    expected = [t0 + timedelta(minutes=i) for i in range(3)]
    before, added, log, new_table = ip.impute_day(path, expected, "ffill", "imputed_ffill", 60, tz)
    assert before == 3
    assert added == 0
    assert new_table is None
    assert log == []


def test_write_atomic_replaces_file(tmp_path):
    path = tmp_path / "bars.parquet"
    rows = [_row(datetime(2026, 4, 18, 13, 31, tzinfo=UTC), 100.0)]
    _write_bars(path, rows)

    # New content with 2 rows
    new_rows = [*rows, _row(datetime(2026, 4, 18, 13, 32, tzinfo=UTC), 101.0)]
    table = pa.Table.from_pylist(new_rows, schema=BAR_SCHEMA)

    ip._write_atomic(path, table)
    assert pq.read_table(path).num_rows == 2
    # No .tmp left behind
    assert not path.with_suffix(path.suffix + ".tmp").exists()
