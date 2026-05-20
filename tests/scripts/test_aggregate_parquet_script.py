from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aggregate_parquet as agg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def test_tf_minutes_valid():
    assert agg._tf_minutes("5m") == 5
    assert agg._tf_minutes("1h") == 60


def test_tf_minutes_invalid():
    with pytest.raises(ValueError):
        agg._tf_minutes("2m")


def test_detect_input_timeframe(tmp_path):
    assert agg._detect_input_timeframe(tmp_path / "timeframe=5m") == "5m"
    assert agg._detect_input_timeframe(tmp_path / "bars") == "1m"


def test_chunk_label_5m_boundary():
    # 09:31..09:35 -> 09:35; 09:36..09:40 -> 09:40
    t = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)  # = 09:31 ET DST
    assert agg._chunk_label(t, 5) == datetime(2026, 4, 18, 13, 35, tzinfo=UTC)
    t = datetime(2026, 4, 18, 13, 35, tzinfo=UTC)
    assert agg._chunk_label(t, 5) == datetime(2026, 4, 18, 13, 35, tzinfo=UTC)
    t = datetime(2026, 4, 18, 13, 36, tzinfo=UTC)
    assert agg._chunk_label(t, 5) == datetime(2026, 4, 18, 13, 40, tzinfo=UTC)


def test_iter_symbol_dirs_all_skips_dollar(tmp_path):
    (tmp_path / "symbol=SPY").mkdir()
    (tmp_path / "symbol=$TICK").mkdir()
    (tmp_path / "symbol=QQQ").mkdir()
    got = [p.name for p in agg._iter_symbol_dirs(tmp_path, "all")]
    assert got == ["symbol=QQQ", "symbol=SPY"]


def test_iter_symbol_dirs_specific_symbol(tmp_path):
    (tmp_path / "symbol=SPY").mkdir()
    got = agg._iter_symbol_dirs(tmp_path, "SPY")
    assert got == [tmp_path / "symbol=SPY"]

    assert agg._iter_symbol_dirs(tmp_path, "NOPE") == []


def test_iter_date_files(tmp_path):
    sdir = tmp_path / "symbol=SPY"
    for d in ("2026-04-16", "2026-04-17", "2026-04-18"):
        (sdir / f"date={d}").mkdir(parents=True)
        (sdir / f"date={d}" / "bars.parquet").write_bytes(b"")
    all_files = agg._iter_date_files(sdir, None)
    assert [f.parent.name for f in all_files] == [
        "date=2026-04-16",
        "date=2026-04-17",
        "date=2026-04-18",
    ]
    filtered = agg._iter_date_files(sdir, "2026-04-17")
    assert len(filtered) == 1 and filtered[0].parent.name == "date=2026-04-17"


def _make_1m_day(path: Path, n_minutes: int = 5):
    """Write n_minutes 1-min bars starting at 09:31 ET (= 13:31 UTC DST)."""
    start = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    rows = []
    for i in range(n_minutes):
        ts = start + timedelta(minutes=i)
        rows.append(
            {
                "bucket_start": ts,
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1000 * (i + 1),
                "vwap": 100.25 + i,
                "tick_count": 10 + i,
                "source": "live",
            }
        )
    table = pa.Table.from_pylist(rows, schema=agg.BAR_SCHEMA)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def test_aggregate_day_5m_from_5_1m_bars(tmp_path):
    src = tmp_path / "bars.parquet"
    _make_1m_day(src, n_minutes=5)

    out = agg._aggregate_day(src, 5)
    assert out.num_rows == 1

    row = out.to_pylist()[0]
    assert row["bucket_start"] == datetime(2026, 4, 18, 13, 35, tzinfo=UTC)
    assert row["open"] == 100.0
    assert row["close"] == 104.5
    assert row["high"] == 105.0
    assert row["low"] == 99.0
    # volumes 1000,2000,3000,4000,5000 -> 15000
    assert row["volume"] == 15000
    # sum of vwap*volume / total_volume
    expected_vwap = sum((100.25 + i) * 1000 * (i + 1) for i in range(5)) / 15000
    assert row["vwap"] == pytest.approx(expected_vwap)
    assert row["tick_count"] == sum(10 + i for i in range(5))
    assert row["source"] == "live"


def test_aggregate_day_empty(tmp_path):
    src = tmp_path / "empty.parquet"
    src.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(agg.BAR_SCHEMA.empty_table(), src, compression="zstd")

    out = agg._aggregate_day(src, 5)
    assert out.num_rows == 0


def test_aggregate_day_two_5m_chunks(tmp_path):
    src = tmp_path / "bars.parquet"
    _make_1m_day(src, n_minutes=6)  # 09:31..09:36
    out = agg._aggregate_day(src, 5)

    # 09:31..09:35 -> first chunk, 09:36 -> second chunk
    assert out.num_rows == 2
    ts_list = out.column("bucket_start").to_pylist()
    assert ts_list[0] == datetime(2026, 4, 18, 13, 35, tzinfo=UTC)
    assert ts_list[1] == datetime(2026, 4, 18, 13, 40, tzinfo=UTC)
