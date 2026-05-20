#!/usr/bin/env python3
"""Clear the Tier 3 bar cache (5m / 15m / 30m / 1h / 1d) (T2.5.E.2).

Thin wrapper around ``uv run python -m tradestation_data.tools.clear_bar_cache``.
The 1-min live cache is hard-guarded and cannot be cleared with this tool.

Examples:
  # Dry run
  python scripts/clear_bar_cache.py

  # Clear 5m + 15m only
  python scripts/clear_bar_cache.py --timeframes 5m,15m --confirm
"""

from __future__ import annotations

import argparse
import sys

from _common import REPO_ROOT, run_uv_module


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--timeframes",
        default="5m,15m,30m,1h,1d",
        help="comma-separated subset of {5m,15m,30m,1h,1d}",
    )
    p.add_argument("--data-root", default=None)
    p.add_argument("--confirm", action="store_true", help="actually delete (else dry run)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    data_root = args.data_root or str(REPO_ROOT / "data")
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    fwd = [
        "--data-root",
        data_root,
        "--timeframes",
        *timeframes,
        "--log-level",
        args.log_level,
    ]
    if args.confirm:
        fwd.append("--confirm")

    return run_uv_module("tradestation_data.tools.clear_bar_cache", fwd)


if __name__ == "__main__":
    sys.exit(main())
