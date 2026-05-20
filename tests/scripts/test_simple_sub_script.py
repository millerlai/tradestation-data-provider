from __future__ import annotations

import pytest
import simple_sub


def test_main_help_exits_cleanly(monkeypatch):
    # argparse's built-in --help exits with code 0 via SystemExit.
    monkeypatch.setattr("sys.argv", ["simple_sub.py", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        simple_sub.main()
    assert excinfo.value.code == 0


def test_main_unknown_flag_exits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["simple_sub.py", "--no-such-flag"])
    with pytest.raises(SystemExit) as excinfo:
        simple_sub.main()
    assert excinfo.value.code != 0


def test_module_exposes_main():
    assert callable(simple_sub.main)
