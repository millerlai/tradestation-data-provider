from __future__ import annotations

from datetime import UTC, datetime

import dedupe_bars
import pyarrow as pa
import pyarrow.parquet as pq


def _write_bars(path, timestamps):
    table = pa.table(
        {
            "bar_time": pa.array(timestamps, type=pa.timestamp("us", tz="UTC")),
            "close": pa.array([100.0 + i for i in range(len(timestamps))], type=pa.float64()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def test_dedupe_file_no_duplicates_leaves_file_untouched(tmp_path):
    path = tmp_path / "bars.parquet"
    ts = [datetime(2026, 4, 18, 13, m, tzinfo=UTC) for m in range(31, 36)]
    _write_bars(path, ts)
    before_mtime = path.stat().st_mtime_ns

    before, after = dedupe_bars.dedupe_file(path, dry_run=False)
    assert before == after == 5
    assert path.stat().st_mtime_ns == before_mtime


def test_dedupe_file_removes_dups(tmp_path):
    path = tmp_path / "bars.parquet"
    base = [datetime(2026, 4, 18, 13, m, tzinfo=UTC) for m in range(31, 34)]
    ts = [base[0], base[0], base[1], base[2], base[2]]  # 5 rows, 3 unique
    _write_bars(path, ts)

    before, after = dedupe_bars.dedupe_file(path, dry_run=False)
    assert before == 5
    assert after == 3

    rewritten = pq.read_table(path).column("bar_time").to_pylist()
    assert len(rewritten) == 3
    assert rewritten == sorted(rewritten)


def test_dedupe_file_dry_run_preserves_dups(tmp_path):
    path = tmp_path / "bars.parquet"
    base = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    _write_bars(path, [base, base, base])

    before, after = dedupe_bars.dedupe_file(path, dry_run=True)
    assert before == 3
    assert after == 1

    # Dry run must not rewrite the file.
    on_disk = pq.read_table(path).num_rows
    assert on_disk == 3


def test_tick_partitions_are_skipped_not_deduped(tmp_path):
    """bartype=0 rows share a bar_time by design; deduping deletes real trades.

    A tick chart's ts_str resolves to the second, and a second holds many
    prints, so they carry the same bar_time and only `ts` separates them.
    Keyed on bar_time, this tool would keep one print per second and delete
    the rest — the exact collapse the ingest path bypasses its buffer to
    avoid.
    """
    part = tmp_path / "bartype=0" / "interval=1" / "symbol=SPY" / "date=2026-04-20"
    part.mkdir(parents=True)
    ts = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    # Five distinct prints, one minute, one bar_time.
    table = pa.table(
        {
            "bar_time": [ts] * 5,
            "close": [450.0, 450.01, 450.02, 450.03, 450.04],
            "ts": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    pq.write_table(table, part / "bars.parquet")

    path = part / "bars.parquet"
    assert dedupe_bars.is_tick_partition(path), "bartype=0 must be recognised"

    # The guard lives in main()'s walk, so prove the walk honours it: run the
    # tool the way its docs tell an operator to.
    import sys

    argv = sys.argv
    try:
        sys.argv = ["dedupe_bars.py", "--root", str(tmp_path)]
        dedupe_bars.main()
    finally:
        sys.argv = argv

    assert pq.read_table(path).num_rows == 5, "a tick partition must not be deduped on bar_time"
