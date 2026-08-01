#!/usr/bin/env python3
"""Fill missing bars in Hive-partitioned bar Parquet files.

Shares the same date / session / timeframe arguments as verify_parquet.py.
For each trading day in range, detects which session bars are missing and
writes synthetic replacements using the chosen method:

  ffill       — use previous bar's close for o/h/l/c (flat, zero volume).
  bfill       — use next bar's open for o/h/l/c (flat, zero volume).
  interpolate — linear interp between prev.close and next.open.

Output goes to a SEPARATE root and carries an extra non-null ``imputed``
boolean column. Both are deliberate. The store under the ingest root is
what TradeStation actually said, and an imputed bar is a guess that looks
exactly like a real one once written; keeping it in a different tree
under a different schema means the two can never be confused, and a
reader that expects BAR_SCHEMA fails loudly rather than silently
consuming invented rows.

``--symbol`` is optional: when omitted, every symbol discovered under
``<root>/timeframe=<tf>/symbol=*/`` is processed. Nothing under ``--root``
is ever modified.

Usage:
  # Single symbol
  python scripts/imputation_parquet.py --symbol SPY \\
      --start-date 2026-03-20 --end-date 2026-04-17 --method ffill

  # All symbols, dry-run first
  python scripts/imputation_parquet.py \\
      --start-date 2026-03-20 --end-date 2026-04-17 --dry-run

  # All symbols, real run after dry-run looks fine
  python scripts/imputation_parquet.py \\
      --start-date 2026-03-20 --end-date 2026-04-17

  python scripts/imputation_parquet.py --symbol VXX \\
      --start-date 2026-04-16 --end-date 2026-04-16 --method interpolate \\
      --start-time 09:30 --end-time 13:00
"""

from __future__ import annotations

import argparse
import bisect
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_parquet import (
    _cluster,
    _discover_symbols,
    _expected_bars,
    _fmt_range,
    _parse_date,
    _parse_hhmm,
    _parse_timeframe,
    _resolve_tz,
)

METHODS = ("ffill", "bfill", "interpolate")

_ET_TZ = ZoneInfo("America/New_York")

# The output schema: whatever the source file had, plus a non-null `imputed`
# flag. Deliberately NOT BAR_SCHEMA — a reader casting to BAR_SCHEMA must
# fail on this file rather than quietly accept invented rows, and the extra
# column is what guarantees that.
_IMPUTED_FIELD = pa.field("imputed", pa.bool_(), nullable=False)


def _output_schema(source: pa.Schema) -> pa.Schema:
    return source.append(_IMPUTED_FIELD)


def _passthrough_table(path: Path) -> pa.Table:
    """One source day, rows unchanged, with `imputed` = False appended.

    A day that needed no imputation still has to reach --output. Skipping it
    would make the output a *delta* against --root, while its own --help calls
    it a copy: a caller pointing HistoryStore at it would get zero rows for
    every complete day and no error, because an empty range is an ordinary
    answer (semantics.md §2.4).

    The column is appended rather than the file copied byte-for-byte so every
    file under --output carries one schema. A tree mixing 11-column and
    12-column files puts the reader back on the schema-drift trap this
    protocol exists to remove.
    """
    table = pq.read_table(path)
    flags = pa.array([False] * table.num_rows, type=pa.bool_())
    return table.append_column(_IMPUTED_FIELD, flags)


def _build_imputed_row(bucket_start: datetime, value: float) -> dict:
    # Every quantity is 0: this bar records no trading, because none was
    # observed. Carrying a neighbour's volume forward would invent activity
    # on top of inventing a price.
    return {
        "bucket_start": bucket_start,
        "bucket_start_et": bucket_start.astimezone(_ET_TZ),
        "open": value,
        "high": value,
        "low": value,
        "close": value,
        "el_volume": 0,
        "el_ticks": 0,
        "el_upticks": 0,
        "el_downticks": 0,
        "el_open_interest": 0,
        "imputed": True,
    }


