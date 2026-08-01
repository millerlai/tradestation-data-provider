#!/usr/bin/env python3
"""Read stored data back, and see what the read API does and does not do.

**Runs offline** — no TradeStation, no DLL. It fabricates a session's worth
of bars and ticks, writes them exactly as the live runtime would, and reads
them back.

    uv run python examples/03_read_history.py

The thing worth noticing is what is NOT here. `load_bars` reads Parquet and
nothing else: there is no cache to miss, no resampling, no backfill. Ask for
a timeframe the collector never recorded and you get zero rows, not a
computed substitute — because a bar assembled here would be indistinguishable
from one TradeStation published the moment it hit disk, and you would have no
way to tell later which you were looking at. Building 5-minute bars out of
1-minute ones is a fine thing to do; it is just yours to do, with your own
rules, downstream of this package.

    data/
      ticks/symbol=SPY/date=2026-04-20/ticks.parquet
      bars/timeframe=1m/symbol=SPY/date=2026-04-20/bars.parquet
      bars/timeframe=1d/symbol=SPY/bars.parquet     <- one file, no date=

Bars are LEFT-labelled: `bucket_start` covers [t, t+step). A 09:30 bar spans
09:30 to 09:31, and an RTH 1m session ends at 15:59, not 16:00
(contract/semantics.md §2).

Times are Eastern. This is a US-equity store, so a bare `datetime(...)` passed
to `load_bars` / `load_ticks` means `America/New_York` (§2.3) — you never do
offset arithmetic to ask a question. Every frame still carries both views,
`bucket_start` in UTC beside `bucket_start_et`.

An *event* timestamp is different: `Tick.timestamp` is an absolute instant, so
give it a timezone-aware value. Only the query bounds have a default.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.storage.bar_writer import BarWriter
from tradestation_data.storage.history_store import HistoryStore
from tradestation_data.storage.tick_writer import TickWriter

ET = ZoneInfo("America/New_York")
# The session open, said the way a US-equity desk says it.
SESSION_OPEN = datetime(2026, 4, 20, 9, 30, tzinfo=ET)
SYMBOL = "SPY"


def write_synthetic_store(root: Path, *, minutes: int = 30) -> tuple[int, int]:
    """Write bars and ticks the way the live sinks would.

    The quantities are shaped like real intraday data: EasyLanguage's
    `Volume` is the up-tick share volume (so it equals `UpTicks`) and
    `Ticks` is the total (so it equals up plus down). Both go to disk
    verbatim under their own names — see contract/semantics.md §3.4 for why
    neither is called "volume".
    """
    price = 450.0
    bars = ticks = 0

    with BarWriter(root / "bars") as bar_writer, TickWriter(root / "ticks") as tick_writer:
        for step in range(minutes):
            bucket = SESSION_OPEN + timedelta(minutes=step)
            open_ = round(price, 2)
            price += 0.03 + (step % 5) * 0.02
            close = round(price, 2)
            up, down = 6_000 + step * 10, 4_000 + step * 5
            bar_writer.write(
                Bar(
                    symbol=SYMBOL,
                    bucket_start=bucket,
                    open=open_,
                    high=round(max(open_, close) + 0.04, 2),
                    low=round(min(open_, close) - 0.03, 2),
                    close=close,
                    el_volume=up,          # EL `Volume`  — up-tick shares
                    el_ticks=up + down,    # EL `Ticks`   — total shares
                    el_upticks=up,
                    el_downticks=down,
                    el_open_interest=0,    # 0 on equities; futures only
                    timeframe="1m",
                )
            )
            bars += 1

            # A couple of prints inside the minute, as the tick sink stores them.
            for sub in (0, 30):
                tick_writer.write(
                    Tick(
                        symbol=SYMBOL,
                        timestamp=bucket + timedelta(seconds=sub),
                        price=close,
                        el_volume=up // 2,
                        el_ticks=(up + down) // 2,
                        el_upticks=up // 2,
                        el_downticks=down // 2,
                        el_open_interest=0,
                        bid=round(close - 0.01, 2),
                        ask=round(close + 0.01, 2),
                    )
                )
                ticks += 1

        # One daily bar. Daily lives in a flat single-file layout, and on a
        # daily chart EasyLanguage's words mean the opposite of the above:
        # `Volume` is total shares and `Ticks` is a trade count. Still
        # verbatim — the inversion is a table the consumer reads, not
        # something any layer here reconciles.
        bar_writer.write(
            Bar(
                symbol=SYMBOL,
                bucket_start=datetime(2026, 4, 20, 4, 0, tzinfo=ET),
                open=450.0,
                high=round(price + 0.5, 2),
                low=449.5,
                close=round(price, 2),
                el_volume=55_437_545,
                el_ticks=612_004,
                el_upticks=55_437_545,
                el_downticks=0,
                el_open_interest=0,
                timeframe="1d",
            )
        )
        bars += 1

    return bars, ticks


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
        print(f"error: {root} is not empty - pass --root to point somewhere disposable")
        return 2

    n_bars, n_ticks = write_synthetic_store(root)
    print(f"wrote {n_bars} bars and {n_ticks} ticks under {root}\n")

    store = HistoryStore(root)
    # Bare datetimes, no tzinfo and no offset arithmetic: on the read API these
    # are Eastern, because that is the only clock this data has ever been in.
    start = datetime(2026, 4, 20, 9, 29)
    end = datetime(2026, 4, 20, 10, 30)

    bars = store.load_bars(SYMBOL, start, end, "1m")
    first, last = bars.row(0, named=True), bars.row(-1, named=True)
    print(f" 1m: {bars.height} bars")
    print(
        f"     first  {first['bucket_start_et']:%H:%M} ET  "
        f"O={first['open']:.2f} H={first['high']:.2f} "
        f"L={first['low']:.2f} C={first['close']:.2f}"
    )
    print(
        f"            el_volume={first['el_volume']} (up-tick shares)  "
        f"el_ticks={first['el_ticks']} (total shares)"
    )
    print(f"     last   {last['bucket_start_et']:%H:%M} ET  C={last['close']:.2f}")

    # A daily bar is anchored at 04:00 ET, the start of the extended session
    # — so the 09:29-10:30 window above does not contain it. Ask for the day.
    day_start = datetime(2026, 4, 20, 0, 0)
    day_end = datetime(2026, 4, 20, 23, 59)
    daily = store.load_bars(SYMBOL, day_start, day_end, "1d")
    d = daily.row(0, named=True)
    print(f"\n 1d: {daily.height} bar   {d['bucket_start_et']:%H:%M} ET (04:00 session anchor)")
    print("     note the intraday window above does not reach it: anchors differ by tf")
    print(
        f"     el_volume={d['el_volume']:,} (total shares here)  "
        f"el_ticks={d['el_ticks']:,} (trade count here)"
    )
    print("     the two words swap meaning between intraday and daily: semantics.md 3.4")

    ticks = store.load_ticks(SYMBOL, start, end)
    print(f"\n ticks: {ticks.height} rows, bid/ask preserved: {ticks.row(0, named=True)['bid']}")

    # The part that used to compute something. A timeframe nobody recorded is
    # simply absent, and an absent answer is a real answer.
    empty = store.load_bars(SYMBOL, start, end, "5m")
    print(f"\n 5m: {empty.height} rows - nothing recorded this timeframe, so nothing is returned.")
    print(f"     Same columns as a populated answer ({len(empty.columns)}), so stacking")
    print("     results across days does not break on a quiet one (semantics.md 2.4).")

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
        print(f"\nremoved {root} (pass --keep to inspect it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
