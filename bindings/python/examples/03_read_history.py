#!/usr/bin/env python3
"""Read stored data back, and see what the read API does and does not do.

**Runs offline** — no TradeStation, no DLL. It fabricates a session's worth
of points, writes them exactly as the live runtime would, and reads
them back.

    uv run python examples/03_read_history.py

The thing worth noticing is what is NOT here. `load_bars` reads Parquet and
nothing else: there is no cache to miss, no resampling, no backfill. Ask for
a chart the collector never recorded and you get zero rows, not a
computed substitute — because a bar assembled here would be indistinguishable
from one TradeStation published the moment it hit disk, and you would have no
way to tell later which you were looking at. Building 5-minute bars out of
1-minute ones is a fine thing to do; it is just yours to do, with your own
rules, downstream of this package.

    data/
      bars/bartype=1/interval=1/symbol=SPY/date=2026-04-20/bars.parquet
      bars/bartype=2/interval=1/symbol=SPY/bars.parquet   <- one file, no date=

`bar_time` is the publisher's own timestamp, landed verbatim: EasyLanguage's
`Time` is the point's CLOSE, so an RTH 1m session runs 09:31 through 16:00.
There is no left-edge conversion and no grid — a consumer wanting left edges
subtracts for itself (contract/semantics.md §2).

Times are Eastern. This is a US-equity store, so a bare `datetime(...)` passed
to `load_bars` means `America/New_York` (§2.3) — you never do
offset arithmetic to ask a question. Every frame still carries both views,
`bar_time` in UTC beside `bar_time_et`.

An *event* timestamp is different: `Bar.bar_time` is an absolute instant, so
give it a timezone-aware value. Only the query bounds have a default.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tradestation_data.domain.bar import Bar
from tradestation_data.storage.bar_writer import BarWriter
from tradestation_data.storage.history_store import HistoryStore

ET = ZoneInfo("America/New_York")
# The session open, said the way a US-equity desk says it.
SESSION_OPEN = datetime(2026, 4, 20, 9, 30, tzinfo=ET)
SYMBOL = "SPY"


def write_synthetic_store(root: Path, *, minutes: int = 30) -> int:
    """Write points the way the live sink would.

    The quantities are shaped like real intraday data: EasyLanguage's
    `Volume` is the up-tick share volume (so it equals `UpTicks`) and
    `Ticks` is the total (so it equals up plus down). Both go to disk
    verbatim under their own names — see contract/semantics.md §3.4 for why
    neither is called "volume".
    """
    price = 450.0
    bars = 0

    with BarWriter(root / "bars") as bar_writer:
        for step in range(minutes):
            bucket = SESSION_OPEN + timedelta(minutes=step)
            open_ = round(price, 2)
            price += 0.03 + (step % 5) * 0.02
            close = round(price, 2)
            up, down = 6_000 + step * 10, 4_000 + step * 5
            bar_writer.write(
                Bar(
                    symbol=SYMBOL,
                    bar_time=bucket,
                    bar_type=1,  # EL `BarType`     — intraday minutes
                    bar_interval=1,  # EL `BarInterval` — how many
                    category=2,  # EL `Category`     — 2 = Stock
                    open=open_,
                    high=round(max(open_, close) + 0.04, 2),
                    low=round(min(open_, close) - 0.03, 2),
                    close=close,
                    el_volume=up,  # EL `Volume`  — up-tick shares
                    el_ticks=up + down,  # EL `Ticks`   — total shares
                    el_upticks=up,
                    el_downticks=down,
                    el_open_interest=down,  # EL `OpenInt` — returns DownTicks here
                    bid=round(close - 0.01, 2),
                    ask=round(close + 0.01, 2),
                )
            )
            bars += 1

        # One daily point. BarType 2 lives in a flat single-file layout, and
        # on a daily chart EasyLanguage's words mean the opposite of the
        # above: `Volume` is total shares, and `Ticks` is "Volume plus Open
        # Interest" — which for a stock, whose OI is 0, is just the volume
        # again. It is not a trade count. semantics.md §3.4 has the table.
        bar_writer.write(
            Bar(
                symbol=SYMBOL,
                bar_time=datetime(2026, 4, 20, 16, 0, tzinfo=ET),
                bar_type=2,
                bar_interval=1,
                category=2,
                open=450.0,
                high=round(price + 0.5, 2),
                low=449.5,
                close=round(price, 2),
                el_volume=55_437_545,
                el_ticks=55_437_545,
                el_upticks=55_437_545,
                el_downticks=0,
                el_open_interest=0,
                bid=None,
                ask=None,
            )
        )
        bars += 1

    return bars


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

    n_bars = write_synthetic_store(root)
    print(f"wrote {n_bars} points under {root}\n")

    store = HistoryStore(root)
    # Bare datetimes, no tzinfo and no offset arithmetic: on the read API these
    # are Eastern, because that is the only clock this data has ever been in.
    start = datetime(2026, 4, 20, 9, 29)
    end = datetime(2026, 4, 20, 10, 30)

    # The chart is named the way EasyLanguage names it -- BarType 1 with
    # BarInterval 1 is a one-minute chart. There is no "1m" string anywhere,
    # and no allow-list to fail against.
    bars = store.load_bars(SYMBOL, start, end, bar_type=1, bar_interval=1)
    first, last = bars.row(0, named=True), bars.row(-1, named=True)
    print(f" bartype=1 interval=1: {bars.height} points")
    print(
        f"     first  {first['bar_time_et']:%H:%M} ET  "
        f"O={first['open']:.2f} H={first['high']:.2f} "
        f"L={first['low']:.2f} C={first['close']:.2f}"
    )
    print(
        f"            el_volume={first['el_volume']} (up-tick shares)  "
        f"el_ticks={first['el_ticks']} (total shares)"
    )
    print(f"            bid={first['bid']} ask={first['ask']}  <- every point carries them")
    print(f"     last   {last['bar_time_et']:%H:%M} ET  C={last['close']:.2f}")

    # The daily point. Its timestamp is EL's own close time, landed verbatim,
    # so it is 16:00 ET -- outside the 09:29-10:30 window above.
    day_start = datetime(2026, 4, 20, 0, 0)
    day_end = datetime(2026, 4, 20, 23, 59)
    daily = store.load_bars(SYMBOL, day_start, day_end, bar_type=2, bar_interval=1)
    d = daily.row(0, named=True)
    print(f"\n bartype=2: {daily.height} point   {d['bar_time_et']:%H:%M} ET")
    print(
        f"     el_volume={d['el_volume']:,} (total shares here)  "
        f"el_ticks={d['el_ticks']:,} (Volume + OpenInterest, so the same again)"
    )
    print("     the words swap meaning between intraday and daily: semantics.md 3.4")

    # A chart nobody recorded is simply absent, and an absent answer is a real
    # answer -- not an error, and not something this binding computes.
    empty = store.load_bars(SYMBOL, start, end, bar_type=1, bar_interval=5)
    print(f"\n interval=5: {empty.height} rows - nothing recorded, so nothing is returned.")
    print(f"     Same columns as a populated answer ({len(empty.columns)}), so stacking")
    print("     results across days does not break on a quiet one (semantics.md 2.4).")

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
        print(f"\nremoved {root} (pass --keep to inspect it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
