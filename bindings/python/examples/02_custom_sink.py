#!/usr/bin/env python3
"""Write your own sink and run the full ingestion runtime with it.

A sink is one output destination. The runtime fans every tick and every
closed bar out to every sink you give it, and isolates their failures from
each other — one sink raising does not stop the others or take the runtime
down. This is the extension point: to send data somewhere the package does
not already support, you write ~10 lines here rather than forking anything.

The example sink tracks a running session high/low per symbol and prints a
summary on shutdown. Swap the body for a database insert, a websocket push,
a Kafka produce — the shape does not change.

Run it:

    # Publisher first (TradeStation, or the C++ harness):
    #   cpp/build/x86-release/Release/TS2Python_TestHarness.exe \
    #       --mode smoke --warmup-ms 8000
    #
    # Then, from bindings/python/:
    uv run python examples/02_custom_sink.py --seconds 10

To declare a sink in config/sinks.yaml instead of constructing it here, put
the class anywhere importable and point the YAML's `class:` at it as
`module:attr`. It has to accept `name=` as a keyword argument — that is how
the registry hands it the name from the YAML:

    sinks:
      - name: session_stats
        class: my_pkg.sinks:SessionStatsSink

Then `tradestation-data-ingest --sinks-config config/sinks.yaml` runs it with
no code change on this side. (The file you are reading cannot be used as that
target as-is: a module name starting with a digit is not importable with a
normal `import` statement.)

Keep on_bar fast — it runs inline in the ingest loop. Anything
slow belongs behind should_flush() / flush(), which the runtime drives from
a separate loop, or off in a task you spawn.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import _compat

from tradestation_data.aggregation.snapshot import MarketSnapshot
from tradestation_data.domain.bar import Bar
from tradestation_data.runtime.ingestion import IngestionRuntime
from tradestation_data.sinks import SinkPipeline
from tradestation_data.sinks.base import BaseSink
from tradestation_data.wire.el_subscriber import TradeStationELProvider


@dataclass
class _Stats:
    ticks: int = 0
    bars: int = 0
    high: float = float("-inf")
    low: float = float("inf")
    last: float = 0.0


class SessionStatsSink(BaseSink):
    """Track high/low/last per symbol and report on close.

    Subclassing BaseSink gives no-op defaults for every hook, so a sink only
    has to implement what it cares about. Implementing the `Sink` Protocol
    directly works too — it is duck-typed, not enforced by inheritance.

    `name` must be settable as a keyword argument: that is how the registry
    passes the identifier declared in sinks.yaml.
    """

    def __init__(self, *, name: str = "session_stats") -> None:
        self.name = name
        self._by_symbol: dict[str, _Stats] = {}
        self._closed = False

    def _stats(self, symbol: str) -> _Stats:
        return self._by_symbol.setdefault(symbol, _Stats())

    def on_bar(self, bar: Bar) -> None:
        # Every bar arriving here was shipped whole by the EL indicator and
        # is already closed. There is no other kind: nothing in this binding
        # builds a bar, so there is no provenance to check before using one.
        st = self._stats(bar.symbol)
        st.bars += 1
        st.high = max(st.high, bar.high)
        st.low = min(st.low, bar.low)
        print(
            f"  point closed  {bar.symbol:<6} "
            f"bt={bar.bar_type} iv={bar.bar_interval}  C={bar.close:.2f}"
        )

    def close(self) -> None:
        # The Sink protocol requires close() to be idempotent (sinks/base.py).
        # SinkPipeline happens to de-duplicate the call, but a sink cannot
        # rely on its caller for that — yours may well be driven directly.
        if self._closed:
            return
        self._closed = True

        if not self._by_symbol:
            print("\nno data received")
            return
        print("\n--- session stats ---")
        for symbol in sorted(self._by_symbol):
            st = self._by_symbol[symbol]
            print(
                f"{symbol:<6} ticks={st.ticks:<5} bars={st.bars:<4} "
                f"high={st.high:<8.2f} low={st.low:<8.2f} last={st.last:.2f}"
            )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    p.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "VXX"])
    p.add_argument("--seconds", type=float, default=10.0, help="Run for N seconds then stop.")
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    snapshot = MarketSnapshot()
    runtime = IngestionRuntime(
        provider=TradeStationELProvider(endpoint=args.endpoint),
        symbols=args.symbols,
        snapshot=snapshot,
        sinks=SinkPipeline([SessionStatsSink()]),
        heartbeat_interval=3600,  # quiet for a short demo run
    )

    print(f"running for {args.seconds:g}s on {args.endpoint} ...")
    task = asyncio.create_task(runtime.run())
    try:
        await asyncio.sleep(args.seconds)
    finally:
        # stop() signals the loops; run() then drains any still-open bar into
        # the sinks before closing them, so nothing in flight is lost.
        runtime.stop()
        await task

    # MarketSnapshot is the live in-memory view the runtime maintains
    # alongside the sinks. view_of() returns an immutable copy, which is what
    # you want from anything that spans an await.
    for symbol in args.symbols:
        view = snapshot.view_of(symbol)
        if view is not None and view.last_tick is not None:
            print(f"snapshot {symbol:<6} last tick px={view.last_tick.price:.2f}")

    return 0


if __name__ == "__main__":
    # zmq.asyncio needs a selector event loop on Windows — see _compat.py.
    raise SystemExit(_compat.run(main()))