def _impute_value(
    missing_ts: datetime,
    prev: dict | None,
    nxt: dict | None,
    method: str,
) -> tuple[float, str] | None:
    """Return (value, fallback_note) or None if no reference exists."""
    if method == "ffill":
        if prev is not None:
            return prev["close"], ""
        if nxt is not None:
            return nxt["open"], "no_prev→bfill"
        return None
    if method == "bfill":
        if nxt is not None:
            return nxt["open"], ""
        if prev is not None:
            return prev["close"], "no_next→ffill"
        return None
    if method == "interpolate":
        if prev is not None and nxt is not None:
            total = (nxt["bucket_start"] - prev["bucket_start"]).total_seconds()
            elapsed = (missing_ts - prev["bucket_start"]).total_seconds()
            frac = elapsed / total if total > 0 else 0.0
            return prev["close"] + frac * (nxt["open"] - prev["close"]), ""
        if prev is not None:
            return prev["close"], "no_next→ffill"
        if nxt is not None:
            return nxt["open"], "no_prev→bfill"
        return None
    raise ValueError(f"unknown method: {method!r}")


def impute_day(
    path: Path,
    expected_utc: list[datetime],
    method: str,
    tf_sec: int,
    display_tz: ZoneInfo,
) -> tuple[int, int, list[tuple[datetime, str]], pa.Table | None]:
    """Return (rows_before, rows_added, per_row_log, new_table_or_None).

    new_table is None when nothing needed imputation.
    """
    table = pq.read_table(path)
    rows = table.to_pylist()
    for r in rows:
        ts = r["bucket_start"]
        if ts.tzinfo is None:
            r["bucket_start"] = ts.replace(tzinfo=UTC)
        else:
            r["bucket_start"] = ts.astimezone(UTC)
        # Rows that came off the wire. Flagged explicitly rather than left
        # null so "real" and "invented" are never a missing-value question.
        r["imputed"] = False

    by_ts = {r["bucket_start"]: r for r in rows}
    missing = [t for t in expected_utc if t not in by_ts]
    if not missing:
        return len(rows), 0, [], None

    sorted_rows = sorted(rows, key=lambda r: r["bucket_start"])
    existing_ts = [r["bucket_start"] for r in sorted_rows]

    new_rows: list[dict] = []
    log: list[tuple[datetime, str]] = []
    for t in missing:
        idx = bisect.bisect_left(existing_ts, t)
        prev = sorted_rows[idx - 1] if idx > 0 else None
        nxt = sorted_rows[idx] if idx < len(sorted_rows) else None
        result = _impute_value(t, prev, nxt, method)
        if result is None:
            log.append((t, "SKIP_no_reference"))
            continue
        value, fallback = result
        new_rows.append(_build_imputed_row(t, value))
        log.append((t, fallback or method))

    if not new_rows:
        return len(rows), 0, log, None

    all_rows = rows + new_rows
    all_rows.sort(key=lambda r: r["bucket_start"])
    new_table = pa.Table.from_pylist(all_rows, schema=_output_schema(table.schema))
    return len(rows), len(new_rows), log, new_table


