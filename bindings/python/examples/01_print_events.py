#!/usr/bin/env python3
"""Smallest possible live subscriber: connect, subscribe, print what arrives.

This is the "is data actually flowing?" example. No storage, no aggregation —
just the provider and a print. Everything else in this package is built on
top of the loop below.

Run it:

    # 1. Something must already be publishing. Either TradeStation with the
    #    EL indicator loaded, or the C++ harness (no TradeStation needed):
    #
    #      cpp/build/x86-release/Release/TS2Python_TestHarness.exe \
    #          --mode smoke --warmup-ms 8000
    #
    # 2. Then, from bindings/python/:
    uv run python examples/01_print_events.py
    uv run python examples/01_print_events.py --count 6      # stop after 6
    uv run python examples/01_print_events.py --symbols SPY QQQ

A PUB socket drops whatever it sends while no subscriber is attached, so
start this side first — that is what the harness's --warmup-ms buys you.

Nothing prints and no error appears? See the "Nothing arrives" note in
examples/README.md. The usual cause is a publisher that started before this
subscriber; the Windows event-loop trap is already handled in _compat.py.
"""

from __future__ import annotations

import argparse
import asyncio

import _compat

from tradestation_data.domain.bar import Bar
from tradestation_data.wire.el_subscriber import TradeStationELProvider


def format_quote(bid: float | None, ask: float | None) -> str:
    """Render a quote where each side is independently optional.

    bid and ask are separate `float | None` fields and the whole path keeps
    them that way — the DLL normalises each one on its own, and so does the
    binding. Testing only `bid` and then formatting both is a TypeError
    waiting for the first one-sided quote.
    """
    if bid is None and ask is None:
        return "no quote"
    left = "-" if bid is None else f"{bid:.2f}"
    right = "-" if ask is None else f"{ask:.2f}"
    return f"{left}/{right}"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    p.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "VXX"])
    p.add_argument("--count", type=int, default=0, help="Stop after N events (0 = forever).")
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    provider = TradeStationELProvider(endpoint=args.endpoint)
    await provider.connect()
    # Subscribe per symbol. ZMQ subscriptions are prefix matches, so a "SPY"
    # subscription also delivers SPYG frames — the provider filters those out
    # again on exact equality (contract/semantics.md §5). You get only what
    # you asked for.
    await provider.subscribe(args.symbols)

    print(f"listening on {args.endpoint} for {', '.join(args.symbols)}  (Ctrl+C to stop)")

    seen = 0
    try:
        async for event in provider.events():
            if isinstance(event, Bar):
                print(
                    f"BAR  {event.symbol:<6} {event.timeframe:>3}  "
                    f"{event.bucket_start_et:%Y-%m-%d %H:%M}  "
                    f"O={event.open:<8.2f} H={event.high:<8.2f} "
                    f"L={event.low:<8.2f} C={event.close:<8.2f} vol={event.volume}"
                )
            else:
                # bid/ask are absent when there is no quote to report:
                # historical replay, or a breadth index that never carries one.
                quote = format_quote(event.bid, event.ask)
                print(
                    f"TICK {event.symbol:<6}      "
                    f"{event.timestamp_et:%Y-%m-%d %H:%M:%S}  "
                    f"px={event.price:<8.2f} vol={event.volume:<6} {quote}"
                )

            seen += 1
            if args.count and seen >= args.count:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C does NOT arrive here as KeyboardInterrupt. asyncio.run
        # installs a SIGINT handler that cancels the main task, so what this
        # coroutine is handed is CancelledError; the KeyboardInterrupt is
        # raised outside, by the runner. Catching only KeyboardInterrupt
        # would skip the summary below and exit with a traceback instead —
        # on the default (--count 0) path, which is the documented one.
        pass
    finally:
        await provider.close()

    # messages_lost is None, not 0, when the publisher is too old to carry a
    # sequence number — "cannot tell" is a different answer from "none lost",
    # and reading them as the same thing is how a gap-free day gets certified
    # by accident (contract/semantics.md §6.6).
    lost = provider.messages_lost
    verdict = "unknown (publisher sends no seq)" if lost is None else str(lost)
    print(f"\n{seen} event(s); messages lost: {verdict}")
    return 0


if __name__ == "__main__":
    # Not plain asyncio.run(): on Windows pyzmq needs a selector event loop
    # or recv never wakes. See _compat.py — every entry point that touches
    # zmq.asyncio needs this.
    raise SystemExit(_compat.run(main()))
