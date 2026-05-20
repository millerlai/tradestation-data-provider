from __future__ import annotations

import audit_bar_cache
import pytest


def test_load_symbols_from_yaml_extracts_ids(tmp_path):
    yaml = tmp_path / "symbols.yaml"
    yaml.write_text(
        "symbols:\n"
        "  - { id: SPY, kind: equity }\n"
        "  - { id: QQQ, kind: equity }\n"
        "  - { id: $TICK, kind: index }\n"
        "# comment line\n"
        "  - not-a-match\n",
        encoding="utf-8",
    )
    assert audit_bar_cache._load_symbols_from_yaml(yaml) == ["SPY", "QQQ", "$TICK"]


def test_load_symbols_from_empty_yaml(tmp_path):
    yaml = tmp_path / "empty.yaml"
    yaml.write_text("", encoding="utf-8")
    assert audit_bar_cache._load_symbols_from_yaml(yaml) == []


def _patch(monkeypatch):
    captured: dict = {}

    def fake(module, argv):
        captured["module"] = module
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(audit_bar_cache, "run_uv_module", fake)
    return captured


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["audit_bar_cache.py", *argv])
    return audit_bar_cache.main()


def test_explicit_symbols_bypass_yaml(monkeypatch):
    captured = _patch(monkeypatch)
    assert _run(monkeypatch, ["--symbols", "SPY,QQQ", "--days", "3"]) == 0

    assert captured["module"] == "tradestation_data.tools.audit_bar_cache"
    argv = captured["argv"]
    syms_idx = argv.index("--symbols")
    assert argv[syms_idx + 1 : syms_idx + 3] == ["SPY", "QQQ"]
    assert argv[argv.index("--days") + 1] == "3"


def test_missing_yaml_and_no_symbols_exits(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(
        audit_bar_cache, "PYTHON_DIR", audit_bar_cache.PYTHON_DIR.parent / "no-such-dir"
    )
    with pytest.raises(SystemExit):
        _run(monkeypatch, [])


def test_auto_detect_from_yaml(monkeypatch, tmp_path):
    fake_python_dir = tmp_path / "python"
    (fake_python_dir / "config").mkdir(parents=True)
    (fake_python_dir / "config" / "symbols.yaml").write_text(
        "symbols:\n  - { id: SPY }\n  - { id: VXX }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_bar_cache, "PYTHON_DIR", fake_python_dir)

    captured = _patch(monkeypatch)
    _run(monkeypatch, [])
    argv = captured["argv"]
    syms_idx = argv.index("--symbols")
    assert argv[syms_idx + 1 : syms_idx + 3] == ["SPY", "VXX"]
