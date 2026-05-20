from __future__ import annotations

import pytest
import run_ingestion


def _patch(monkeypatch):
    captured: dict = {}

    def fake(entrypoint, argv):
        captured["entrypoint"] = entrypoint
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(run_ingestion, "run_uv_entrypoint", fake)
    return captured


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["run_ingestion.py", *argv])
    return run_ingestion.main()


def test_explicit_config_passes_through(monkeypatch, tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols: []\n", encoding="utf-8")
    captured = _patch(monkeypatch)

    assert _run(monkeypatch, ["--config", str(cfg)]) == 0
    assert captured["entrypoint"] == "tradestation-data-ingest"

    argv = captured["argv"]
    assert argv[argv.index("--config") + 1] == str(cfg)
    assert argv[argv.index("--endpoint") + 1] == "tcp://127.0.0.1:5555"
    assert argv[argv.index("--log-level") + 1] == "INFO"
    assert "--no-storage" not in argv
    assert "--log-json" not in argv


def test_flags_forwarded(monkeypatch, tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("", encoding="utf-8")
    captured = _patch(monkeypatch)

    _run(
        monkeypatch,
        [
            "--config",
            str(cfg),
            "--log-level",
            "DEBUG",
            "--log-json",
            "--no-storage",
            "--heartbeat-seconds",
            "5",
            "--print-bars",
            "10",
        ],
    )
    argv = captured["argv"]
    assert argv[argv.index("--log-level") + 1] == "DEBUG"
    assert "--log-json" in argv
    assert "--no-storage" in argv
    assert argv[argv.index("--heartbeat-seconds") + 1] == "5.0"
    assert argv[argv.index("--print-bars") + 1] == "10"


def test_print_bars_default_is_not_forwarded(monkeypatch, tmp_path):
    # --print-bars 0 (default) must not appear on the forwarded argv so the
    # runtime stays in its default quiet path.
    cfg = tmp_path / "s.yaml"
    cfg.write_text("", encoding="utf-8")
    captured = _patch(monkeypatch)
    _run(monkeypatch, ["--config", str(cfg)])
    assert "--print-bars" not in captured["argv"]


def test_config_autodetect_root_first(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "python" / "config").mkdir(parents=True)
    root_cfg = repo / "config" / "symbols.yaml"
    root_cfg.write_text("x", encoding="utf-8")
    py_cfg = repo / "python" / "config" / "symbols.yaml"
    py_cfg.write_text("y", encoding="utf-8")

    monkeypatch.setattr(run_ingestion, "REPO_ROOT", repo)
    monkeypatch.setattr(run_ingestion, "PYTHON_DIR", repo / "python")

    captured = _patch(monkeypatch)
    _run(monkeypatch, [])
    argv = captured["argv"]
    assert argv[argv.index("--config") + 1] == str(root_cfg)


def test_config_autodetect_falls_back_to_python_dir(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "python" / "config").mkdir(parents=True)
    py_cfg = repo / "python" / "config" / "symbols.yaml"
    py_cfg.write_text("y", encoding="utf-8")

    monkeypatch.setattr(run_ingestion, "REPO_ROOT", repo)
    monkeypatch.setattr(run_ingestion, "PYTHON_DIR", repo / "python")

    captured = _patch(monkeypatch)
    _run(monkeypatch, [])
    argv = captured["argv"]
    assert argv[argv.index("--config") + 1] == str(py_cfg)


def test_missing_python_dir_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(run_ingestion, "PYTHON_DIR", tmp_path / "missing")
    _patch(monkeypatch)
    with pytest.raises(SystemExit):
        _run(monkeypatch, [])
