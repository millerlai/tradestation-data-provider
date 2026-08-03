#!/usr/bin/env python3
"""Replay recorded wire frames through the real binding — no TradeStation.

**Runs offline.** It stands up a ZeroMQ PUB socket in-process, replays the
frames recorded in `contract/fixtures/` through it, and lets an ordinary
`TradeStationELProvider` consume them over the actual wire. Nothing is
stubbed or monkey-patched: the decode path here is the one that runs in
production.

    uv run python examples/04_replay_fixtures.py                  # bars
    uv run python examples/04_replay_fixtures.py --fixture smoke
    uv run python examples/04_replay_fixtures.py --fixture session

That makes it the pattern to copy when you want to test **your own sink**
against realistic input: point the pipeline below at your sink instead of
the printing one and you get repeatable, byte-exact market data with no
TradeStation, no market hours, and no waiting.

The fixtures are recorded from a real DLL, never hand-written — a
hand-written one would only restate what we believe the wire looks like.
`contract/fixtures/README.md` lists which harness mode produced each file.

Available fixtures:

    smoke        points across three symbols; bar_time floored to the minute
    noquote      absent quotes as JSON null, on an index and a normal symbol
    bars         nine BarType/BarInterval combinations, none refused —
                 including 2-minute, weekly and 2-day, which the superseded
                 wire rejected with rc -5 and never published at all
    session      first and last point of an RTH session, landed as EL stamped
                 them (09:31 / 16:00 ET — the close times, verbatim)

There is no superseded-protocol fixture to replay. A publisher older than
`proto` 2 is refused outright rather than read on a compatibility path, so
there is nothing for such a fixture to certify.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import _compat
import zmq
import zmq.asyncio

from tradestation_data.sinks import SinkPipeline
from tradestation_data.sinks.memory import InMemorySink
from tradestation_data.wire.el_subscriber import TradeStationELProvider

# bindings/python/examples/ -> repo root is four levels up.
FIXTURES = Path(__file__).resolve().parents[3] / "contract" / "fixtures"


def load_frames(name: str) -> list[tuple[str, bytes]]:
    """Read a fixture as (topic, raw payload) pairs, exactly as recorded."""
    path = FIXTURES / f"{name}.jsonl"
    if not path.is_file():
        raise SystemExit(
            f"fixture not found: {path}\n"
            "The fixtures live in the repo, not in the installed package — "
            "run this example from a git checkout."
        )
    frames: list[tuple[str, bytes]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        # A frame that was not valid UTF-8 is recorded under a different key
        # rather than dropped, so the failure survives into the fixture.
        if "payload" not in entry:
            continue
        frames.append((entry["topic"], entry["payload"].encode("utf-8")))
    return frames


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", default="bars")
    args = p.parse_args()

    frames = load_frames(args.fixture)
    symbols = sorted({topic for topic, _ in frames})
    print(f"replaying {len(frames)} frame(s) from {args.fixture}.jsonl  symbols={symbols}\n")

    # inproc:// needs both ends on ONE context — it never touches the network
    # stack, so there is no port to collide with and no handshake to wait on.
    ctx = zmq.asyncio.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind("inproc://replay")

    # Passing the context in is what lets the provider join the same inproc
    # bus. Over TCP you would just give it an endpoint.
    provider = TradeStationELProvider(endpoint="inproc://replay", context=ctx)
    await provider.connect()
    await provider.subscribe(symbols)

    sink = InMemorySink(name="captured")
    pipeline = SinkPipeline([sink])

    try:
        # Subscribe BEFORE publishing. A PUB socket silently drops everything
        # it sends while no subscriber is attached — publish first and the
        # replay is simply empty, with no error to explain why.
        await asyncio.sleep(0.1)
        for topic, payload in frames:
            await pub.send_multipart([topic.encode("utf-8"), payload])

        events = provider.events()
        received = 0
        while received < len(frames):
            try:
                event = await asyncio.wait_for(anext(events), timeout=2.0)
            except (TimeoutError, StopAsyncIteration):
                # Fewer events than frames means the binding refused some —
                # an unreadable ts_str, say. That is a legitimate outcome,
                # not a hang: it logs and skips rather than raising.
                break
            received += 1

            pipeline.on_bar(event)
            # Each side is independently optional — see format_quote in
            # 01_print_events.py for why testing only `bid` is a trap.
            quote = (
                "no quote"
                if event.bid is None and event.ask is None
                else f"{'-' if event.bid is None else format(event.bid, '.2f')}"
                f"/{'-' if event.ask is None else format(event.ask, '.2f')}"
            )
            print(
                f"{event.symbol:<6} bt={event.bar_type} iv={event.bar_interval:<3} "
                f"cat={event.category}  {event.bar_time_et:%Y-%m-%d %H:%M} ET  "
                f"O={event.open:.2f} H={event.high:.2f} "
                f"L={event.low:.2f} C={event.close:.2f}  {quote}"
            )

        await events.aclose()
    finally:
        pipeline.close()
        await provider.close()
        pub.close(linger=0)
        # destroy() rather than term(): plain term() occasionally segfaults on
        # Windows with pyzmq under asyncio.
        ctx.destroy(linger=0)

    print(f"\ndecoded {received}/{len(frames)} frame(s)")
    print(f"buffered in the sink: {len(sink.bars())} point(s)")

    lost = provider.messages_lost
    if lost is None:
        print("messages lost: unknown - this publisher carries no sequence number")
    else:
        print(f"messages lost: {lost}")
    return 0


if __name__ == "__main__":
    # zmq.asyncio needs a selector event loop on Windows — see _compat.py.
    raise SystemExit(_compat.run(main()))
