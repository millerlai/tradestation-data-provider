"""
T2.5.E.2 — clear Tier 3 bar cache.

Wipes computed bars (provenance `derived:*`) under the requested timeframes
(default: everything but 1m). Run this after a BAR_SCHEMA change or any
resampler logic change — the next `HistoryStore.load_bars()` call will lazily
rebuild on demand.

**Provenance decides what goes, not the directory name.** Since the wire
started carrying `tf`, a native daily bar from TradeStation lives in
`timeframe=1d/` alongside derived ones — and a native daily carries the
exchange's official close and its split/dividend adjustment, neither of which
a tick rollup can reconstruct. Deleting the directory would replace real data
with a plausible-looking approximation, silently. So this tool reads the
`source` column and only removes bars this binding computed.

Safety: refuses to run without `--confirm`.

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
from pathlib import Path

from tradestation_data.domain.timeframe import NATIVE_ONLY_TIMEFRAMES, TIER3_TIMEFRAMES
from tradestation_data.storage.history_store import partition_holds_native

log = logging.getLogger("tradestation_data.tools.clear_bar_cache")

__all__ = ["TIER3_TIMEFRAMES", "main"]


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

    bars_root = args.data_root / "bars"
    if not bars_root.exists():
        log.warning("bars_root_not_found path=%s (nothing to do)", bars_root)
        return 0

    targets: list[Path] = []
    for tf in args.timeframes:
        if tf in NATIVE_ONLY_TIMEFRAMES:
            # Named explicitly, since it is no longer in the default list.
            # The provenance check below would spare these files anyway, but
            # saying so is better than a silent "0 deleted".
            log.warning(
                "skipping_native_only_timeframe tf=%s — published, not cache; "
                "it cannot be rebuilt, so it is never cleared",
                tf,
            )
            continue
        tf_dir = bars_root / f"timeframe={tf}"
        if tf_dir.exists() and tf_dir.is_dir():
            targets.append(tf_dir)

    if not targets:
        log.info("no_cache_dirs_found under %s for timeframes=%s", bars_root, args.timeframes)
        return 0

    derived: list[Path] = []
    native_kept = 0
    for tgt in targets:
        for p in sorted(tgt.rglob("bars.parquet")):
            if partition_holds_native(p):
                native_kept += 1
            else:
                derived.append(p)

    log.info(
        "scan_complete derived_files=%d native_files_kept=%d",
        len(derived),
        native_kept,
    )

    if not args.confirm:
        log.warning(
            "dry_run — %d derived file(s) would be removed, %d native kept. "
            "Pass --confirm to actually delete.",
            len(derived),
            native_kept,
        )
        return 0

    deleted = 0
    for f in derived:
        try:
            f.unlink()
            deleted += 1
        except OSError:
            log.exception("failed_to_remove path=%s", f)

    # Prune directories the deletions emptied. Deepest first, and only when
    # already empty, so a partition still holding a native bar keeps its path.
    for tgt in targets:
        for d in sorted(tgt.glob("symbol=*/date=*"), reverse=True):
            _rmdir_if_empty(d)
        for d in sorted(tgt.glob("symbol=*"), reverse=True):
            _rmdir_if_empty(d)
        _rmdir_if_empty(tgt)

    log.info("done files_removed=%d native_files_kept=%d", deleted, native_kept)
    return 0 if deleted == len(derived) else 1


def _rmdir_if_empty(path: Path) -> None:
    if not path.is_dir() or any(path.iterdir()):
        return
    try:
        path.rmdir()
    except OSError:
        log.exception("failed_to_remove_dir path=%s", path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
