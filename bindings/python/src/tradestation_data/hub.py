"""
ts2py-hub: a ZeroMQ XSUB/XPUB forwarder between the TradeStation DLL's N
publisher processes (one per chart, all `connect`) and every consumer (also
`connect`). See docs/plans/transport-hub-2026-08-15.md for why this exists
(TradeStation's `-multiexe` splits charts across processes, and `bind()` is
exclusive while `connect()` is not) and contract/wire.md ("hub 的義務") for
the normative spec this module implements.

Depends on pyzmq, plus `tradestation_data._logging` for the shared structured
log plumbing -- which is stdlib-only, and is the ONE intra-package import here.
Nothing else: no polars, no pyarrow, no sink registry. That independence is
deliberate: this is the REFERENCE implementation of a contract component,
and a non-Python deployment has to be able to rewrite it from contract/wire.md
alone, the same way a second language binding rewrites the wire parser.

Topology::

    N publishers (XPUB, connect)         consumers (SUB, connect)
              \\                              /
               v                            v
        XSUB bind :5555  --[ hub ]--  XPUB bind :5556

Four libzmq behaviours only show up on this "subscriber-side-binds" topology
and none of them are documented in zguide or zmq_proxy(3) -- every
non-obvious line below exists to work around exactly one of them, named in
the comment that needs it:

  L1. A newly attached publisher pipe's cached subscriptions are written by
      libzmq immediately, but that first write is stranded: it never
      triggers the read-activation signal the peer relies on. This is real
      and was measured in isolation -- a bare XSUB that does nothing but
      poll, with no other write ever issued on the socket, never delivers
      the subscription to the peer. Only a LATER write on the same pipe
      releases it, which is why `WakeScheduler` schedules a few no-op
      "wake" writes after every attach.

      A hub built as a continuous poll loop, it turns out, tends to release
      the stranded write on its own even without the wake: `serve()`'s
      `poller.poll()` on the frontend XSUB appears to surface it within a
      poll cycle or two regardless, which is why no test in this suite
      fails with `WakeScheduler.schedule()` disabled. Treat that as an
      undocumented accident of libzmq's own command processing, not a
      guarantee of this or any other poll-loop shape -- it is NOT evidence
      the wake is safe to remove. The wake stays; the absence of a
      negative-control test for L1 (see tests/test_hub.py) is a known,
      deliberate gap, not dead code nobody got around to testing.
  L2. The frontend XSUB's subscription trie is reference-counted. Replaying
      a real subscribe (e.g. as part of a "wake") inflates the count and an
      unsubscribe then never brings it back to zero, so it is never
      forwarded upstream. Worked around by forwarding a real subscribe
      exactly once, never replaying it, and using a topic no real chart
      will ever match for the wake instead (`WAKE_TOPIC`).
  L3. `ZMQ_XPUB_VERBOSE` reports every subscribe but only the LAST
      unsubscribe per topic, so N consumers on one topic produce N
      subscribes and a single unsubscribe. Worked around by mirroring the
      forwarded-subscribe count per topic and emitting that many
      unsubscribes upstream when the one downstream unsubscribe arrives
      (`SubscriptionMirror`).
  L4. A connect-side pipe survives its peer's disconnection (so reconnects
      are transparent), so the frontend XSUB never sees an unsubscribe when
      a publisher process dies mid-session. Out of scope for this module --
      it is worked around on the DLL side with its own socket monitor (see
      the plan doc's "DLL 的 socket monitor").

L1-L3 are implemented here. L2 and L3 are load-bearing AND pinned: remove
either and tests/test_hub.py turns red, scenario 7 included. L1 is
load-bearing but NOT pinned, for the reason in its own paragraph above --
do not read the green suite as permission to delete the wake.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from types import FrameType

import zmq
from zmq.utils.monitor import recv_monitor_message

# The ONLY intra-package import here, and deliberately so: it is stdlib-only
# plumbing (see its module docstring), not binding logic. Importing it does not
# pull polars/pyarrow/the sink registry in, and it does not put anything in the
# way of reimplementing this hub in another language — that job reads
# contract/wire.md, never this file.
from tradestation_data._logging import PLAIN_FORMAT, ExtraDumpFilter

log = logging.getLogger("tradestation_data.hub")

DEFAULT_FRONTEND = "tcp://127.0.0.1:5555"
DEFAULT_BACKEND = "tcp://127.0.0.1:5556"

# A subscription topic no real chart will ever match, used only to force a
# write on the frontend XSUB after something changes (L1). Neither string is
# a prefix of the other -- diverging right after the shared "__ts2py"
# stem (`_` vs `w`) -- so this can never be mistaken for a subscription to
# the control topic, and subscribing to the control topic can never be
# mistaken for a wake.
WAKE_TOPIC = "__ts2py_wake__"
_WAKE_TOPIC_BYTES = WAKE_TOPIC.encode()
_WAKE_SUBSCRIBE = b"\x01" + _WAKE_TOPIC_BYTES
_WAKE_UNSUBSCRIBE = b"\x00" + _WAKE_TOPIC_BYTES

# Measured: a single follow-up write ~0.2s after attach was enough to
# release subscriptions stranded by L1. Three attempts at increasing delay
# is the validated margin from the adversarial test suite, not a guess.
FOLLOW_UP_DELAYS_SECONDS: tuple[float, ...] = (0.05, 0.3, 1.0)

_HEARTBEAT_INTERVAL_SECONDS = 30.0
_POLL_TIMEOUT_MS = 25
_FRONTEND_RCVHWM = 1_000_000
_BACKEND_SNDHWM = 100_000


# ---------------------------------------------------------------------------
# Pure decision logic. No socket, no clock read -- both are handed in by the
# caller, so tests/test_hub.py can exercise every branch without a socket in
# sight. `serve()` below is the thin ZMQ shell around these.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WakeScheduler:
    """Tracks when the next "wake" write on the frontend XSUB is due.

    Exists to work around L1, in the module docstring above -- read that
    before deleting this class. In short: real and measured in isolation,
    but this hub's own poll loop happens to mask it anyway, so its absence
    of test coverage here is a known gap, not proof it is unneeded.

    Driven by a clock the caller supplies (`now`, a `time.monotonic()`
    reading) rather than reading one itself, so it is exercised in tests
    with arbitrary instants and no real sleeping.
    """

    delays: tuple[float, ...] = FOLLOW_UP_DELAYS_SECONDS
    _due: list[float] = field(default_factory=list)

    def schedule(self, now: float) -> None:
        """Queue one follow-up wake at `now + delay` for each configured delay.

        Called after every publisher-attach monitor event and every
        subscription change (spec item 6) -- L1's stranding can happen more
        than once for the same pipe, so one attempt is not the validated
        margin; `FOLLOW_UP_DELAYS_SECONDS` is.
        """
        self._due.extend(now + d for d in self.delays)

    def pop_due(self, now: float) -> bool:
        """True, and drop every entry at or before `now`, iff at least one is due.

        All currently-due entries are cleared together rather than one at a
        time: a wake carries no per-event payload (it is always the same
        subscribe/unsubscribe pair), so coalescing several
        coincidentally-due follow-ups into a single wake write loses
        nothing and halves nothing either.
        """
        remaining = [d for d in self._due if d > now]
        fired = len(remaining) != len(self._due)
        self._due = remaining
        return fired


@dataclass(slots=True)
class SubscriptionMirror:
    """Mirrors the frontend XSUB trie's per-topic subscriber refcount.

    L3: the backend XPUB reports every subscribe (`ZMQ_XPUB_VERBOSE`) but
    only the LAST unsubscribe per topic, so N consumers subscribed to one
    topic produce N `\\x01` frames and a single `\\x00`. L2: the frontend
    XSUB's own refcount only forwards an unsubscribe upstream once it drops
    to zero, and replaying a subscribe to compensate would inflate it
    further and never let that happen. This hub is the sole writer on the
    frontend XSUB (contract/wire.md's invariant for this component), so it
    can track exactly how many subscribes it forwarded per topic and, on
    the single downstream unsubscribe, emit that many upstream -- driving
    the real refcount to precisely zero without ever replaying a subscribe.
    """

    _counts: dict[bytes, int] = field(default_factory=dict)

    @property
    def live_topic_count(self) -> int:
        """Topics with at least one forwarded, not-yet-withdrawn subscribe."""
        return len(self._counts)

    def record_subscribe(self, topic: bytes) -> None:
        self._counts[topic] = self._counts.get(topic, 0) + 1

    def record_unsubscribe(self, topic: bytes) -> int:
        """Clear `topic` and return how many `\\x00` frames to forward upstream.

        At least one is always returned even when nothing was recorded for
        this topic. Over-sending is harmless -- the frontend XSUB's own
        refcount simply floors at zero and stops forwarding once it gets
        there -- while under-sending (returning 0) is exactly the silent
        failure this class exists to prevent: a topic the mirror lost track
        of for any reason would then never be withdrawn upstream at all.
        """
        return max(self._counts.pop(topic, 0), 1)


def decide_subscription_forward(mirror: SubscriptionMirror, message: bytes) -> list[bytes]:
    """One backend XPUB subscription message in, frontend XSUB frames out.

    `message` is the raw frame XPUB delivers for a subscribe/unsubscribe: a
    leading `0x01`/`0x00` byte followed by the topic. A real subscription is
    forwarded verbatim exactly once and never replayed (L2, spec item 4); an
    unsubscribe is expanded to as many frames as `mirror` recorded
    subscribes for that topic (L3, spec item 5). Anything else -- an empty
    message, which `ZMQ_XPUB_VERBOSE` never actually produces -- is ignored
    rather than raising: a hub that cannot parse its own control channel
    must not crash the forwarding loop over it.
    """
    if not message:
        return []
    kind, topic = message[:1], message[1:]
    if kind == b"\x01":
        mirror.record_subscribe(topic)
        return [message]
    if kind == b"\x00":
        count = mirror.record_unsubscribe(topic)
        return [b"\x00" + topic] * count
    return []


@dataclass(slots=True)
class HubStats:
    """Counters surfaced in the `hub_heartbeat` log line.

    Per the spec, the heartbeat is the operator's only window into "which
    chart isn't coming through" once this is wired to real TradeStation
    charts, so every field here is one this module can answer cheaply on
    every poll iteration.
    """

    frames_forwarded: int = 0
    subscriptions_forwarded: int = 0
    subscriptions_withdrawn: int = 0
    wakes_sent: int = 0
    # Cumulative, not a live gauge: the frontend monitor (below) is
    # registered for EVENT_ACCEPTED | EVENT_HANDSHAKE_SUCCEEDED only, with
    # no EVENT_DISCONNECTED -- noticing a publisher's departure is the DLL's
    # own socket monitor's job (L4), not this process's.
    publisher_handshakes_total: int = 0


# ---------------------------------------------------------------------------
# ZMQ shell. Thin on purpose: every decision above is delegated to the pure
# helpers; this just wires sockets to them.
# ---------------------------------------------------------------------------


def _bind(sock: zmq.SyncSocket, endpoint: str, role: str) -> bool:
    """Bind `sock`, logging a readable, actionable error on failure.

    Never raises -- spec item 9 requires a second hub's bind failure to end
    the process with a readable message, not a bare traceback.
    """
    try:
        sock.bind(endpoint)
    except zmq.error.ZMQError as exc:
        addr_in_use = exc.errno == zmq.EADDRINUSE
        hint = (
            "another ts2py-hub is likely already bound to this endpoint"
            if addr_in_use
            else "the endpoint is unreachable or invalid on this host"
        )
        log.error(
            "hub_bind_failed",
            extra={
                "role": role,
                "endpoint": endpoint,
                "errno": exc.errno,
                "reason": str(exc),
                "hint": (
                    f"{hint}. If this is the frontend port, a DLL still bound "
                    f"directly instead of connecting to a hub is the other "
                    f"common cause."
                ),
            },
        )
        return False
    return True


def _handle_monitor_event(mon: zmq.SyncSocket, stats: HubStats) -> None:
    """Drain one frontend monitor message, counting a completed handshake.

    `EVENT_ACCEPTED` fires on the raw TCP accept; `EVENT_HANDSHAKE_SUCCEEDED`
    once the peer has actually proven it speaks ZMTP -- only the latter is
    counted as a publisher connection. Both still cause the caller to
    schedule a wake (spec item 6): L1's stranding is about the pipe's write
    path becoming live at all, not about which specific event announced it.
    """
    event = recv_monitor_message(mon)
    if event["event"] == zmq.EVENT_HANDSHAKE_SUCCEEDED:
        stats.publisher_handshakes_total += 1
        log.info(
            "publisher_attached",
            extra={"endpoint": event["endpoint"].decode("utf-8", errors="replace")},
        )


def serve(
    ctx: zmq.Context[zmq.SyncSocket],
    frontend: str,
    backend: str,
    *,
    stop: threading.Event,
    ready: threading.Event | None = None,
    poll_timeout_ms: int = _POLL_TIMEOUT_MS,
    heartbeat_interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> bool:
    """Run the hub loop on `ctx` until `stop` is set.

    Returns True on a clean exit, False if either socket failed to bind
    (already logged by `_bind`). `ready`, if given, is set once both
    sockets are bound and the loop is about to start polling -- tests use
    it instead of a fixed startup sleep.

    Blocking-send is never a concern here: XSUB and XPUB are both
    pub-sub-family sockets, which drop past their high-water mark instead of
    blocking, by libzmq design -- so the poll loop below can never stall on
    a `send`.
    """
    xsub = ctx.socket(zmq.XSUB)
    xpub = ctx.socket(zmq.XPUB)
    mon: zmq.SyncSocket | None = None
    xsub.setsockopt(zmq.LINGER, 0)
    xpub.setsockopt(zmq.LINGER, 0)
    xsub.setsockopt(zmq.RCVHWM, _FRONTEND_RCVHWM)
    # Verbose: without it, XPUB reports only the FIRST subscriber per topic
    # and treats a second subscribe as a duplicate. A consumer reconnecting
    # a few ms before its predecessor's pipe is torn down would then be
    # silently swallowed -- the same overlap contract/wire.md documents for
    # the DLL's own XPUB, and the same fix.
    xpub.setsockopt(zmq.XPUB_VERBOSE, 1)
    xpub.setsockopt(zmq.SNDHWM, _BACKEND_SNDHWM)

    try:
        if not _bind(xsub, frontend, "frontend (XSUB)"):
            return False
        if not _bind(xpub, backend, "backend (XPUB)"):
            return False

        # Only the frontend needs a monitor: L1 is entirely about a
        # publisher's pipe on THIS socket. The backend XPUB already tells us
        # everything it can about consumers through the XPUB_VERBOSE frames
        # handled below.
        mon = xsub.get_monitor_socket(zmq.EVENT_ACCEPTED | zmq.EVENT_HANDSHAKE_SUCCEEDED)

        poller = zmq.Poller()
        poller.register(xsub, zmq.POLLIN)
        poller.register(xpub, zmq.POLLIN)
        poller.register(mon, zmq.POLLIN)

        mirror = SubscriptionMirror()
        wakes = WakeScheduler()
        stats = HubStats()
        last_heartbeat = time.monotonic()

        if ready is not None:
            ready.set()

        while not stop.is_set():
            events = dict(poller.poll(poll_timeout_ms))

            if xsub in events:
                # Data, publisher -> consumer, forwarded verbatim: the hub
                # is transport, not a parser (spec item 3). A hub that can
                # read the JSON payload will eventually grow a filter.
                xpub.send_multipart(xsub.recv_multipart())
                stats.frames_forwarded += 1

            if xpub in events:
                message = xpub.recv()
                for out_frame in decide_subscription_forward(mirror, message):
                    xsub.send(out_frame)
                topic_str = message[1:].decode("utf-8", errors="replace")
                if message[:1] == b"\x01":
                    stats.subscriptions_forwarded += 1
                    log.info("subscription_forwarded", extra={"topic": topic_str})
                elif message[:1] == b"\x00":
                    stats.subscriptions_withdrawn += 1
                    log.info("subscription_withdrawn", extra={"topic": topic_str})
                wakes.schedule(time.monotonic())

            if mon in events:
                _handle_monitor_event(mon, stats)
                wakes.schedule(time.monotonic())

            now = time.monotonic()
            if wakes.pop_due(now):
                xsub.send(_WAKE_SUBSCRIBE)
                xsub.send(_WAKE_UNSUBSCRIBE)
                stats.wakes_sent += 1
                log.debug("wake_sent")

            if now - last_heartbeat >= heartbeat_interval:
                log.info(
                    "hub_heartbeat",
                    extra={
                        "frames_forwarded": stats.frames_forwarded,
                        "live_topics": mirror.live_topic_count,
                        "publisher_handshakes_total": stats.publisher_handshakes_total,
                        "subscriptions_forwarded": stats.subscriptions_forwarded,
                        "subscriptions_withdrawn": stats.subscriptions_withdrawn,
                        "wakes_sent": stats.wakes_sent,
                    },
                )
                last_heartbeat = now
        return True
    finally:
        # LINGER=0 is already set on both sockets, so every close() below is
        # instant regardless of anything still queued.
        if mon is not None:
            mon.close(linger=0)
        xpub.close(linger=0)
        xsub.close(linger=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(PLAIN_FORMAT))
    handler.addFilter(ExtraDumpFilter())
    root.addHandler(handler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tradestation-data-hub",
        description=(
            "ZeroMQ XSUB/XPUB forwarder between the TradeStation DLL's publisher "
            "processes and every consumer. See "
            "docs/plans/transport-hub-2026-08-15.md and contract/wire.md."
        ),
    )
    p.add_argument(
        "--frontend",
        default=DEFAULT_FRONTEND,
        help=f"XSUB bind endpoint, facing publishers (default: {DEFAULT_FRONTEND}).",
    )
    p.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help=f"XPUB bind endpoint, facing consumers (default: {DEFAULT_BACKEND}).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    ctx = zmq.Context.instance()
    stop = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("signal_received", extra={"signal": signum})
        stop.set()

    # Plain registration, no Windows branch needed: unlike
    # wire/el_subscriber.py's asyncio SUB, this loop is a synchronous poll
    # with a short timeout (matching contract/tools/record.py's approach),
    # so control returns to Python often enough for a pending signal to be
    # serviced promptly on every platform.
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    ok = serve(ctx, args.frontend, args.backend, stop=stop)
    ctx.term()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
