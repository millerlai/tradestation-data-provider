from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import dump_parquet as dp
import pyarrow as pa
import pyarrow.parquet as pq


def test_format_value_none():
    assert dp._format_value(None, None) == ""


def test_format_value_preserves_non_datetime():
    assert dp._format_value(42, None) == "42"
    assert dp._format_value("abc", ZoneInfo("UTC")) == "abc"


def test_format_value_converts_tz():
    utc = datetime(2026, 4, 18, 13, 31, tzinfo=UTC)
    et = ZoneInfo("America/New_York")
    assert dp._format_value(utc, et).startswith("2026-04-18 09:31")


def test_format_value_naive_datetime_preserved():
    naive = datetime(2026, 4, 18, 13, 31)
    # tz-aware conversion only applies when input has tzinfo
    assert "2026-04-18 13:31" in dp._format_value(naive, ZoneInfo("America/New_York"))


def test_print_rows_prints_header_and_data(capsys):
    rows = [{"a": 1, "b": "x"}, {"a": 22, "b": "yy"}]
    dp._print_rows(rows, ["a", "b"], None)
    out = capsys.readouterr().out
    assert "a" in out and "b" in out
    assert "1" in out
    assert "22" in out
    assert "yy" in out


def test_main_schema_only_prints_schema(tmp_path, monkeypatch, capsys):
    path = tmp_path / "x.parquet"
    table = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})
    pq.write_table(table, path)

    monkeypatch.setattr("sys.argv", ["dump_parquet.py", str(path), "--schema-only"])
    rc = dp.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "rows        : 3" in out
    assert "schema:" in out
    assert "c" in out


def test_main_missing_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "nope.parquet"
    monkeypatch.setattr("sys.argv", ["dump_parquet.py", str(path)])
    rc = dp.main()
    assert rc == 2
    assert "file not found" in capsys.readouterr().err


def test_main_unknown_tz(tmp_path, monkeypatch, capsys):
    path = tmp_path / "x.parquet"
    pq.write_table(pa.table({"c": [1]}), path)

    monkeypatch.setattr(
        "sys.argv", ["dump_parquet.py", str(path), "--tz", "Mars/Olympus", "--schema-only"]
    )
    rc = dp.main()
    assert rc == 2
    assert "unknown timezone" in capsys.readouterr().err
