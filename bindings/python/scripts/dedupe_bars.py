#!/usr/bin/env python3
"""Drop duplicate (symbol, bar_time) rows from Hive-partitioned bar Parquet.

TradeStation's historical replay occasionally re-emits identical bars
after a chart reload. The ingestion runtime now de-duplicates live
(see IngestionRuntime._handle_provider_bar), but files captured before
that fix may still contain duplicate rows.

This script walks a bars root, rewrites any partition that has dups
keeping the first occurrence of each timestamp. Identical-OHLC dups
are lossless to remove.

**bartype=0 partitions are SKIPPED, and that is not an optimisation.**
A tick chart's rows legitimately share a bar_time: `ts_str` has minute
resolution, so every print inside one minute carries the same one and
only the `ts` column separates them. Deduping those on bar_time keeps
one print per minute and deletes the rest — hundreds of real trades per
minute, irreversibly, with a cheerful "rows removed" report. That is the
exact collapse the ingest path bypasses the intra-bar buffer to avoid;
an offline tool must not reintroduce it.

Usage:
  python scripts/dedupe_bars.py                  # default: ./data/bars
  python scripts/dedupe_bars.py --root D:\\data\\bars
  python scripts/dedupe_bars.py --dry-run        # report only
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq


def is_tick_partition(path: Path) -> bool:
    """Whether this file sits under bartype=0 — a chart whose bar_time repeats."""
    return any(part == "bartype=0" for part in path.parts)


def dedupe_file(path: Path, *, dry_run: bool) -> tuple[int, int]:
    """Return (rows_before, rows_after). No write if already unique."""
    table = pq.read_table(path)
    n_before = table.num_rows
    ts = table.column("bar_time").to_pylist()
    seen: set = set()
    keep_mask = []
    for t in ts:
        if t in seen:
            keep_mask.append(False)
        else:
            seen.add(t)
            keep_mask.append(True)
    n_after = sum(keep_mask)
    if n_after == n_before:
        return n_before, n_after
    if dry_run:
        return n_before, n_after
    deduped = table.filter(keep_mask)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(deduped, tmp, compression="zstd")
    tmp.replace(path)
    return n_before, n_after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path("data/bars"),
        help="Bars root dir (default: data/bars).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change but do not rewrite files.",
    )
    args = ap.parse_args()

    if not args.root.exists():
        print(f"error: root not found: {args.root}")
        return 2

    total_before = total_after = files_touched = 0
    skipped_tick = 0
    for pf in sorted(args.root.rglob("*.parquet")):
        if is_tick_partition(pf):
            # A tick chart's rows share a bar_time by design — see the module
            # docstring. Deduping them would delete real trades.
            skipped_tick += 1
            continue
        before, after = dedupe_file(pf, dry_run=args.dry_run)
        total_before += before
        total_after += after
        if before != after:
            files_touched += 1
            rel = pf.relative_to(args.root)
            verb = "would dedupe" if args.dry_run else "deduped"
            print(f"  {verb}: {rel}  {before} -> {after}  (-{before - after})")

    print()
    print(f"files scanned : {sum(1 for _ in args.root.rglob('*.parquet'))}")
    print(f"files touched : {files_touched}")
    print(f"rows before   : {total_before}")
    print(f"rows after    : {total_after}")
    print(f"rows removed  : {total_before - total_after}")
    if skipped_tick:
        print(
            f"tick files skipped : {skipped_tick}  "
            f"(bartype=0 rows share a bar_time by design; only `ts` separates them)"
        )
    if args.dry_run:
        print("(dry-run — no files written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
