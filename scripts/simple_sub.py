#!/usr/bin/env python3
"""Minimal ZeroMQ SUB verifier for the TS2Python wire protocol.

Runs standalone with just `pyzmq` — intentionally does not depend on the
tradestation_data package so it can be executed from anywhere as a diagnostic.

Usage:
  python scripts/simple_sub.py                          # subscribe to all
  python scripts/simple_sub.py SPY QQQ                  # filter
  python scripts/simple_sub.py --endpoint tcp://127.0.0.1:5555
  python scripts/simple_sub.py --count 100              # exit after N msgs
  python scripts/simple_sub.py --latency                # print end-to-end ms
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbols", nargs="*", help="topics to subscribe to (default: all)")
    ap.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    ap.add_argument("--count", type=int, default=0, help="exit after N messages (0 = forever)")
    ap.add_argument("--latency", action="store_true", help="print per-message end-to-end latency")
    ap.add_argument("--quiet", action="store_true", help="only print summary")
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
    try:
        while not stop:
            if not sock.poll(timeout=200, flags=zmq.POLLIN):
                continue
            topic, payload = sock.recv_multipart(flags=zmq.NOBLOCK)
            symbol = topic.decode("utf-8", errors="replace")
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
