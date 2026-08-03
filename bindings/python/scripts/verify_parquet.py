#!/usr/bin/env python3
"""Verify Hive-partitioned bar Parquet files for completeness.

Given a date range, timeframe, and session window, compute the expected
set of bar timestamps for each trading day and compare against what's
actually stored on disk. Reports files that are missing, files that are
incomplete, and the specific gaps within each day.

`--symbol` is optional: when omitted, every symbol discovered under
``<root>/bartype=<N>/interval=<M>/symbol=*/`` is verified and a cross-symbol
summary table is printed at the end.

TWO CAVEATS, BOTH DELIBERATE.

This is an operator's completeness check, not a guarantee about the data.
It answers "did every bar the session should have produced arrive", which
is a question about the collection run — nothing it reports changes what
is on disk, and it never writes.

It does NOT know about half days. The session window comes from
``--start-time`` / ``--end-time`` and applies to every day in range, so an
early close (the day after Thanksgiving, Christmas Eve) is reported
INCOMPLETE every time. ``--holidays`` only skips a day entirely; there is
no way to shorten one. Pass a matching ``--end-time`` for those dates, or
read the INCOMPLETE as expected.

Usage:
  # Single symbol, verbose per-day output
  python scripts/verify_parquet.py --symbol SPY \\
      --start-date 2026-03-20 --end-date 2026-04-17

  # All symbols under data/bars/bartype=1/interval=1/
  python scripts/verify_parquet.py \\
      --start-date 2026-03-20 --end-date 2026-04-17

  # All symbols, only print problem days
  python scripts/verify_parquet.py \\
      --start-date 2026-03-20 --end-date 2026-04-17 --only bad
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
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


def _parse_hhmm(s: str) -> time:
    parts = s.split(":")
    if len(parts) == 2:
        h, m = int(parts[0]), int(parts[1])
        return time(h, m)
    if len(parts) == 3:
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
        return time(h, m, sec)
    raise ValueError(f"bad HH:MM[:SS] value: {s!r}")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _resolve_tz(name: str) -> ZoneInfo:
    tz_name = _TZ_ALIASES.get(name.upper(), name)
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"unknown timezone: {name!r}") from e


def _validate_intraday_bar_args(bar_interval: int, bar_type: int) -> None:
    """Raise ValueError unless these args support an expected per-day grid.

    Shared by verify_parquet.py and imputation_parquet.py: both derive an
    expected intraday minute grid via `_expected_bars`, and neither supports
    anything else.
    """
    if bar_interval < 1:
        raise ValueError(f"--bar-interval must be >= 1, got {bar_interval}")
    if bar_type != 1:
        raise ValueError(
            f"--bar-type {bar_type} is not supported here. This tool "
            f"derives an expected per-day bar grid, which only exists for intraday "
            f"minute charts (BarType 1). A daily store (BarType 2) is FLAT -- one "
            f"file per symbol, no date= level -- so this tool's date-partitioned "
            f"paths would read every day as FILE_MISSING and its minute grid would "
            f"invent hundreds of 'missing' bars. A day with no daily bar is visible "
            f"as a missing row in bars.parquet itself."
        )


def _expected_bars(
    day: date,
    start: time,
    end: time,
    tf_sec: int,
    tz: ZoneInfo,
) -> list[datetime]:
    """Close-labelled bar times for a session. 1m 09:30-16:00 → [09:31..16:00].

    Matches ``BAR_SCHEMA.bar_time`` semantics: ``bar_time`` is EasyLanguage's
    ``Time``, the bar's CLOSE, landed verbatim (contract/semantics.md §2).
    A US RTH 09:30-16:00 session therefore stores 09:31 through 16:00 — the
    left-labelled 09:30..15:59 grid this function used to generate belonged
    to the deleted conversion, and against a verbatim store it reported one
    phantom missing bar and one unexpected bar every single day.
    """
    start_dt = datetime.combine(day, start).replace(tzinfo=tz)
    end_dt = datetime.combine(day, end).replace(tzinfo=tz)
    step = timedelta(seconds=tf_sec)
    out: list[datetime] = []
    t = start_dt + step
    while t <= end_dt:
        out.append(t.astimezone(UTC))
        t += step
    return out


@dataclass
class DayReport:
    day: date
    status: str  # OK / INCOMPLETE / FILE_MISSING / UNREADABLE / HOLIDAY / WEEKEND
    rows: int = 0
    expected: int = 0
    missing: list[datetime] = None  # UTC
    note: str = ""


def _cluster(missing: list[datetime], tf_sec: int) -> list[tuple[datetime, datetime, int]]:
    """Collapse contiguous missing timestamps into (start, end, count) runs."""
    if not missing:
        return []
    missing = sorted(missing)
    step = timedelta(seconds=tf_sec)
    runs: list[tuple[datetime, datetime, int]] = []
    run_start = run_end = missing[0]
    run_n = 1
    for t in missing[1:]:
        if t - run_end == step:
            run_end = t
            run_n += 1
        else:
            runs.append((run_start, run_end, run_n))
            run_start = run_end = t
            run_n = 1
    runs.append((run_start, run_end, run_n))
    return runs


def _fmt_range(
    runs: list[tuple[datetime, datetime, int]],
    tz: ZoneInfo,
    max_shown: int,
) -> str:
    if not runs:
        return ""

    def fmt(d: datetime) -> str:
        return d.astimezone(tz).strftime("%H:%M")

    pieces = []
    for s, e, n in runs[:max_shown]:
        if s == e:
            pieces.append(f"{fmt(s)} (1)")
        else:
            pieces.append(f"{fmt(s)}-{fmt(e)} ({n})")
    if len(runs) > max_shown:
        pieces.append(f"+{len(runs) - max_shown} more")
    return ", ".join(pieces)


def _load_day(path: Path) -> list[datetime] | None:
    """Return list of stored bar_time UTC datetimes; None if unreadable."""
    try:
        table = pq.read_table(path, columns=["bar_time"])
    except Exception:
        return None
    ts = table.column("bar_time").to_pylist()
    out: list[datetime] = []
    for t in ts:
        if t is None:
            continue
        t = t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
        out.append(t)
    return out


def verify(
    *,
    root: Path,
    symbol: str,
    start_date: date,
    end_date: date,
    bar_type: int,
    bar_interval: int,
    tf_sec: int,
    start_time: time,
    end_time: time,
    tz: ZoneInfo,
    holidays: set[date],
    include_weekends: bool,
) -> list[DayReport]:
    reports: list[DayReport] = []
    d = start_date
    while d <= end_date:
        if not include_weekends and d.weekday() >= 5:
            reports.append(DayReport(day=d, status="WEEKEND"))
            d += timedelta(days=1)
            continue
        if d in holidays:
            reports.append(DayReport(day=d, status="HOLIDAY"))
            d += timedelta(days=1)
            continue

        path = (
            root
            / f"bartype={bar_type}"
            / f"interval={bar_interval}"
            / f"symbol={symbol}"
            / f"date={d.isoformat()}"
            / "bars.parquet"
        )
        expected = _expected_bars(d, start_time, end_time, tf_sec, tz)
        exp_set = set(expected)

        if not path.exists():
            reports.append(
                DayReport(
                    day=d,
                    status="FILE_MISSING",
                    expected=len(expected),
                    missing=list(expected),
                )
            )
            d += timedelta(days=1)
            continue

        stored = _load_day(path)
        if stored is None:
            reports.append(
                DayReport(
                    day=d,
                    status="UNREADABLE",
                    expected=len(expected),
                    note="parquet read failed",
                )
            )
            d += timedelta(days=1)
            continue

        stored_set = set(stored)
        missing = sorted(exp_set - stored_set)
        status = "OK" if not missing else "INCOMPLETE"
        extras = len(stored_set - exp_set)
        note = ""
        if extras:
            note = f"{extras} extra bars outside session"
        reports.append(
            DayReport(
                day=d,
                status=status,
                rows=len(stored),
                expected=len(expected),
                missing=missing,
                note=note,
            )
        )
        d += timedelta(days=1)
    return reports


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--symbol",
        default=None,
        help=(
            "Symbol (e.g., SPY, $TICK). Quote $-prefixed on shells. "
            "Omit to verify every symbol found under <root>/bartype=<N>/interval=<M>/."
        ),
    )
    ap.add_argument("--start-date", required=True, type=_parse_date, help="Inclusive (YYYY-MM-DD).")
    ap.add_argument("--end-date", required=True, type=_parse_date, help="Inclusive (YYYY-MM-DD).")
    ap.add_argument(
        "--bar-type",
        type=int,
        default=1,
        help="EL BarType. Only 1 (intraday minutes) is supported here.",
    )
    ap.add_argument(
        "--bar-interval",
        type=int,
        default=1,
        help="EL BarInterval, verbatim. For BarType 1 this is the minutes.",
    )
    ap.add_argument(
        "--start-time", default="09:30", help="Session start HH:MM in --tz (default: 09:30)."
    )
    ap.add_argument(
        "--end-time", default="16:00", help="Session end HH:MM in --tz (default: 16:00)."
    )
    ap.add_argument("--tz", default="ET", help="Session timezone (default: ET).")
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "bars",
        help="Bars root (default: repo data/bars).",
    )
    ap.add_argument(
        "--holidays",
        default="",
        help="Comma-separated YYYY-MM-DD to skip (e.g., 2026-04-03 for Good Friday).",
    )
    ap.add_argument(
        "--include-weekends",
        action="store_true",
        help="Verify Sat/Sun too (for 24/7 markets); by default weekends are silently skipped.",
    )
    ap.add_argument(
        "--max-gap-runs",
        type=int,
        default=3,
        help="How many contiguous gap ranges to show per day (default: 3).",
    )
    ap.add_argument(
        "--only",
        choices=["all", "bad"],
        default="all",
        help="'bad' hides OK/HOLIDAY rows (default: all).",
    )
    args = ap.parse_args()

    try:
        _validate_intraday_bar_args(args.bar_interval, args.bar_type)
        tf_label = f"bartype={args.bar_type}/interval={args.bar_interval}"
        tf_sec = args.bar_interval * 60
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

    if args.symbol is not None:
        symbols = [args.symbol]
    else:
        symbols = _discover_symbols(args.root, args.bar_type, args.bar_interval)
        if not symbols:
            print(
                f"error: no symbols found under {args.root}/bartype={args.bar_type}/interval={args.bar_interval}/",
                file=sys.stderr,
            )
            return 2

    per_day_expected = len(_expected_bars(args.start_date, start_time, end_time, tf_sec, tz))
    print(
        f"range={args.start_date}..{args.end_date}  tf={tf_label}  "
        f"session={args.start_time}-{args.end_time} {args.tz}  "
        f"expected/day={per_day_expected}"
    )
    print(f"root={args.root}  symbols={len(symbols)}")
    print()

    overall_bad = 0
    overall_missing = 0
    cross_rows: list[tuple[str, dict[str, int], int]] = []
    multi = len(symbols) > 1

    for sym in symbols:
        reports = verify(
            root=args.root,
            symbol=sym,
            start_date=args.start_date,
            end_date=args.end_date,
            bar_type=args.bar_type,
            bar_interval=args.bar_interval,
            tf_sec=tf_sec,
            start_time=start_time,
            end_time=end_time,
            tz=tz,
            holidays=holidays,
            include_weekends=args.include_weekends,
        )
        counts, total_missing = _print_symbol_section(
            symbol=sym,
            reports=reports,
            tf_sec=tf_sec,
            tz=tz,
            tz_label=args.tz,
            only=args.only,
            max_gap_runs=args.max_gap_runs,
            multi=multi,
        )
        cross_rows.append((sym, counts, total_missing))
        overall_bad += (
            counts.get("INCOMPLETE", 0)
            + counts.get("FILE_MISSING", 0)
            + counts.get("UNREADABLE", 0)
        )
        overall_missing += total_missing

    if multi:
        _print_cross_summary(cross_rows)
        print(f"\noverall total_missing  {overall_missing}")

    return 0 if overall_bad == 0 else 1


def _discover_symbols(root: Path, bar_type: int, bar_interval: int) -> list[str]:
    base = root / f"bartype={bar_type}" / f"interval={bar_interval}"
    if not base.exists():
        return []
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if not child.name.startswith("symbol="):
            continue
        out.append(child.name[len("symbol=") :])
    return out


def _print_symbol_section(
    *,
    symbol: str,
    reports: list[DayReport],
    tf_sec: int,
    tz: ZoneInfo,
    tz_label: str,
    only: str,
    max_gap_runs: int,
    multi: bool,
) -> tuple[dict[str, int], int]:
    if multi:
        print(f"===== symbol={symbol} =====")
    header = f"{'Date':<11}  {'Status':<12}  {'Rows':>5}  {'Missing':>7}  Gaps ({tz_label})"
    print(header)
    print("-" * len(header))

    counts: dict[str, int] = {}
    total_missing = 0
    for r in reports:
        counts[r.status] = counts.get(r.status, 0) + 1
        if only == "bad" and r.status in ("OK", "HOLIDAY", "WEEKEND"):
            continue
        if r.status in ("HOLIDAY", "WEEKEND"):
            print(f"{r.day.isoformat():<11}  {r.status:<12}")
            continue
        if r.status == "FILE_MISSING":
            total_missing += r.expected
            print(
                f"{r.day.isoformat():<11}  {r.status:<12}  "
                f"{'-':>5}  {r.expected:>7}  (entire session)"
            )
            continue
        if r.status == "UNREADABLE":
            print(f"{r.day.isoformat():<11}  {r.status:<12}  {'-':>5}  {'-':>7}  {r.note}")
            continue
        miss_n = len(r.missing or [])
        total_missing += miss_n
        runs = _cluster(r.missing or [], tf_sec)
        gaps = _fmt_range(runs, tz, max_gap_runs)
        extra = f"  [{r.note}]" if r.note else ""
        print(f"{r.day.isoformat():<11}  {r.status:<12}  {r.rows:>5}  {miss_n:>7}  {gaps}{extra}")

    print()
    print("summary:")
    for k in ("OK", "INCOMPLETE", "FILE_MISSING", "UNREADABLE", "HOLIDAY", "WEEKEND"):
        if k in counts:
            print(f"  {k:<14} {counts[k]}")
    print(f"  total_missing  {total_missing}")
    print()
    return counts, total_missing


def _print_cross_summary(rows: list[tuple[str, dict[str, int], int]]) -> None:
    sym_w = max(8, max(len(s) for s, _, _ in rows))
    header = (
        f"{'Symbol':<{sym_w}}  "
        f"{'OK':>4}  {'INC':>4}  {'MISS':>4}  {'UNR':>4}  "
        f"{'TotalMiss':>10}  Status"
    )
    print("=" * len(header))
    print("CROSS-SYMBOL SUMMARY")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for sym, counts, total_missing in rows:
        ok = counts.get("OK", 0)
        inc = counts.get("INCOMPLETE", 0)
        miss = counts.get("FILE_MISSING", 0)
        unr = counts.get("UNREADABLE", 0)
        bad = inc + miss + unr
        if bad == 0:
            status = "clean"
        elif miss == ok + inc + miss:
            status = "ALL_MISSING"
        elif miss > 0 and inc == 0:
            status = f"{miss}d gap"
        elif inc > 0 and miss == 0:
            status = f"{inc}d incomplete"
        else:
            status = f"{miss}d gap + {inc}d incomplete"
        print(
            f"{sym:<{sym_w}}  {ok:>4}  {inc:>4}  {miss:>4}  {unr:>4}  {total_missing:>10}  {status}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
