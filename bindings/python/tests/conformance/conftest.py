from __future__ import annotations

import json
from pathlib import Path

import pytest

# bindings/python/tests/conformance/conftest.py → parents[2] is the Python
# binding root, parents[4] is the repo root. contract/ sits at the top,
# shared by every binding.
CONTRACT_DIR = Path(__file__).resolve().parents[4] / "contract"
FIXTURES_DIR = CONTRACT_DIR / "fixtures"


def pytest_collection_modifyitems(config, items):
    """Skip this package when contract/ is not on disk.

    The sdist ships tests/ but not contract/ — the fixtures live above the
    binding root and hatchling's include list cannot reach out of it. So
    `pip download --no-binary :all:` + `pytest` would otherwise fail with ten
    FileNotFoundErrors that say nothing about the package. Inside the work
    tree the directory is always there, which is where these tests matter.
    """
    if FIXTURES_DIR.is_dir():
        return
    skip = pytest.mark.skip(reason=f"contract fixtures not present at {FIXTURES_DIR} (sdist)")
    here = Path(__file__).parent
    for item in items:
        if here in Path(item.fspath).parents:
            item.add_marker(skip)


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
