from __future__ import annotations

import subprocess as _sp

import _common
import pytest


def test_repo_root_points_at_repo():
    assert (_common.REPO_ROOT / "src").is_dir()
    assert (_common.REPO_ROOT / "scripts").is_dir()


def test_python_dir_resolves():
    # Single-root layout: PYTHON_DIR collapses onto REPO_ROOT, the two
    # names are kept as aliases so the wrapper scripts copied from the
    # parent repo keep working unchanged.
    assert _common.PYTHON_DIR == _common.REPO_ROOT
    assert _common.PYTHON_DIR.is_dir()


def test_ensure_uv_found(monkeypatch):
    monkeypatch.setattr(_common.shutil, "which", lambda name: "/usr/bin/uv")
    _common.ensure_uv()  # must not raise


def test_ensure_uv_missing_exits(monkeypatch):
    monkeypatch.setattr(_common.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as excinfo:
        _common.ensure_uv()
    assert "uv" in str(excinfo.value)


class _FakePopen:
    """Minimal Popen stand-in: captures cmd/cwd and returns a scripted rc."""

    def __init__(self, returncode: int = 0, wait_effects=None):
        self._rc = returncode
        # Each call to wait() pops one item; KeyboardInterrupt instances
        # are raised, everything else is returned as the returncode.
        self._effects = list(wait_effects or [])

    def __call__(self, cmd, *, cwd=None, **kwargs):
        self.cmd = list(cmd)
        self.cwd = cwd
        return self

    def wait(self):
        if self._effects:
            effect = self._effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return self._rc


def test_run_uv_module_builds_cmd(monkeypatch):
    monkeypatch.setattr(_common, "ensure_uv", lambda: None)
    fake = _FakePopen(returncode=0)
    monkeypatch.setattr(_sp, "Popen", fake)

    rc = _common.run_uv_module("foo.bar", ["--flag", "value"])

    assert rc == 0
    assert fake.cmd == ["uv", "run", "python", "-m", "foo.bar", "--flag", "value"]
    assert fake.cwd == _common.PYTHON_DIR


def test_run_uv_entrypoint_builds_cmd(monkeypatch):
    monkeypatch.setattr(_common, "ensure_uv", lambda: None)
    fake = _FakePopen(returncode=3)
    monkeypatch.setattr(_sp, "Popen", fake)

    rc = _common.run_uv_entrypoint("tradestation-data-ingest", ["--config", "cfg.yaml"])

    assert rc == 3
    assert fake.cmd == ["uv", "run", "tradestation-data-ingest", "--config", "cfg.yaml"]
    assert fake.cwd == _common.PYTHON_DIR


def test_wait_graceful_swallows_keyboard_interrupt(monkeypatch, capsys):
    """Ctrl+C in the wrapper must not propagate — the child handles SIGINT
    itself, so the wrapper just keeps waiting and prints a friendly note."""
    monkeypatch.setattr(_common, "ensure_uv", lambda: None)
    # First wait() raises (user's Ctrl+C), second wait() returns 0 (child
    # finished its graceful shutdown).
    fake = _FakePopen(wait_effects=[KeyboardInterrupt(), 0])
    monkeypatch.setattr(_sp, "Popen", fake)

    rc = _common.run_uv_entrypoint("tradestation-data-ingest", [])

    assert rc == 0
    err = capsys.readouterr().err
    assert "interrupt" in err.lower()
    assert "shutdown complete" in err.lower()


def test_wait_graceful_handles_repeated_interrupts(monkeypatch, capsys):
    """Impatient user hammers Ctrl+C — we should still only print the
    'waiting...' banner once and finish cleanly when the child exits."""
    monkeypatch.setattr(_common, "ensure_uv", lambda: None)
    fake = _FakePopen(
        wait_effects=[
            KeyboardInterrupt(),
            KeyboardInterrupt(),
            KeyboardInterrupt(),
            0,
        ]
    )
    monkeypatch.setattr(_sp, "Popen", fake)

    rc = _common.run_uv_entrypoint("tradestation-data-ingest", [])

    assert rc == 0
    err = capsys.readouterr().err
    # Banner printed exactly once, not per Ctrl+C.
    assert err.lower().count("waiting for ingestion runtime") == 1
