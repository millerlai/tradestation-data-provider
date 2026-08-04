#!/usr/bin/env python3
"""Verify Hive-partitioned bar Parquet files for completeness.

Given a date range and a timeframe, learn which bar times this series
actually publishes, then compare each trading day against that. Reports
files that are missing, files that are incomplete, and the specific gaps
within each day.

`--symbol` is optional: when omitted, every symbol discovered under
``<root>/bartype=<N>/interval=<M>/symbol=*/`` is verified and a cross-symbol
summary table is printed at the end.

THE EXPECTED SET IS LEARNED, NOT COMPUTED.

It used to be a uniform ``start + n * interval`` grid, and that grid is
wrong: TradeStation restarts its intraday grid at the RTH open and close,
so a 60-minute chart on a 06:00-20:00 session publishes fifteen bars a day
including two stubs (09:00-09:30 and 15:30-16:00) — see
contract/semantics.md §2 for the measurement. Against a uniform grid those
two real bars read as "extra" and four grid positions that were never
published read as MISSING. imputation_parquet.py shares this module's
expected set, so there the same error wrote invented OHLC rows at
timestamps TradeStation never emitted.

No grid can fix it: the segment boundaries follow the chart's session
template, which the wire does not carry. So the expected set is derived
from the data instead — the times-of-day (in ``--tz``) carried by more than
half of the days actually on disk in range. A stub bar every day is
expected; a grid position that never appears is not.

TWO CONSEQUENCES.

It needs history. Fewer than ``--min-reference-days`` readable days in
range and it refuses rather than guessing, because a profile learned from
one day says only that that day matches itself.

Half days are still reported INCOMPLETE. An early close (the day after
Thanksgiving, Christmas Eve) is a minority of the range, so its shortened
afternoon does not enter the profile and the bars it never had are
reported missing. ``--holidays`` only skips a day entirely; there is no way
to shorten one. Read those INCOMPLETEs as expected.

This is an operator's completeness check, not a guarantee about the data.
It answers "did every bar this series normally produces arrive", which is a
question about the collection run — nothing it reports changes what is on
disk, and it never writes.

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
from collections import Counter
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


def _day_path(root: Path, bar_type: int, bar_interval: int, symbol: str, day: date) -> Path:
    """The one spelling of a day's bars file.

    ``date=`` always applies here: BarType 2 is stored flat, and
    ``_validate_intraday_bar_args`` has already refused it.
    """
    return (
        root
        / f"bartype={bar_type}"
        / f"interval={bar_interval}"
        / f"symbol={symbol}"
        / f"date={day.isoformat()}"
        / "bars.parquet"
    )


def _observed_profile(
    *,
    root: Path,
    bar_type: int,
    bar_interval: int,
    symbol: str,
    start_date: date,
    end_date: date,
    start: time,
    end: time,
    tz: ZoneInfo,
    holidays: set[date],
    include_weekends: bool,
    min_days: int,
) -> tuple[list[time], int]:
    """Learn which times-of-day this series publishes. Returns (profile, days).

    A time-of-day enters the profile when MORE THAN HALF the readable days in
    range carry it. Majority rather than union because one bad day — a half
    session, a collection run that died at noon — must not teach the profile
    times the series does not really have; majority rather than intersection
    because that same day must not un-teach the ones it does.

    Times-of-day rather than offsets from a session start: that is the form
    that survives DST, and ``start`` / ``end`` now only narrow the window
    afterwards. They no longer generate anything, so a session boundary the
    caller gets slightly wrong costs a filtered bar instead of a whole
    invented grid.
    """
    per_day: list[set[time]] = []
    d = start_date
    while d <= end_date:
        if (include_weekends or d.weekday() < 5) and d not in holidays:
            path = _day_path(root, bar_type, bar_interval, symbol, d)
            if path.exists():
                stored = _load_day(path)
                if stored:
                    tod = {t.astimezone(tz).time() for t in stored}
                    tod = {x for x in tod if start < x <= end}
                    if tod:
                        per_day.append(tod)
        d += timedelta(days=1)

    if len(per_day) < min_days:
        raise ValueError(
            f"{symbol}: only {len(per_day)} readable day(s) in range, need "
            f"{min_days}. Expected bar times are learned from the data, "
            f"because no uniform grid matches TradeStation's session-"
            f"restarting one (contract/semantics.md §2) — and a profile this "
            f"thin is one day agreeing with itself. Widen --start-date / "
            f"--end-date, or lower --min-reference-days to accept it."
        )

    counts: Counter[time] = Counter()
    for day_times in per_day:
        counts.update(day_times)
    n = len(per_day)
    return sorted(t for t, c in counts.items() if c * 2 > n), n


def _expected_bars(day: date, profile: list[time], tz: ZoneInfo) -> list[datetime]:
    """The learned profile placed on one calendar day, as UTC.

    ``bar_time`` is EasyLanguage's ``Time``, the bar's CLOSE, landed verbatim
    (contract/semantics.md §2), so these are close labels: a 1m RTH day is
    09:31 through 16:00, never 09:30 through 15:59.
    """
    return [datetime.combine(day, t, tzinfo=tz).astimezone(UTC) for t in profile]


@dataclass
class DayReport:
    day: date
    status: str  # OK / INCOMPLETE / FILE_MISSING / UNREADABLE / HOLIDAY / WEEKEND
    rows: int = 0
    expected: int = 0
    missing: list[datetime] = None  # UTC
    expected_ts: list[datetime] = None  # UTC, ordered — what _cluster needs
    note: str = ""


def _cluster(
    missing: list[datetime], expected: list[datetime]
) -> list[tuple[datetime, datetime, int]]:
    """Collapse missing timestamps adjacent IN THE EXPECTED SERIES into runs.

    Adjacency is by position, not by "one interval apart". The expected series
    is not a uniform grid — TradeStation restarts its intraday grid at the RTH
    open and close, so two consecutive expected bars can be a stub apart
    (contract/semantics.md §2) and a fixed step would report one gap as two.
    """
    if not missing:
        return []
    pos = {t: i for i, t in enumerate(expected)}
    ordered = sorted(missing)
    runs: list[tuple[datetime, datetime, int]] = []
    run_start = run_end = ordered[0]
    run_n = 1
    for t in ordered[1:]:
        if pos[t] == pos[run_end] + 1:
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
    profile: list[time],
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

        path = _day_path(root, bar_type, bar_interval, symbol, d)
        expected = _expected_bars(d, profile, tz)
        exp_set = set(expected)

        if not path.exists():
            reports.append(
                DayReport(
                    day=d,
                    status="FILE_MISSING",
                    expected=len(expected),
                    missing=list(expected),
                    expected_ts=expected,
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
                expected_ts=expected,
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
        "--start-time",
        default="09:30",
        help=(
            "Window start HH:MM in --tz (default: 09:30). Narrows the learned "
            "profile; exclusive, because bar_time is a close."
        ),
    )
    ap.add_argument(
        "--end-time",
        default="16:00",
        help="Window end HH:MM in --tz, inclusive (default: 16:00).",
    )
    ap.add_argument(
        "--min-reference-days",
        type=int,
        default=5,
        help=(
            "Readable days required before a learned profile is trusted "
            "(default: 5). Below this the run fails rather than guessing."
        ),
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

    print(
        f"range={args.start_date}..{args.end_date}  tf={tf_label}  "
        f"window={args.start_time}-{args.end_time} {args.tz}"
    )
    print(f"root={args.root}  symbols={len(symbols)}")
    print()

    overall_bad = 0
    overall_missing = 0
    cross_rows: list[tuple[str, dict[str, int], int]] = []
    multi = len(symbols) > 1

    for sym in symbols:
        try:
            profile, ref_days = _observed_profile(
                root=args.root,
                bar_type=args.bar_type,
                bar_interval=args.bar_interval,
                symbol=sym,
                start_date=args.start_date,
                end_date=args.end_date,
                start=start_time,
                end=end_time,
                tz=tz,
                holidays=holidays,
                include_weekends=args.include_weekends,
                min_days=args.min_reference_days,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            overall_bad += 1
            continue
        print(f"profile: {len(profile)} bars/day learned from {ref_days} day(s)  [{sym}]")
        reports = verify(
            root=args.root,
            symbol=sym,
            start_date=args.start_date,
            end_date=args.end_date,
            bar_type=args.bar_type,
            bar_interval=args.bar_interval,
            profile=profile,
            tz=tz,
            holidays=holidays,
            include_weekends=args.include_weekends,
        )
        counts, total_missing = _print_symbol_section(
            symbol=sym,
            reports=reports,
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
        runs = _cluster(r.missing or [], r.expected_ts or [])
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
