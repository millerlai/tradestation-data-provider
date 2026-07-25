#!/usr/bin/env python3
"""Wire inspector and conformance-fixture recorder for the TS2Python protocol.

Depends on `pyzmq` and nothing else — deliberately *not* on any binding.
That independence is what qualifies it to record fixtures: a recorder that
imported the reference binding would bake that binding's assumptions into
the very files used to check every other binding against.

Fixtures must be recorded from a real DLL, never hand-written. Hand-written
fixtures only restate what we believe the wire looks like; recorded ones
catch the places where the implementation and the spec disagree.

Usage:
  python contract/tools/record.py                       # subscribe to all
  python contract/tools/record.py SPY QQQ               # filter
  python contract/tools/record.py --endpoint tcp://127.0.0.1:5555
  python contract/tools/record.py --count 100           # exit after N msgs
  python contract/tools/record.py --latency             # print end-to-end ms

Recording a fixture (pair with cpp test_harness, which drives the DLL
without TradeStation):

  python contract/tools/record.py --count 6 --quiet \\
      --record ../fixtures/smoke.jsonl

Each output line is {"topic": ..., "payload": ...} where `payload` is the
frame verbatim, before any parsing. Payloads are UTF-8 JSON per
contract/v2/envelope.md; a frame that fails to decode is recorded with
`payload_invalid_utf8` instead so the failure survives into the fixture
rather than being silently normalised away.
"""

from __future__ import annotations

import argparse
import json
import signal
import statistics
import sys
import time
from collections import Counter
from datetime import datetime

import zmq


def fixture_entry(symbol: str, payload: bytes) -> dict[str, str]:
    """Turn one received frame into a fixture line, without interpreting it.

    Payloads are UTF-8 JSON per contract/v2/envelope.md. A frame that does
    not decode is preserved under a different key rather than dropped or
    coerced — a fixture that silently omits what the wire actually produced
    is worse than no fixture, because every binding would then be checked
    against a cleaned-up version of reality.
    """
    try:
        return {"topic": symbol, "payload": payload.decode("utf-8")}
    except UnicodeDecodeError:
        return {"topic": symbol, "payload_invalid_utf8": repr(payload)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbols", nargs="*", help="topics to subscribe to (default: all)")
    ap.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    ap.add_argument("--count", type=int, default=0, help="exit after N messages (0 = forever)")
    ap.add_argument("--latency", action="store_true", help="print per-message end-to-end latency")
    ap.add_argument("--quiet", action="store_true", help="only print summary")
    ap.add_argument(
        "--record",
        metavar="PATH",
        help="append each frame verbatim to PATH as JSONL (conformance fixture)",
    )
    args = ap.parse_args()

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.connect(args.endpoint)
    if args.symbols:
        for sym in args.symbols:
            sock.setsockopt_string(zmq.SUBSCRIBE, sym)
    else:
        sock.setsockopt_string(zmq.SUBSCRIBE, "")  # all topics

    print(f"[sub] connected {args.endpoint} topics={args.symbols or '(all)'}", file=sys.stderr)

    # Windows pyzmq's blocking recv_multipart() does not honour Ctrl+C until
    # a message arrives — the signal queues up in Python but the C call
    # won't return. We install a SIGINT handler that flips a stop flag and
    # drive the loop with a short poll timeout so the interpreter regularly
    # regains control and can exit promptly.
    stop = False

    def _on_sigint(_sig, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_sigint)

    by_symbol: Counter[str] = Counter()
    latencies_ms: list[float] = []
    n = 0
    rec = open(args.record, "w", encoding="utf-8", newline="\n") if args.record else None
    if rec is not None:
        print(f"[sub] recording to {args.record}", file=sys.stderr)
    try:
        while not stop:
            if not sock.poll(timeout=200, flags=zmq.POLLIN):
                continue
            topic, payload = sock.recv_multipart(flags=zmq.NOBLOCK)
            symbol = topic.decode("utf-8", errors="replace")

            # Record before parsing. A frame we cannot decode is exactly the
            # kind of thing a fixture should preserve — dropping it here
            # would quietly shrink the wire down to the subset we already
            # know how to read.
            if rec is not None:
                rec.write(json.dumps(fixture_entry(symbol, payload), ensure_ascii=False) + "\n")

            try:
                doc = json.loads(payload)
            except json.JSONDecodeError as exc:
                print(f"[sub] bad JSON for {symbol}: {exc} payload={payload[:200]!r}")
                continue

            by_symbol[symbol] += 1
            n += 1

            if args.latency:
                wire_ts = float(doc.get("ts", 0.0))
                now = time.time()
                latencies_ms.append((now - wire_ts) * 1000.0)

            if not args.quiet:
                now_str = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
                print(f"{{{now_str}}} - {symbol}\t{doc}")

            if args.count and n >= args.count:
                break
    finally:
        if stop:
            print("\n[sub] interrupted", file=sys.stderr)
        if rec is not None:
            rec.close()
        sock.close(linger=0)
        ctx.term()

    print("[sub] summary:", file=sys.stderr)
    for sym, cnt in by_symbol.most_common():
        print(f"  {sym:>10} : {cnt}", file=sys.stderr)
    if args.latency and latencies_ms:
        latencies_ms.sort()
        pct = lambda p: latencies_ms[min(int(len(latencies_ms) * p), len(latencies_ms) - 1)]  # noqa: E731
        print(
            f"  latency_ms p50={statistics.median(latencies_ms):.2f} "
            f"p95={pct(0.95):.2f} p99={pct(0.99):.2f} "
            f"max={max(latencies_ms):.2f}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
