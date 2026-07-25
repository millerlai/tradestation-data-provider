from __future__ import annotations

import json
from pathlib import Path

import pytest

# bindings/python/tests/conformance/conftest.py → parents[2] is the Python
# binding root, parents[4] is the repo root. contract/ sits at the top,
# shared by every binding.
CONTRACT_DIR = Path(__file__).resolve().parents[4] / "contract"
FIXTURES_DIR = CONTRACT_DIR / "fixtures"


def load_case(name: str) -> tuple[list[tuple[str, bytes]], dict]:
    """Return (frames, expected) for one fixture.

    Frames come back as raw (topic, payload-bytes) pairs so the binding is
    exercised on exactly what the DLL emitted, not on a re-serialised
    approximation of it.
    """
    frames: list[tuple[str, bytes]] = []
    with (FIXTURES_DIR / f"{name}.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            entry = json.loads(line)
            if "payload" not in entry:
                pytest.fail(f"{name}.jsonl has a frame that was not valid UTF-8: {entry}")
            frames.append((entry["topic"], entry["payload"].encode("utf-8")))

    expected = json.loads((FIXTURES_DIR / "expected" / f"{name}.json").read_text(encoding="utf-8"))
    return frames, expected
