"""
T2.5.E.2 — clear Tier 3 bar cache.

Wipes `{data_root}/bars/timeframe=<tf>/...` for every Tier 3 timeframe
(5m / 15m / 30m / 1h / 1d), leaving the Tier 2 live 1-min cache alone.
Run this after a BAR_SCHEMA change or any resampler logic change — the
next `HistoryStore.load_bars()` call will lazily rebuild on demand.

Safety: refuses to run without `--confirm`. The 1-min cache is *never*
touched by this tool (that'd be data loss, not just cache eviction) —
use the audit script if you suspect 1m drift.

Example:
  # Dry run — list what would be deleted
  uv run python -m tradestation_data.tools.clear_bar_cache --data-root ./data

  # Actually delete
  uv run python -m tradestation_data.tools.clear_bar_cache --data-root ./data --confirm

  # Target a subset
  uv run python -m tradestation_data.tools.clear_bar_cache --data-root ./data \\
      --timeframes 5m 15m --confirm
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

log = logging.getLogger("tradestation_data.tools.clear_bar_cache")

TIER3_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h", "1d")
PROTECTED_TIMEFRAMES: frozenset[str] = frozenset({"1m"})


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clear_bar_cache", description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument(
        "--timeframes",
        nargs="+",
        default=list(TIER3_TIMEFRAMES),
        help=f"Timeframes to clear (default: {' '.join(TIER3_TIMEFRAMES)}).",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag, just lists the targets.",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Guardrail: reject any attempt to clear the 1m live cache.
    protected_requested = [tf for tf in args.timeframes if tf in PROTECTED_TIMEFRAMES]
    if protected_requested:
        log.error(
            "refusing to clear protected timeframes %s — Tier 2 live cache is not "
            "regenerable from this tool",
            protected_requested,
        )
        return 2

    bars_root = args.data_root / "bars"
    if not bars_root.exists():
        log.warning("bars_root_not_found path=%s (nothing to do)", bars_root)
        return 0

    targets: list[Path] = []
    for tf in args.timeframes:
        tf_dir = bars_root / f"timeframe={tf}"
        if tf_dir.exists() and tf_dir.is_dir():
            targets.append(tf_dir)

    if not targets:
        log.info("no_cache_dirs_found under %s for timeframes=%s", bars_root, args.timeframes)
        return 0

    total_files = 0
    for tgt in targets:
        files = list(tgt.rglob("*.parquet"))
        total_files += len(files)
        log.info("target path=%s parquet_files=%d", tgt, len(files))

    if not args.confirm:
        log.warning(
            "dry_run — %d file(s) across %d dir(s) would be removed. "
            "Pass --confirm to actually delete.",
            total_files,
            len(targets),
        )
        return 0

    deleted = 0
    for tgt in targets:
        try:
            shutil.rmtree(tgt)
            log.info("removed path=%s", tgt)
            deleted += 1
        except OSError:
            log.exception("failed_to_remove path=%s", tgt)

    log.info("done deleted_dirs=%d total_files_removed=%d", deleted, total_files)
    return 0 if deleted == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
