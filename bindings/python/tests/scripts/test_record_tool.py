from __future__ import annotations

import json

import pytest
import record


def test_main_help_exits_cleanly(monkeypatch):
    # argparse's built-in --help exits with code 0 via SystemExit.
    monkeypatch.setattr("sys.argv", ["record.py", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        record.main()
    assert excinfo.value.code == 0


def test_main_unknown_flag_exits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["record.py", "--no-such-flag"])
    with pytest.raises(SystemExit) as excinfo:
        record.main()
    assert excinfo.value.code != 0


def test_module_exposes_main():
    assert callable(record.main)


def test_fixture_entry_preserves_payload_verbatim():
    raw = b'{"v":2,"kind":"tick","seq":1,"sid":9,"ts":1.5,"px":450.0}'
    entry = record.fixture_entry("SPY", raw)
    assert entry == {"topic": "SPY", "payload": raw.decode()}


def test_fixture_entry_keeps_unparseable_frames():
    """A frame json.loads would reject must still reach the fixture.

    Fixtures are the reference every binding gets checked against, so
    dropping what the recorder could not read would quietly narrow the wire
    down to the subset we already knew how to handle — and hide exactly the
    frames worth testing against.
    """
    raw = b'{"v":2,"kind":"tick",TRUNCATED'
    entry = record.fixture_entry("SPY", raw)
    assert entry["payload"] == raw.decode()


def test_fixture_entry_flags_invalid_utf8_instead_of_coercing():
    raw = b"\xff\xfe not utf-8"
    entry = record.fixture_entry("SPY", raw)
    assert "payload" not in entry
    assert entry["payload_invalid_utf8"] == repr(raw)


def test_fixture_entry_lines_are_valid_jsonl():
    line = json.dumps(record.fixture_entry("$TICK", b'{"v":2}'), ensure_ascii=False)
    assert "\n" not in line
    assert json.loads(line)["topic"] == "$TICK"
