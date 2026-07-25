"""Tests for tradestation_data.tools.audit_bar_cache (T2.5.E.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from tradestation_data.tools import audit_bar_cache as abm


def _frame(rows: list[dict]) -> pl.DataFrame:
    """Helper: build a bars-shaped DataFrame from row dicts."""
    if not rows:
        return pl.DataFrame(
            schema={
                "bucket_start": pl.Datetime("us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "tick_count": pl.Int64,
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("bucket_start").dt.replace_time_zone("UTC"))


def _row(
    t: datetime,
    *,
    o: float = 100.0,
    h: float = 100.5,
    low: float = 99.5,
    c: float = 100.2,
    v: int = 1000,
    tc: int = 50,
) -> dict:
    return {
        "bucket_start": t,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": v,
        "tick_count": tc,
    }


def test_audit_result_clean_property():
    r = abm.AuditResult(symbol="SPY", day="2026-04-20", live_rows=3, rebuilt_rows=3, diffs=[])
    assert r.clean is True
    r2 = abm.AuditResult(symbol="SPY", day="2026-04-20", live_rows=3, rebuilt_rows=3, diffs=["x"])
    assert r2.clean is False


def test_parse_args_requires_data_root_and_symbols():
    with pytest.raises(SystemExit):
        abm._parse_args([])
    with pytest.raises(SystemExit):
        abm._parse_args(["--data-root", "/tmp"])


def test_parse_args_parses_end_date():
    args = abm._parse_args(
        [
            "--data-root",
            "/tmp",
            "--symbols",
            "SPY",
            "--end-date",
            "2026-04-15",
            "--days",
            "3",
        ]
    )
    assert args.symbols == ["SPY"]
    assert args.days == 3
    assert args.end_date == datetime(2026, 4, 15, tzinfo=UTC)


def test_compare_identical_frames_has_no_diffs():
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    rows = [_row(t + timedelta(minutes=i)) for i in range(3)]
    assert abm._compare_dataframes(_frame(rows), _frame(rows)) == []


def test_compare_row_count_mismatch_is_reported():
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    live = _frame([_row(t + timedelta(minutes=i)) for i in range(3)])
    rebuilt = _frame([_row(t + timedelta(minutes=i)) for i in range(2)])
    diffs = abm._compare_dataframes(live, rebuilt)
    assert any(d.startswith("row_count ") for d in diffs)


def test_compare_missing_buckets_on_each_side():
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    # live has [0, 1, 2], rebuilt has [1, 2, 3] — 0 missing_in_rebuilt, 3 missing_in_live.
    live = _frame([_row(t + timedelta(minutes=i)) for i in (0, 1, 2)])
    rebuilt = _frame([_row(t + timedelta(minutes=i)) for i in (1, 2, 3)])
    diffs = abm._compare_dataframes(live, rebuilt)
    assert any("missing_in_rebuilt" in d for d in diffs)
    assert any("missing_in_live" in d for d in diffs)


def test_compare_price_drift_reports_column():
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    live = _frame([_row(t, c=100.0)])
    rebuilt = _frame([_row(t, c=100.1)])  # clearly beyond 1e-9 tol
    diffs = abm._compare_dataframes(live, rebuilt)
    assert any(d.startswith("price_drift column=close") for d in diffs)


def test_compare_volume_drift_reports_column():
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    live = _frame([_row(t, v=1000)])
    rebuilt = _frame([_row(t, v=1001)])
    diffs = abm._compare_dataframes(live, rebuilt)
    assert any(d.startswith("count_drift column=volume") for d in diffs)


def test_compare_float_noise_within_tolerance_is_clean():
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    live = _frame([_row(t, c=100.0)])
    rebuilt = _frame([_row(t, c=100.0 + 1e-12)])  # below _PRICE_TOL
    assert abm._compare_dataframes(live, rebuilt) == []


class _StubStore:
    def __init__(self, per_day: dict[str, pl.DataFrame]):
        self._per_day = per_day

    def load_bars(self, symbol: str, start, end, tf: str) -> pl.DataFrame:
        return self._per_day.get(start.date().isoformat(), _frame([]))


class _StubResampler:
    def __init__(self, per_day: dict[str, pl.DataFrame]):
        self._per_day = per_day

    def resample(self, symbol: str, start, end, tf: str) -> pl.DataFrame:
        return self._per_day.get(start.date().isoformat(), _frame([]))


def _install_stubs(monkeypatch, *, live_by_day, rebuilt_by_day):
    monkeypatch.setattr(abm, "HistoryStore", lambda root: _StubStore(live_by_day))
    monkeypatch.setattr(abm, "Resampler", lambda tick_root: _StubResampler(rebuilt_by_day))


def test_main_missing_data_root_returns_2(tmp_path):
    rc = abm.main(
        [
            "--data-root",
            str(tmp_path / "does_not_exist"),
            "--symbols",
            "SPY",
        ]
    )
    assert rc == 2


def test_main_all_empty_returns_2(tmp_path, monkeypatch):
    _install_stubs(monkeypatch, live_by_day={}, rebuilt_by_day={})
    rc = abm.main(
        [
            "--data-root",
            str(tmp_path),
            "--symbols",
            "SPY",
            "--days",
            "2",
            "--end-date",
            "2026-04-20",
        ]
    )
    assert rc == 2


def test_main_clean_run_returns_0(tmp_path, monkeypatch):
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    rows = [_row(t + timedelta(minutes=i)) for i in range(3)]
    day_key = "2026-04-20"
    _install_stubs(
        monkeypatch,
        live_by_day={day_key: _frame(rows)},
        rebuilt_by_day={day_key: _frame(rows)},
    )
    rc = abm.main(
        [
            "--data-root",
            str(tmp_path),
            "--symbols",
            "SPY",
            "--days",
            "1",
            "--end-date",
            "2026-04-20",
        ]
    )
    assert rc == 0


def test_main_drift_run_returns_1(tmp_path, monkeypatch):
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    day_key = "2026-04-20"
    live = _frame([_row(t, c=100.0)])
    rebuilt = _frame([_row(t, c=100.5)])  # price drift
    _install_stubs(
        monkeypatch,
        live_by_day={day_key: live},
        rebuilt_by_day={day_key: rebuilt},
    )
    rc = abm.main(
        [
            "--data-root",
            str(tmp_path),
            "--symbols",
            "SPY",
            "--days",
            "1",
            "--end-date",
            "2026-04-20",
        ]
    )
    assert rc == 1
