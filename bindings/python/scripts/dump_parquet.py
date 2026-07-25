#!/usr/bin/env python3
"""Print the contents of a Parquet file.

Usage:
  python scripts/dump_parquet.py path/to/file.parquet
  python scripts/dump_parquet.py file.parquet --limit 20
  python scripts/dump_parquet.py file.parquet --columns bucket_start,close,volume
  python scripts/dump_parquet.py file.parquet --head 5 --tail 5
  python scripts/dump_parquet.py file.parquet --schema-only
  python scripts/dump_parquet.py file.parquet --where "bucket_start >= '2026-04-17T14:00'"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow.parquet as pq

_TZ_ALIASES = {
    "ET": "America/New_York",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "UTC": "UTC",
    "PT": "America/Los_Angeles",
    "CT": "America/Chicago",
    "TPE": "Asia/Taipei",
    "TW": "Asia/Taipei",
}


def _format_value(v: object, tz: ZoneInfo | None) -> str:
    if v is None:
        return ""
    if tz is not None and isinstance(v, datetime) and v.tzinfo is not None:
        v = v.astimezone(tz)
    return str(v)


def _print_rows(rows: list[dict], cols: list[str], tz: ZoneInfo | None) -> None:
    widths = {c: len(c) for c in cols}
    str_rows: list[dict] = []
    for r in rows:
        sr = {c: _format_value(r.get(c), tz) for c in cols}
        for c in cols:
            widths[c] = max(widths[c], len(sr[c]))
        str_rows.append(sr)
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  ".join("-" * widths[c] for c in cols))
    for sr in str_rows:
        print("  ".join(sr[c].ljust(widths[c]) for c in cols))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="Path to the parquet file.")
    ap.add_argument(
        "--columns",
        help="Comma-separated column list (default: all).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Print only the first N rows (0 = all). Ignored if --head/--tail given.",
    )
    ap.add_argument(
        "--head",
        type=int,
        default=0,
        help="Print the first N rows.",
    )
    ap.add_argument(
        "--tail",
        type=int,
        default=0,
        help="Print the last N rows (after --head).",
    )
    ap.add_argument(
        "--sort-by",
        help="Column to sort by before printing (use '-name' for descending).",
    )
    ap.add_argument(
        "--schema-only",
        action="store_true",
        help="Print schema + row count, skip the data.",
    )
    ap.add_argument(
        "--metadata",
        action="store_true",
        help="Also print file-level metadata (version, created_by, row groups).",
    )
    ap.add_argument(
        "--tz",
        default="ET",
        help=(
            "Display tz-aware timestamps converted to this zone. "
            "Accepts IANA names ('America/New_York', 'UTC') or shortcuts "
            "(ET, UTC, PT, CT, TPE). Default: ET. Use 'native' to keep the "
            "stored tz as-is."
        ),
    )
    args = ap.parse_args()

    tz: ZoneInfo | None = None
    if args.tz and args.tz.lower() != "native":
        tz_name = _TZ_ALIASES.get(args.tz.upper(), args.tz)
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            print(f"error: unknown timezone: {args.tz}", file=sys.stderr)
            return 2

    if not args.path.exists():
        print(f"error: file not found: {args.path}", file=sys.stderr)
        return 2
    if not args.path.is_file():
        print(f"error: not a file: {args.path}", file=sys.stderr)
        return 2

    pf = pq.ParquetFile(args.path)
    n_rows = pf.metadata.num_rows

    print(f"file        : {args.path}")
    print(f"rows        : {n_rows}")
    print(f"row groups  : {pf.num_row_groups}")
    print(f"columns     : {len(pf.schema_arrow.names)}")
    print()
    print("schema:")
    for field in pf.schema_arrow:
        nullable = "" if field.nullable else "  NOT NULL"
        print(f"  {field.name:<20} {field.type!s:<30}{nullable}")

    if args.metadata:
        md = pf.metadata
        print()
        print("metadata:")
        print(f"  format_version : {md.format_version}")
        print(f"  created_by     : {md.created_by}")
        print(f"  serialized_kb  : {md.serialized_size // 1024}")
        rg_count = pf.num_row_groups
        # For large row-group counts show only first/last few + stats so
        # the output stays readable (BarWriter currently writes one group
        # per bar, which can produce hundreds of groups per day).
        if rg_count <= 20:
            rg_indices: list[int] = list(range(rg_count))
        else:
            rg_indices = list(range(3)) + list(range(rg_count - 3, rg_count))
        total_rows = total_bytes = 0
        for i in range(rg_count):
            rg = md.row_group(i)
            total_rows += rg.num_rows
            total_bytes += rg.total_byte_size
        print(
            f"  row_groups     : {rg_count}  "
            f"(avg rows/group={total_rows / rg_count:.1f}  "
            f"avg bytes/group={total_bytes // rg_count})"
        )
        last_shown = -2
        for i in rg_indices:
            if i > last_shown + 1:
                print("  ...")
            rg = md.row_group(i)
            print(
                f"  row_group[{i}]: rows={rg.num_rows}  "
                f"bytes={rg.total_byte_size}  cols={rg.num_columns}"
            )
            last_shown = i

    if args.schema_only:
        return 0

    cols = [c.strip() for c in args.columns.split(",")] if args.columns else None
    if cols is not None:
        available = set(pf.schema_arrow.names)
        missing = [c for c in cols if c not in available]
        if missing:
            print(f"error: columns not in file: {missing}", file=sys.stderr)
            return 2

    table = pf.read(columns=cols)

    if args.sort_by:
        key = args.sort_by.lstrip("-")
        order = "descending" if args.sort_by.startswith("-") else "ascending"
        table = table.sort_by([(key, order)])

    rows = table.to_pylist()
    use_cols = cols if cols is not None else pf.schema_arrow.names

    head, tail = args.head, args.tail
    if head or tail:
        pieces: list[dict] = []
        if head:
            pieces.extend(rows[:head])
        if head and tail and (head + tail) < len(rows):
            pieces.append({c: "..." for c in use_cols})
        if tail:
            pieces.extend(rows[-tail:])
        print()
        _print_rows(pieces, use_cols, tz)
        return 0

    if args.limit and args.limit < len(rows):
        rows = rows[: args.limit]

    print()
    _print_rows(rows, use_cols, tz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
