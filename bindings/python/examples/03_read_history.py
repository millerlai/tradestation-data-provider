#!/usr/bin/env python3
"""Read stored data back: one tick store, many timeframes, on demand.

**Runs offline** — no TradeStation, no DLL. It fabricates a half-hour of
ticks, writes them exactly as the live runtime would, then reads bars back
out at several timeframes to show what the storage layer does for you.

    uv run python examples/03_read_history.py

The thing worth noticing: only the ticks are ever written by the collector.
`load_bars(..., "5m")` finds no 5-minute cache, resamples from the ticks,
persists the result, and returns it. Ask again and it is a cache hit. You
never run an aggregation step by hand.

    data/
      ticks/symbol=SPY/date=2026-04-20/ticks.parquet   <- Tier 1, written live
      bars/timeframe=1m/symbol=SPY/date=.../bars.parquet   <- Tier 2
      bars/timeframe=5m/symbol=SPY/date=.../bars.parquet   <- Tier 3, on demand

Bars are LEFT-labelled: `bucket_start` covers [t, t+step). A 09:30 five-minute
bar spans 09:30 to 09:35, and an RTH 1m session ends at 15:59, not 16:00
(contract/semantics.md §2). Grids anchor to the 09:30 ET session open rather
than to the Unix epoch, so a 1-hour bar starts at 09:30, not 09:00.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tradestation_data.domain.tick import Tick
from tradestation_data.storage.history_store import HistoryStore
from tradestation_data.storage.tick_writer import TickWriter

# 2026-04-20 09:30 ET. April is EDT (UTC-4), so the session opens at 13:30Z.
SESSION_OPEN = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
SYMBOL = "SPY"


def write_synthetic_ticks(root: Path, *, minutes: int = 30) -> int:
    """Write one tick every 10 seconds, as the live tick sink would."""
    price = 450.0
    written = 0
    with TickWriter(root / "ticks") as writer:
        for step in range(minutes * 6):
            # A slow drift plus a sawtooth, so the OHLC of each bar differs
            # and the rollups below are visibly not all the same number.
            price = 450.0 + step * 0.01 + (step % 6) * 0.05
            writer.write(
                Tick(
                    symbol=SYMBOL,
                    timestamp=SESSION_OPEN + timedelta(seconds=10 * step),
                    price=round(price, 2),
                    volume=100,
                    bid=round(price - 0.01, 2),
                    ask=round(price + 0.01, 2),
                    tick_count=1,
                    source="example",
                )
            )
            written += 1
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--root",
        type=Path,
        default=Path("data-example"),
        help="Where to generate the store. Must not already contain anything.",
    )
    p.add_argument("--keep", action="store_true", help="Leave the generated files behind.")
    args = p.parse_args()

    root: Path = args.root
    # Refuse to touch a directory that already holds something. This example
    # deletes its output on the way out, and the default sinks.yaml collects
    # real market data into ./data — one absent-minded `--root data` should
    # not be able to wipe it.
    if root.exists() and any(root.iterdir()):
        print(f"error: {root} is not empty — pass --root to point somewhere disposable")
        return 2

    n = write_synthetic_ticks(root)
    print(f"wrote {n} ticks to {root / 'ticks'}\n")

    store = HistoryStore(root)
    start = SESSION_OPEN - timedelta(minutes=1)
    end = SESSION_OPEN + timedelta(hours=1)

    for timeframe in ("1m", "5m", "15m"):
        # Cache miss the first time: resample from ticks, persist, return.
        # Every call after this one reads the parquet that just got written.
        bars = store.load_bars(SYMBOL, start, end, timeframe)
        first = bars.row(0, named=True)
        last = bars.row(-1, named=True)
        print(f"{timeframe:>3}: {bars.height:>2} bars   {first['source']}")
        print(
            f"     first  {first['bucket_start_et']:%H:%M} ET  "
            f"O={first['open']:.2f} H={first['high']:.2f} "
            f"L={first['low']:.2f} C={first['close']:.2f} vol={first['volume']}"
        )
        print(
            f"     last   {last['bucket_start_et']:%H:%M} ET  "
            f"O={last['open']:.2f} H={last['high']:.2f} "
            f"L={last['low']:.2f} C={last['close']:.2f} vol={last['volume']}"
        )

    # `source` is provenance, and the storage layer acts on it: a bar it
    # computed is stamped "derived:<origin>", and derived data is never
    # allowed to overwrite or delete a bar that arrived over the wire.
    # TradeStation's own daily bar carries the exchange's official close and
    # its split/dividend adjustment — a tick rollup cannot reconstruct
    # either, so the two must stay distinguishable on disk.
    print("\non-disk layout:")
    for path in sorted(root.rglob("*.parquet")):
        print(f"  {path.relative_to(root).as_posix()}")

    if not args.keep:
        shutil.rmtree(root)
        print(f"\nremoved {root} (pass --keep to inspect it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
