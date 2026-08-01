from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import imputation_parquet as ip
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import verify_parquet as vp

_ET = ZoneInfo("America/New_York")

# The shape BarWriter puts on disk. The script reads real partitions, so the
# fixture has to be one — including bucket_start_et, which the output schema
# carries through untouched.
BAR_SCHEMA = pa.schema(
    [
        pa.field("bucket_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("bucket_start_et", pa.timestamp("us", tz="America/New_York"), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("el_volume", pa.int64(), nullable=False),
        pa.field("el_ticks", pa.int64(), nullable=False),
        pa.field("el_upticks", pa.int64(), nullable=False),
        pa.field("el_downticks", pa.int64(), nullable=False),
        pa.field("el_open_interest", pa.int64(), nullable=False),
    ]
)


def _row(ts, close, open_=None, el_volume=1000):
    return {
        "bucket_start": ts,
        "bucket_start_et": ts.astimezone(_ET),
        "open": open_ if open_ is not None else close,
        "high": close,
        "low": close,
        "close": close,
        "el_volume": el_volume,
        "el_ticks": el_volume * 2,
        "el_upticks": el_volume,
        "el_downticks": el_volume,
        "el_open_interest": 0,
    }


def test_build_imputed_row_structure():
    ts = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    row = ip._build_imputed_row(ts, 123.45)
    assert row["open"] == row["high"] == row["low"] == row["close"] == 123.45
    # No trading was observed, so none is recorded. Carrying a neighbour's
    # volume forward would invent activity on top of inventing a price.
    for q in ("el_volume", "el_ticks", "el_upticks", "el_downticks", "el_open_interest"):
        assert row[q] == 0, q
    assert row["imputed"] is True
    # Provenance is a column of its own now, not a string smuggled into a
    # field that also names real publishers.
    assert "source" not in row


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

    before, added, _log, new_table = ip.impute_day(path, expected, "ffill", 60, tz)
    assert before == 3
    assert added == 1
    assert new_table is not None
    assert new_table.num_rows == 4

    # Which rows came off the wire is a boolean column, and the three that
    # did are flagged False rather than left null — "real" versus "invented"
    # is never a missing-value question.
    assert new_table.column("imputed").to_pylist() == [False, False, True, False]


def test_impute_day_does_not_touch_the_input_file(tmp_path):
    """Imputation is non-destructive: the source partition is only read.

    Rewriting in place is unrecoverable if a run is interrupted, and nothing
    on disk would record which rows had been invented.
    """
    path = tmp_path / "bars.parquet"
    t0 = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    _write_bars(path, [_row(t0, 100.0), _row(t0 + timedelta(minutes=2), 102.0)])
    before_bytes = path.read_bytes()

    tz = vp._resolve_tz("UTC")
    expected = [t0 + timedelta(minutes=i) for i in range(3)]
    _before, added, _log, new_table = ip.impute_day(path, expected, "ffill", 60, tz)

    assert added == 1
    assert new_table is not None
    assert path.read_bytes() == before_bytes
    assert "imputed" not in pq.read_table(path).column_names


def test_impute_day_no_gap_returns_none(tmp_path):
    path = tmp_path / "bars.parquet"
    t0 = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    rows = [_row(t0 + timedelta(minutes=i), 100.0 + i) for i in range(3)]
    _write_bars(path, rows)

    tz = vp._resolve_tz("UTC")
    expected = [t0 + timedelta(minutes=i) for i in range(3)]
    before, added, log, new_table = ip.impute_day(path, expected, "ffill", 60, tz)
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
