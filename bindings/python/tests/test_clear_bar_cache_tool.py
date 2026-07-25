"""Tests for tradestation_data.tools.clear_bar_cache (T2.5.E.2)."""

from __future__ import annotations

from pathlib import Path

from tradestation_data.tools import clear_bar_cache as ccm


def _seed_cache(root: Path, timeframes: tuple[str, ...], files_per_tf: int = 2) -> None:
    """Create fake timeframe= dirs with N parquet files each."""
    bars = root / "bars"
    for tf in timeframes:
        tf_dir = bars / f"timeframe={tf}"
        sym_dir = tf_dir / "symbol=SPY" / "date=2026-04-20"
        sym_dir.mkdir(parents=True, exist_ok=True)
        for i in range(files_per_tf):
            (sym_dir / f"bars-{i}.parquet").write_bytes(b"fake")


def test_parse_args_defaults_to_all_tier3_timeframes(tmp_path):
    args = ccm._parse_args(["--data-root", str(tmp_path)])
    assert args.timeframes == list(ccm.TIER3_TIMEFRAMES)
    assert args.confirm is False


def test_parse_args_custom_timeframes(tmp_path):
    args = ccm._parse_args(["--data-root", str(tmp_path), "--timeframes", "5m", "15m", "--confirm"])
    assert args.timeframes == ["5m", "15m"]
    assert args.confirm is True


def test_refuses_to_clear_protected_1m_cache(tmp_path, caplog):
    _seed_cache(tmp_path, ("1m", "5m"))
    rc = ccm.main(
        [
            "--data-root",
            str(tmp_path),
            "--timeframes",
            "1m",
            "5m",
            "--confirm",
        ]
    )
    assert rc == 2
    # The 1m cache must still be present after refusal.
    assert (tmp_path / "bars" / "timeframe=1m").exists()
    assert (tmp_path / "bars" / "timeframe=5m").exists()


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
    _seed_cache(tmp_path, ("5m", "15m"))
    rc = ccm.main(["--data-root", str(tmp_path), "--timeframes", "5m", "15m"])
    assert rc == 0
    # Without --confirm, nothing should be deleted.
    assert (tmp_path / "bars" / "timeframe=5m").exists()
    assert (tmp_path / "bars" / "timeframe=15m").exists()
    assert list((tmp_path / "bars" / "timeframe=5m").rglob("*.parquet"))


def test_confirm_removes_target_dirs(tmp_path):
    _seed_cache(tmp_path, ("5m", "15m", "1h"))
    rc = ccm.main(
        [
            "--data-root",
            str(tmp_path),
            "--timeframes",
            "5m",
            "15m",
            "--confirm",
        ]
    )
    assert rc == 0
    assert not (tmp_path / "bars" / "timeframe=5m").exists()
    assert not (tmp_path / "bars" / "timeframe=15m").exists()
    # Untargeted timeframe (1h) is left alone.
    assert (tmp_path / "bars" / "timeframe=1h").exists()


def test_partial_delete_failure_returns_1(tmp_path, monkeypatch):
    """rmtree raising on one of many targets → rc=1 but others still removed."""
    _seed_cache(tmp_path, ("5m", "15m"))
    real_rmtree = ccm.shutil.rmtree
    failed_on: list[Path] = []

    def fake_rmtree(path, *args, **kwargs):
        p = Path(path)
        if p.name == "timeframe=5m":
            failed_on.append(p)
            raise OSError("simulated")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ccm.shutil, "rmtree", fake_rmtree)
    rc = ccm.main(
        [
            "--data-root",
            str(tmp_path),
            "--timeframes",
            "5m",
            "15m",
            "--confirm",
        ]
    )
    assert rc == 1
    assert failed_on, "fake_rmtree was never called on 5m"
    # 15m should have been removed cleanly.
    assert not (tmp_path / "bars" / "timeframe=15m").exists()


def test_protected_set_is_literally_1m_only():
    # Guardrail: if someone adds more protected timeframes, this reminder fires.
    assert frozenset({"1m"}) == ccm.PROTECTED_TIMEFRAMES
    assert "1m" not in ccm.TIER3_TIMEFRAMES
