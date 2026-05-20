"""Shared helpers for the wrapper scripts under ``scripts/``.

Every wrapper shells out to ``uv run ...`` from the project root so uv
can resolve the project venv + lockfile. Keeping the path + uv-lookup
logic here avoids repeating it in every wrapper.

Layout assumed by this module:
    <project-root>/pyproject.toml
    <project-root>/scripts/_common.py     ← this file
    <project-root>/src/tradestation_data/...

REPO_ROOT and PYTHON_DIR both resolve to <project-root>; the two names
are kept (rather than collapsed into one) so the wrapper scripts copied
over from the parent repo continue to work without modification.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_DIR = REPO_ROOT


def ensure_uv() -> None:
    if shutil.which("uv") is None:
        sys.exit("'uv' not found on PATH. Install from https://docs.astral.sh/uv/ and re-run.")


def _wait_graceful(proc: subprocess.Popen) -> int:
    """Block until *proc* exits, swallowing Ctrl+C in the wrapper process.

    On Windows a console Ctrl+C is delivered to the whole process group,
    so the child already received SIGINT and is running its own graceful
    shutdown path (closing sockets, flushing parquet footers, etc). The
    wrapper's job is only to wait — if it lets KeyboardInterrupt bubble
    out of ``proc.wait()`` the user sees a confusing traceback rooted in
    ``subprocess.py`` even though the real shutdown succeeded.
    """
    interrupted = False
    while True:
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            if not interrupted:
                print(
                    "\nReceived interrupt — waiting for ingestion runtime "
                    "to shut down gracefully...",
                    file=sys.stderr,
                )
                interrupted = True
            continue
        if interrupted:
            print("Shutdown complete.", file=sys.stderr)
        return rc


def run_uv_module(module: str, argv: list[str]) -> int:
    """Invoke ``uv run python -m <module> <argv>`` from the project root."""
    ensure_uv()
    cmd = ["uv", "run", "python", "-m", module, *argv]
    proc = subprocess.Popen(cmd, cwd=PYTHON_DIR)
    return _wait_graceful(proc)


def run_uv_entrypoint(entrypoint: str, argv: list[str]) -> int:
    """Invoke ``uv run <entrypoint> <argv>`` from the project root."""
    ensure_uv()
    cmd = ["uv", "run", entrypoint, *argv]
    proc = subprocess.Popen(cmd, cwd=PYTHON_DIR)
    return _wait_graceful(proc)
