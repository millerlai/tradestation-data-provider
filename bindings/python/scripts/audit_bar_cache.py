#!/usr/bin/env python3
"""Run the weekly 1-min bar-cache audit (T2.5.E.1).

Thin wrapper around ``uv run python -m tradestation_data.tools.audit_bar_cache``.
Rebuilds 1-min bars from ticks and diffs against the live bar cache. Exits
non-zero if drift is detected — wire up in a scheduled task / cron weekly.

Examples:
  python scripts/audit_bar_cache.py
  python scripts/audit_bar_cache.py --symbols SPY,QQQ --days 3
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _common import PYTHON_DIR, REPO_ROOT, run_uv_module


def _load_symbols_from_yaml(path: Path) -> list[str]:
    """Extract the ``id:`` field from ``- { id: X, ... }`` lines without
    importing a YAML library (keeps the wrapper dep-free)."""
    pattern = re.compile(r"^\s*-\s*\{\s*id:\s*([^,}\s]+)")
    syms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            syms.append(m.group(1).strip())
    return syms


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbols", help="comma-separated symbols (default: config/symbols.yaml)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--data-root", default=None)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    data_root = args.data_root or str(REPO_ROOT / "data")

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        cfg = PYTHON_DIR / "config" / "symbols.yaml"
        symbols = _load_symbols_from_yaml(cfg) if cfg.exists() else []
        if not symbols:
            sys.exit(f"Could not auto-detect symbols from {cfg}. Pass --symbols explicitly.")

    fwd = [
        "--data-root",
        data_root,
        "--symbols",
        *symbols,
        "--days",
        str(args.days),
        "--log-level",
        args.log_level,
    ]
    return run_uv_module("tradestation_data.tools.audit_bar_cache", fwd)


if __name__ == "__main__":
    sys.exit(main())