def _write_atomic(path: Path, table: pa.Table) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--symbol",
        default=None,
        help=(
            "Symbol (e.g., SPY, $TICK). Quote $-prefixed on shells. "
            "Omit to impute every symbol found under <root>/timeframe=<tf>/."
        ),
    )
    ap.add_argument("--start-date", required=True, type=_parse_date)
    ap.add_argument("--end-date", required=True, type=_parse_date)
    ap.add_argument("--timeframe", "--tf", default="1m")
    ap.add_argument("--start-time", default="09:30")
    ap.add_argument("--end-time", default="16:00")
    ap.add_argument("--tz", default="ET")
    ap.add_argument(
        "--method",
        choices=METHODS,
        default="ffill",
        help="Imputation method (default: ffill).",
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "bars",
    )
    ap.add_argument(
        "--holidays",
        default="",
        help="Comma-separated YYYY-MM-DD to skip.",
    )
    ap.add_argument("--include-weekends", action="store_true")
    ap.add_argument(
        "--output",
        required=True,
        type=Path,
        help=(
            "Destination root for the imputed copy. REQUIRED, and must not be "
            "--root: imputed bars are guesses that look exactly like real ones "
            "once written, so they are kept in a separate tree under a schema "
            "with an extra `imputed` column. Every day in range is written, "
            "not only the ones that needed filling — the result is a complete "
            "store you can read directly, not a delta against --root. Days "
            "with no source file are the one exception and are reported as "
            "FILE_MISSING."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change but do not write files.",
    )
    ap.add_argument("--max-gap-runs", type=int, default=3)
    args = ap.parse_args()

    try:
        tf_label, tf_sec = _parse_timeframe(args.timeframe)
        start_time = _parse_hhmm(args.start_time)
        end_time = _parse_hhmm(args.end_time)
        tz = _resolve_tz(args.tz)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.end_date < args.start_date:
        print("error: --end-date before --start-date", file=sys.stderr)
        return 2
    if end_time <= start_time:
        print("error: --end-time must be after --start-time", file=sys.stderr)
        return 2
    if not args.root.exists():
        print(f"error: root not found: {args.root}", file=sys.stderr)
        return 2

    holidays: set[date] = set()
    for h in filter(None, (s.strip() for s in args.holidays.split(","))):
        try:
            holidays.add(_parse_date(h))
        except ValueError:
            print(f"error: bad holiday date: {h!r}", file=sys.stderr)
            return 2

    if args.output.resolve() == args.root.resolve():
        print("error: --output must differ from --root", file=sys.stderr)
        return 2
    per_day_expected = len(_expected_bars(args.start_date, start_time, end_time, tf_sec, tz))

    if args.symbol is not None:
        symbols = [args.symbol]
    else:
        symbols = _discover_symbols(args.root, tf_label)
        if not symbols:
            print(
                f"error: no symbols found under {args.root}/timeframe={tf_label}/",
                file=sys.stderr,
            )
            return 2

    print(
        f"range={args.start_date}..{args.end_date}  tf={tf_label}  "
        f"session={args.start_time}-{args.end_time} {args.tz}  "
        f"method={args.method}  expected/day={per_day_expected}"
    )
    print(f"root={args.root}  output={args.output}  dry_run={args.dry_run}  symbols={len(symbols)}")
    print()

    multi = len(symbols) > 1
    cross_rows: list[tuple[str, dict[str, int]]] = []
    overall_imputed = overall_skipped = overall_touched = 0

    for sym in symbols:
        if multi:
            print(f"===== symbol={sym} =====")
        stats = _impute_one_symbol(
            symbol=sym,
            start_date=args.start_date,
            end_date=args.end_date,
            tf_label=tf_label,
            tf_sec=tf_sec,
            start_time=start_time,
            end_time=end_time,
            tz=tz,
            tz_label=args.tz,
            method=args.method,
            root=args.root,
            output=args.output,
            holidays=holidays,
            include_weekends=args.include_weekends,
            dry_run=args.dry_run,
            max_gap_runs=args.max_gap_runs,
        )
        cross_rows.append((sym, stats))
        overall_touched += stats["files_touched"]
        overall_imputed += stats["rows_imputed"]
        overall_skipped += stats["rows_skipped"]

    if multi:
        _print_cross_summary(
            cross_rows,
            dry_run=args.dry_run,
            overall_touched=overall_touched,
            overall_imputed=overall_imputed,
            overall_skipped=overall_skipped,
        )

    return 0


def _impute_one_symbol(
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    tf_label: str,
    tf_sec: int,
    start_time: time,
    end_time: time,
    tz: ZoneInfo,
    tz_label: str,
    method: str,
    root: Path,
    output: Path,
    holidays: set[date],
    include_weekends: bool,
    dry_run: bool,
    max_gap_runs: int,
) -> dict[str, int]:
    header = (
        f"{'Date':<11}  {'Status':<14}  {'Rows':>5}  {'+Imp':>5}  {'Skip':>5}  Gaps ({tz_label})"
    )
    print(header)
    print("-" * len(header))

    touched = imputed_total = skipped_total = 0
    file_missing = holiday_n = weekend_n = no_change = 0
    d = start_date
    while d <= end_date:
        if not include_weekends and d.weekday() >= 5:
            weekend_n += 1
            d += timedelta(days=1)
            continue
        if d in holidays:
            holiday_n += 1
            d += timedelta(days=1)
            continue

        path = (
            root
            / f"timeframe={tf_label}"
            / f"symbol={symbol}"
            / f"date={d.isoformat()}"
            / "bars.parquet"
        )
        expected = _expected_bars(d, start_time, end_time, tf_sec, tz)

        if not path.exists():
            file_missing += 1
            print(
                f"{d.isoformat():<11}  {'FILE_MISSING':<14}  {'-':>5}  {'-':>5}  {'-':>5}  (skipped)"
            )
            d += timedelta(days=1)
            continue

        try:
            before, added, log, new_table = impute_day(path, expected, method, tf_sec, tz)
        except Exception as e:
            print(f"{d.isoformat():<11}  {'ERROR':<14}  -     -     -     {e}")
            d += timedelta(days=1)
            continue

        skipped = sum(1 for _, note in log if note == "SKIP_no_reference")
        if new_table is None:
            # Complete day: still copy it, or --output is a delta rather than
            # the "imputed copy" its --help promises. See _passthrough_table.
            no_change += 1
            if not dry_run:
                out_path = output / path.relative_to(root)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                _write_atomic(out_path, _passthrough_table(path))
                status = "COPIED"
            else:
                status = "WOULD_COPY"
            print(f"{d.isoformat():<11}  {status:<14}  {before:>5}  {0:>5}  {0:>5}")
            d += timedelta(days=1)
            continue

        imputed_ts = [t for t, note in log if note != "SKIP_no_reference"]
        runs = _cluster(imputed_ts, tf_sec)
        gaps = _fmt_range(runs, tz, max_gap_runs)

        if not dry_run:
            out_path = output / path.relative_to(root)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(out_path, new_table)
            status = "WRITTEN"
        else:
            status = "WOULD_IMPUTE"

        touched += 1
        imputed_total += added
        skipped_total += skipped
        print(f"{d.isoformat():<11}  {status:<14}  {before:>5}  {added:>5}  {skipped:>5}  {gaps}")
        d += timedelta(days=1)

    print()
    print("summary:")
    print(f"  files_touched   {touched}")
    print(f"  rows_imputed    {imputed_total}")
    print(f"  rows_skipped    {skipped_total}  (no reference bar available)")
    print(f"  copied_intact   {no_change}  (complete already; copied so --output is whole)")
    print(f"  file_missing    {file_missing}")
    print(f"  holiday         {holiday_n}")
    print(f"  weekend         {weekend_n}")
    if dry_run:
        print("(dry-run — no files written)")
    print()

    return {
        "files_touched": touched,
        "rows_imputed": imputed_total,
        "rows_skipped": skipped_total,
        "already_ok": no_change,
        "file_missing": file_missing,
        "holiday": holiday_n,
        "weekend": weekend_n,
    }


def _print_cross_summary(
    rows: list[tuple[str, dict[str, int]]],
    *,
    dry_run: bool,
    overall_touched: int,
    overall_imputed: int,
    overall_skipped: int,
) -> None:
    sym_w = max(8, max(len(s) for s, _ in rows))
    header = (
        f"{'Symbol':<{sym_w}}  "
        f"{'Touched':>7}  {'+Imp':>5}  {'Skip':>5}  "
        f"{'OK':>4}  {'Miss':>5}  {'Hol':>4}  {'Wknd':>5}"
    )
    print("=" * len(header))
    print(f"CROSS-SYMBOL SUMMARY{'  (dry-run)' if dry_run else ''}")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for sym, s in rows:
        print(
            f"{sym:<{sym_w}}  "
            f"{s['files_touched']:>7}  {s['rows_imputed']:>5}  {s['rows_skipped']:>5}  "
            f"{s['already_ok']:>4}  {s['file_missing']:>5}  {s['holiday']:>4}  {s['weekend']:>5}"
        )
    print()
    print(
        f"overall: touched={overall_touched}  imputed={overall_imputed}  skipped={overall_skipped}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
