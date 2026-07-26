#!/usr/bin/env python3
"""Aggregate 1-minute bar Parquet into a coarser timeframe (5m … 1d).

Reads a Hive-partitioned 1-min bar cache laid out as:

  {input}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet

and writes the aggregated cache to:

  {output}/timeframe={TF}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet

Bars are **left-labelled and session-anchored**, exactly as
`contract/semantics.md` §2.2 defines and `storage.resampler` implements —
both write into the same `timeframe=` directories, so a second grid here
would not conflict loudly, it would just make every `bucket_start` join
match nothing. For N=5:

    09:30, 09:31, 09:32, 09:33, 09:34  ->  09:30
    09:35, 09:36, 09:37, 09:38, 09:39  ->  09:35

Missing minutes simply produce a chunk with fewer than N members; nothing
is fabricated.

Rules:
  bucket_start = the chunk's grid label (09:30..09:34 -> 09:30)
  open         = first bar's open
  close        = last bar's close
  high         = max(high)
  low          = min(low)
  volume       = sum(volume)
  tick_count   = sum(tick_count)
  source       = `derived:<input timeframe>` — these bars are computed, and
                 §2.3 forbids letting them pass for native ones

Usage:
  python scripts/aggregate_parquet.py --symbol SPY --timeframe 5m \\
      --input data/bars/timeframe=1m --output data/bars

  # Every symbol under --input (skips '$'-prefixed index symbols)
  python scripts/aggregate_parquet.py --symbol all --timeframe 5m \\
      --input data/bars/timeframe=1m --output data/bars

  # One specific trading date
  python scripts/aggregate_parquet.py --symbol SPY --timeframe 5m \\
      --input data/bars/timeframe=1m --output data/bars --date 2026-04-17
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.bar import derived_source
from tradestation_data.domain.timeframe import (
    NATIVE_ONLY_TIMEFRAMES,
    TIMEFRAME_MINUTES,
    align_bucket_start,
)

BAR_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("bucket_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.int64(), nullable=False),
        pa.field("tick_count", pa.int32(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)


def _tf_minutes(tf: str) -> int:
    if tf in NATIVE_ONLY_TIMEFRAMES:
        # Aggregating minutes into a daily bar produces something that looks
        # exactly like the published one and carries neither the exchange's
        # official close nor the split/dividend adjustment. Refusing here is
        # the whole point: once written, nothing downstream can tell.
        raise ValueError(
            f"{tf!r} is published, not derived: export it from TradeStation "
            "rather than aggregating it. See contract/semantics.md 2.3."
        )
    if tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Valid: {list(TIMEFRAME_MINUTES)}")
    return TIMEFRAME_MINUTES[tf]


def _detect_input_timeframe(input_root: Path) -> str:
    """Parse 'timeframe=Xm' from the trailing path component; default 1m."""
    m = re.fullmatch(r"timeframe=(\w+)", input_root.name)
    return m.group(1) if m else "1m"


def _aggregate_day(src_path: Path, out_tf: str) -> pa.Table:
    """Aggregate one symbol-date file of 1-min bars onto the `out_tf` grid."""
    table = pq.read_table(src_path)
    if table.num_rows == 0:
        return BAR_SCHEMA.empty_table()
    table = table.sort_by("bucket_start")

    bucket = table.column("bucket_start").to_pylist()
    open_ = table.column("open").to_pylist()
    high = table.column("high").to_pylist()
    low = table.column("low").to_pylist()
    close = table.column("close").to_pylist()
    volume = table.column("volume").to_pylist()
    tick_count = table.column("tick_count").to_pylist()

    out_bucket: list = []
    out_open: list = []
    out_high: list = []
    out_low: list = []
    out_close: list = []
    out_volume: list = []
    out_tick: list = []
    out_source: list = []

    # `{input}/symbol=SYM/date=YYYY-MM-DD/bars.parquet` -> the input root.
    in_tf = _detect_input_timeframe(src_path.parents[2])
    derived_src = derived_source(in_tf)

    def flush(label: datetime, start: int, end: int) -> None:
        out_bucket.append(label)
        out_open.append(open_[start])
        out_high.append(max(high[start:end]))
        out_low.append(min(low[start:end]))
        out_close.append(close[end - 1])
        out_volume.append(sum(volume[start:end]))
        out_tick.append(sum(tick_count[start:end]))
        out_source.append(derived_src)

    current_label: datetime | None = None
    chunk_start = 0
    for i, ts in enumerate(bucket):
        label = align_bucket_start(ts, out_tf)
        if current_label is None:
            current_label = label
            chunk_start = i
        elif label != current_label:
            flush(current_label, chunk_start, i)
            current_label = label
            chunk_start = i
    if current_label is not None:
        flush(current_label, chunk_start, len(bucket))

    return pa.Table.from_pydict(
        {
            "bucket_start": out_bucket,
            "open": out_open,
            "high": out_high,
            "low": out_low,
            "close": out_close,
            "volume": out_volume,
            "tick_count": out_tick,
            "source": out_source,
        },
        schema=BAR_SCHEMA,
    )


def _iter_symbol_dirs(input_root: Path, symbol: str) -> list[Path]:
    if symbol.lower() == "all":
        # Skip '$'-prefixed index symbols (VIX, TICK, VOLD, ...) — they
        # lack per-minute volume and are handled separately upstream.
        return sorted(
            p
            for p in input_root.glob("symbol=*")
            if p.is_dir() and not p.name.startswith("symbol=$")
        )
    p = input_root / f"symbol={symbol}"
    return [p] if p.is_dir() else []


def _iter_date_files(symbol_dir: Path, date_filter: str | None) -> list[Path]:
    if date_filter:
        f = symbol_dir / f"date={date_filter}" / "bars.parquet"
        return [f] if f.is_file() else []
    return sorted(symbol_dir.glob("date=*/bars.parquet"))


def _write(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--symbol", required=True, help="Symbol (e.g. SPY) or 'all'.")
    ap.add_argument(
        "--timeframe",
        required=True,
        help=f"Output timeframe. One of: {', '.join(TIMEFRAME_MINUTES)}.",
    )
    ap.add_argument("--input", type=Path, required=True, help="Input 1-min bars root.")
    ap.add_argument("--output", type=Path, required=True, help="Output bars root.")
    ap.add_argument("--date", help="Process only this date (YYYY-MM-DD).")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip (symbol, date) whose output file already exists.",
    )
    args = ap.parse_args()

    try:
        out_min = _tf_minutes(args.timeframe)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    in_tf = _detect_input_timeframe(args.input)
    in_min = _tf_minutes(in_tf)
    if out_min % in_min != 0:
        print(
            f"error: output timeframe {args.timeframe} is not a multiple "
            f"of input timeframe {in_tf}",
            file=sys.stderr,
        )
        return 2
    if in_min != 1:
        # The chunk-label math assumes 1-min input; generalising to N>1
        # would need a different anchoring rule.
        print(
            f"error: only 1m input is supported, got {in_tf}",
            file=sys.stderr,
        )
        return 2

    if not args.input.exists():
        print(f"error: input root not found: {args.input}", file=sys.stderr)
        return 2

    symbol_dirs = _iter_symbol_dirs(args.input, args.symbol)
    if not symbol_dirs:
        print(
            f"error: no matching symbol dirs under {args.input}",
            file=sys.stderr,
        )
        return 2

    total_days = total_in = total_out = 0
    skipped = 0
    for sdir in symbol_dirs:
        symbol = sdir.name.removeprefix("symbol=")
        files = _iter_date_files(sdir, args.date)
        if not files:
            continue
        for src in files:
            date_str = src.parent.name.removeprefix("date=")
            dst = (
                args.output
                / f"timeframe={args.timeframe}"
                / f"symbol={symbol}"
                / f"date={date_str}"
                / "bars.parquet"
            )
            if args.skip_existing and dst.is_file():
                skipped += 1
                continue
            agg = _aggregate_day(src, args.timeframe)
            _write(agg, dst)
            in_rows = pq.read_metadata(src).num_rows
            out_rows = agg.num_rows
            total_days += 1
            total_in += in_rows
            total_out += out_rows
            print(
                f"  {symbol:<10} {date_str}  {in_rows:4d} -> {out_rows:4d}  "
                f"{dst.relative_to(args.output)}"
            )

    print()
    print(f"days written : {total_days}")
    print(f"rows in      : {total_in}")
    print(f"rows out     : {total_out}")
    if skipped:
        print(f"days skipped : {skipped}  (--skip-existing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
