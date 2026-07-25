from __future__ import annotations

import clear_bar_cache


def _patch(monkeypatch):
    captured: dict = {}

    def fake(module, argv):
        captured["module"] = module
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(clear_bar_cache, "run_uv_module", fake)
    return captured


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["clear_bar_cache.py", *argv])
    return clear_bar_cache.main()


def test_default_all_timeframes_dry_run(monkeypatch):
    captured = _patch(monkeypatch)
    assert _run(monkeypatch, []) == 0

    assert captured["module"] == "tradestation_data.tools.clear_bar_cache"
    argv = captured["argv"]
    tf_idx = argv.index("--timeframes")
    assert argv[tf_idx + 1 : tf_idx + 6] == ["5m", "15m", "30m", "1h", "1d"]
    assert "--confirm" not in argv


def test_custom_timeframes_with_confirm(monkeypatch):
    captured = _patch(monkeypatch)
    _run(monkeypatch, ["--timeframes", "5m,15m", "--confirm"])

    argv = captured["argv"]
    tf_idx = argv.index("--timeframes")
    assert argv[tf_idx + 1 : tf_idx + 3] == ["5m", "15m"]
    assert "--confirm" in argv
