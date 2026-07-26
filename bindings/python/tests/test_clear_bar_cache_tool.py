"""Tests for tradestation_data.tools.clear_bar_cache (T2.5.E.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar
from tradestation_data.storage.bar_writer import BarWriter
from tradestation_data.tools import clear_bar_cache as ccm


def _write_bar(root: Path, timeframe: str, source: str, *, symbol: str = "SPY") -> Path:
    """Write one real bars.parquet under bars/timeframe=<tf>/symbol=.../date=..."""
    with BarWriter(root / "bars") as w:
        w.write(
            Bar(
                symbol=symbol,
                bucket_start=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
                open=1.0,
                high=2.0,
                low=0.5,
                close=1.5,
                volume=10,
                tick_count=3,
                source=source,
                timeframe=timeframe,
            )
        )
    (path,) = (root / "bars" / f"timeframe={timeframe}" / f"symbol={symbol}").rglob("bars.parquet")
    return path


def _seed_derived(root: Path, timeframes: tuple[str, ...]) -> None:
    for tf in timeframes:
        _write_bar(root, tf, "derived:ticks")


def test_parse_args_defaults_to_all_tier3_timeframes(tmp_path):
    args = ccm._parse_args(["--data-root", str(tmp_path)])
    assert args.timeframes == list(ccm.TIER3_TIMEFRAMES)
    assert args.confirm is False


def test_parse_args_custom_timeframes(tmp_path):
    args = ccm._parse_args(["--data-root", str(tmp_path), "--timeframes", "5m", "15m", "--confirm"])
    assert args.timeframes == ["5m", "15m"]
    assert args.confirm is True


def test_tier3_default_excludes_the_live_1m_tier(tmp_path):
    assert "1m" not in ccm.TIER3_TIMEFRAMES


def test_missing_bars_root_is_noop(tmp_path):
    # data_root exists but no bars/ subdir — should just warn + rc=0.
    rc = ccm.main(["--data-root", str(tmp_path)])
    assert rc == 0


def test_no_matching_timeframe_dirs_is_noop(tmp_path):
    # bars/ exists but none of the requested timeframes have dirs under it.
    (tmp_path / "bars").mkdir()
    rc = ccm.main(["--data-root", str(tmp_path), "--timeframes", "5m", "15m"])
    assert rc == 0


def test_dry_run_preserves_all_files(tmp_path):
    _seed_derived(tmp_path, ("5m", "15m"))
    rc = ccm.main(["--data-root", str(tmp_path), "--timeframes", "5m", "15m"])
    assert rc == 0
    # Without --confirm, nothing should be deleted.
    assert list((tmp_path / "bars" / "timeframe=5m").rglob("bars.parquet"))
    assert list((tmp_path / "bars" / "timeframe=15m").rglob("bars.parquet"))


def test_confirm_removes_derived_partitions(tmp_path):
    _seed_derived(tmp_path, ("5m", "15m", "1h"))
    rc = ccm.main(
        ["--data-root", str(tmp_path), "--timeframes", "5m", "15m", "--confirm"],
    )
    assert rc == 0
    assert not list((tmp_path / "bars" / "timeframe=5m").rglob("bars.parquet"))
    assert not list((tmp_path / "bars" / "timeframe=15m").rglob("bars.parquet"))
    # Untargeted timeframe (1h) is left alone.
    assert list((tmp_path / "bars" / "timeframe=1h").rglob("bars.parquet"))


def test_native_daily_bars_survive_a_confirmed_clear(tmp_path):
    """The default run covers 1d, and native daily bars now live there.

    Since the wire started carrying `tf`, a daily chart writes a *native*
    bar into bars/timeframe=1d/. It holds the exchange's official close and
    the split/dividend adjustment, neither of which a tick rollup can
    reproduce — deleting it by directory name would swap real data for a
    plausible approximation with nothing to show it happened.
    """
    native = _write_bar(tmp_path, "1d", "tradestation_el")
    derived = _write_bar(tmp_path, "1d", "derived:ticks", symbol="QQQ")

    rc = ccm.main(["--data-root", str(tmp_path), "--confirm"])

    assert rc == 0
    assert native.exists(), "native daily bar was deleted"
    assert not derived.exists(), "derived daily bar should have been evicted"
    assert pq.read_table(native)["source"].to_pylist() == ["tradestation_el"]


def test_unreadable_partition_is_kept_not_deleted(tmp_path):
    """A file we cannot interrogate is treated as native — the safe failure."""
    junk = tmp_path / "bars" / "timeframe=5m" / "symbol=SPY" / "date=2026-04-20"
    junk.mkdir(parents=True)
    (junk / "bars.parquet").write_bytes(b"not parquet")

    rc = ccm.main(["--data-root", str(tmp_path), "--timeframes", "5m", "--confirm"])

    assert rc == 0
    assert (junk / "bars.parquet").exists()


def test_delete_failure_returns_1(tmp_path, monkeypatch):
    """An unlink that fails is reported, not swallowed into a clean exit."""
    _seed_derived(tmp_path, ("5m",))

    def boom(self, *args, **kwargs):
        raise OSError("simulated")

    monkeypatch.setattr(Path, "unlink", boom)
    rc = ccm.main(["--data-root", str(tmp_path), "--timeframes", "5m", "--confirm"])
    assert rc == 1
