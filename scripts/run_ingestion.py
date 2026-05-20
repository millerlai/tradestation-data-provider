#!/usr/bin/env python3
"""Launch the Python ingestion runtime (TradeStation EL → Parquet).

Thin wrapper around ``uv run tradestation-data-ingest``. Ensures we invoke uv
from the ``python/`` sub-project (so uv picks up pyproject.toml + .venv),
while writing data out to the repo root ``data/`` tree by default.

The ingestion runtime connects to the C++ DLL's ZeroMQ PUB socket
(default tcp://127.0.0.1:5555), subscribes to every symbol listed in
``config/symbols.yaml``, aggregates 1-min bars, and persists ticks + bars
to Hive-partitioned Parquet.

Examples:
  # Standard run — reads config/symbols.yaml, writes to data/
  python scripts/run_ingestion.py

  # Smoke test against a local stub publisher, no writes
  python scripts/run_ingestion.py --endpoint tcp://127.0.0.1:5556 --no-storage --log-level DEBUG

  # Production-style JSON logs + alt data root
  python scripts/run_ingestion.py --log-json --data-root D:\\trading\\data
"""

from __future__ import annotations

import argparse
import sys

from _common import PYTHON_DIR, REPO_ROOT, run_uv_entrypoint


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", help="path to symbols.yaml")
    p.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    p.add_argument("--data-root", default=None)
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    p.add_argument("--log-json", action="store_true")
    p.add_argument("--heartbeat-seconds", type=float, default=60)
    p.add_argument("--no-storage", action="store_true")
    p.add_argument(
        "--print-bars",
        type=int,
        default=0,
        metavar="N",
        help="Print the first N bars (schema + row values) as they are "
        "written to bars.parquet. Useful for verifying the on-disk format "
        "mid-run. 0 = disabled (default).",
    )
    args = p.parse_args()

    if not PYTHON_DIR.exists():
        sys.exit(f"Python project dir not found: {PYTHON_DIR}")

    if args.config:
        config = args.config
    else:
        root_cfg = REPO_ROOT / "config" / "symbols.yaml"
        py_cfg = PYTHON_DIR / "config" / "symbols.yaml"
        if root_cfg.exists():
            config = str(root_cfg)
        elif py_cfg.exists():
            config = str(py_cfg)
        else:
            sys.exit(
                f"symbols.yaml not found under {root_cfg} or {py_cfg}. Pass --config explicitly."
            )

    data_root = args.data_root or str(REPO_ROOT / "data")

    fwd = [
        "--config",
        config,
        "--endpoint",
        args.endpoint,
        "--data-root",
        data_root,
        "--log-level",
        args.log_level,
        "--heartbeat-seconds",
        str(args.heartbeat_seconds),
    ]
    if args.log_json:
        fwd.append("--log-json")
    if args.no_storage:
        fwd.append("--no-storage")
    if args.print_bars > 0:
        fwd.extend(["--print-bars", str(args.print_bars)])

    print(f"repoRoot   : {REPO_ROOT}")
    print(f"pythonDir  : {PYTHON_DIR}")
    print(f"config     : {config}")
    print(f"endpoint   : {args.endpoint}")
    print(f"dataRoot   : {data_root}")
    print(f"logLevel   : {args.log_level}")
    print()

    return run_uv_entrypoint("tradestation-data-ingest", fwd)


if __name__ == "__main__":
    sys.exit(main())
