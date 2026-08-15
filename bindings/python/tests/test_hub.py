"""tests/test_hub.py — the nine scenarios from
docs/plans/transport-hub-2026-08-15.md's 「實測佐證」 table, automated.

Real `tcp://127.0.0.1:<port>` sockets throughout, never `zmq_inproc_bus`:
`inproc://` requires both ends to share one `zmq.Context`, but a publisher
and a consumer are separate processes in the real deployment (see
CLAUDE.md). Each test's hub runs in a background thread; `zmq.XPUB` sockets
stand in for the DLL's publisher processes and `zmq.SUB` sockets stand in
for consumers, matching the socket types the real deployment actually uses
(contract/wire.md: DLL is XPUB, hub's frontend is XSUB; consumer is SUB, hub's
backend is XPUB).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest
import zmq

from tradestation_data.hub import (
    WAKE_TOPIC,
    SubscriptionMirror,
    WakeScheduler,
    decide_subscription_forward,
    serve,
)

_TIMEOUT_S = 5.0
_POLL_MS = 100

_WAKE_SUBSCRIBE = b"\x01" + WAKE_TOPIC.encode()
_WAKE_UNSUBSCRIBE = b"\x00" + WAKE_TOPIC.encode()


# ---------------------------------------------------------------------------
# Socket-level test infrastructure
# ---------------------------------------------------------------------------


def _free_endpoint() -> str:
    """A `tcp://127.0.0.1:<port>` string for a currently-unused port.

    Grabbed by binding a throwaway stdlib socket to port 0 and reading back
    what the OS assigned, then releasing it immediately for the hub to bind
    instead -- the standard trick, and good enough for a test suite that
    runs its hubs one at a time.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return f"tcp://127.0.0.1:{s.getsockname()[1]}"


class _HubHandle:
    """A hub running in a background thread, plus every socket a test
    opened through it -- all torn down together by `teardown()`.
    """

    def __init__(self, ctx: zmq.Context, frontend: str, backend: str) -> None:
        self.ctx = ctx
        self.frontend = frontend
        self.backend = backend
        self.stop = threading.Event()
        self.ready = threading.Event()
        self._sockets: list[zmq.Socket] = []
        self.thread = threading.Thread(
            target=serve,
            kwargs={
                "ctx": ctx,
                "frontend": frontend,
                "backend": backend,
                "stop": self.stop,
                "ready": self.ready,
            },
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        assert self.ready.wait(timeout=_TIMEOUT_S), "hub never became ready"

    def new_publisher(self) -> zmq.Socket:
        """A fresh XPUB socket standing in for one DLL (`orchart.exe`) process."""
        sock = self.ctx.socket(zmq.XPUB)
        # Matches the real DLL's own socket (cpp/src/ts2python.cpp: `sock->set(
        # zmq::sockopt::xpub_verbose, 1)`). Without this, a receiving XPUB
        # dedupes repeat subscribes on the SAME pipe by itself -- xpub.cpp's
        # `notify = first_added || _verbose_subs` -- independent of how many
        # times the hub actually forwarded them. Omitting it here would make
        # this stand-in quietly hide real hub behaviour rather than observe it.
        sock.setsockopt(zmq.XPUB_VERBOSE, 1)
        sock.connect(self.frontend)
        self._sockets.append(sock)
        return sock

    def new_consumer(self) -> zmq.Socket:
        """A fresh SUB socket standing in for one consumer process."""
        sock = self.ctx.socket(zmq.SUB)
        sock.connect(self.backend)
        self._sockets.append(sock)
        return sock

    def disconnect(self, sock: zmq.Socket) -> None:
        """Close one socket now, simulating that process going away."""
        sock.close(linger=0)
        self._sockets.remove(sock)

    def teardown(self) -> None:
        for sock in self._sockets:
            sock.close(linger=0)
        self.stop.set()
        self.thread.join(timeout=_TIMEOUT_S)


@pytest.fixture
def hub() -> Iterator[_HubHandle]:
    ctx = zmq.Context()
    handle = _HubHandle(ctx, _free_endpoint(), _free_endpoint())
    handle.start()
    try:
        yield handle
    finally:
        handle.teardown()
        ctx.term()


def _wait_for(
    sock: zmq.Socket, predicate: Callable[[bytes], bool], timeout_s: float = _TIMEOUT_S
) -> bytes | None:
    """Poll `sock` for single-frame messages until one satisfies `predicate`.

    Anything not matching -- most often a wake-nonce subscribe/unsubscribe
    pair, which the hub sends unprompted after every publisher attach -- is
    discarded silently. Generous deadline with early exit rather than a
    fixed sleep: most of these resolve in well under a second.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sock.poll(_POLL_MS, zmq.POLLIN):
            msg = sock.recv()
            if predicate(msg):
                return msg
    return None


def _wait_for_multipart(
    sock: zmq.Socket, predicate: Callable[[list[bytes]], bool], timeout_s: float = _TIMEOUT_S
) -> list[bytes] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sock.poll(_POLL_MS, zmq.POLLIN):
            frames = sock.recv_multipart()
            if predicate(frames):
                return frames
    return None


def _nothing_arrives(sock: zmq.Socket, timeout_s: float) -> bool:
    """True iff no message at all arrives on `sock` within `timeout_s`."""
    return not sock.poll(int(timeout_s * 1000), zmq.POLLIN)


# ---------------------------------------------------------------------------
# Scenario 1: publisher connects first, no consumer at all yet.
# ---------------------------------------------------------------------------


def test_no_consumer_means_publisher_learns_no_real_subscription(hub: _HubHandle) -> None:
    """A publisher with nobody subscribed sees only the harmless periodic
    wake-nonce pair, never a real topic -- there is nothing to forward."""
    pub = hub.new_publisher()
    leaked = _wait_for(pub, lambda m: m not in (_WAKE_SUBSCRIBE, _WAKE_UNSUBSCRIBE), timeout_s=1.5)
    assert leaked is None, f"unexpected message with no consumer attached: {leaked!r}"


# ---------------------------------------------------------------------------
# Scenario 2: a consumer connects and subscribes.
# ---------------------------------------------------------------------------


def test_consumer_subscription_reaches_publisher(hub: _HubHandle) -> None:
    pub = hub.new_publisher()
    con = hub.new_consumer()
    con.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    assert _wait_for(pub, lambda m: m == b"\x01SPY") == b"\x01SPY"


# ---------------------------------------------------------------------------
# Scenario 3 (MANDATORY): a second publisher connects well after the
# subscription already exists, and must still learn it -- the L1 stranded-
# pipe-write case (see hub.py's module docstring for the full account).
#
# This test passes with WakeScheduler.schedule() patched to a no-op too:
# this hub's own continuous poll loop happens to release the stranded write
# on its own, an undocumented libzmq accident rather than a guarantee, so
# that is NOT evidence the wake is safe to remove -- see hub.py. There is
# deliberately no negative-control test for L1 in this suite; that gap is
# known, not an oversight.
# ---------------------------------------------------------------------------


def test_late_joining_publisher_still_learns_existing_subscription(hub: _HubHandle) -> None:
    con = hub.new_consumer()
    con.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    # Give the subscription time to land on the hub well before the
    # publisher exists -- this is what makes the new pipe's cached-
    # subscription replay (L1) the code path under test, rather than the
    # ordinary live-subscribe broadcast.
    time.sleep(0.5)

    pub = hub.new_publisher()
    msg = _wait_for(pub, lambda m: m == b"\x01SPY", timeout_s=6.0)
    assert msg == b"\x01SPY", "late-joining publisher never learned the existing subscription"


# ---------------------------------------------------------------------------
# Scenario 4: two publishers, both deliver.
# ---------------------------------------------------------------------------


def test_two_publishers_both_deliver_to_the_same_consumer(hub: _HubHandle) -> None:
    con = hub.new_consumer()
    con.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    pub_a = hub.new_publisher()
    pub_b = hub.new_publisher()

    assert _wait_for(pub_a, lambda m: m == b"\x01SPY") == b"\x01SPY"
    assert _wait_for(pub_b, lambda m: m == b"\x01SPY") == b"\x01SPY"

    pub_a.send_multipart([b"SPY", b"from-a"])
    pub_b.send_multipart([b"SPY", b"from-b"])

    got_a = _wait_for_multipart(con, lambda f: f == [b"SPY", b"from-a"])
    got_b = _wait_for_multipart(con, lambda f: f == [b"SPY", b"from-b"])
    assert got_a == [b"SPY", b"from-a"]
    assert got_b == [b"SPY", b"from-b"]


# ---------------------------------------------------------------------------
# An unsubscribed topic is filtered (backend XPUB's own per-peer filtering,
# exercised end-to-end through the hub's verbatim forwarding).
# ---------------------------------------------------------------------------


def test_unsubscribed_topic_is_filtered_out(hub: _HubHandle) -> None:
    con = hub.new_consumer()
    con.setsockopt_string(zmq.SUBSCRIBE, "SPY")  # never QQQ
    pub = hub.new_publisher()
    assert _wait_for(pub, lambda m: m == b"\x01SPY") == b"\x01SPY"

    pub.send_multipart([b"QQQ", b"ignored"])
    pub.send_multipart([b"SPY", b"delivered"])

    got = _wait_for_multipart(con, lambda f: True, timeout_s=3.0)
    assert got == [b"SPY", b"delivered"], f"expected only the SPY frame, got {got!r}"
    # QQQ must never trail in either.
    assert _nothing_arrives(con, timeout_s=0.5)


# ---------------------------------------------------------------------------
# Scenario 5: a second consumer subscribing to a new topic is seen by every
# already-attached publisher (the frontend XSUB fans a subscribe out to
# every attached pipe, not just the one that happened to trigger it).
# ---------------------------------------------------------------------------


def test_second_consumer_joining_is_seen_by_every_publisher(hub: _HubHandle) -> None:
    pub_a = hub.new_publisher()
    pub_b = hub.new_publisher()
    con1 = hub.new_consumer()
    con1.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    assert _wait_for(pub_a, lambda m: m == b"\x01SPY") == b"\x01SPY"
    assert _wait_for(pub_b, lambda m: m == b"\x01SPY") == b"\x01SPY"

    con2 = hub.new_consumer()
    con2.setsockopt_string(zmq.SUBSCRIBE, "QQQ")
    assert _wait_for(pub_a, lambda m: m == b"\x01QQQ") == b"\x01QQQ"
    assert _wait_for(pub_b, lambda m: m == b"\x01QQQ") == b"\x01QQQ"


# ---------------------------------------------------------------------------
# Scenario 6: a consumer restart (a fresh socket, standing in for a fresh
# process) is seen by the publisher as a new subscription.
# ---------------------------------------------------------------------------


def test_consumer_restart_is_seen_by_publisher(hub: _HubHandle) -> None:
    pub = hub.new_publisher()
    con1 = hub.new_consumer()
    con1.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    assert _wait_for(pub, lambda m: m == b"\x01SPY") == b"\x01SPY"

    hub.disconnect(con1)

    con2 = hub.new_consumer()  # "restart": a brand new socket, not a reconnect
    con2.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    msg = _wait_for(pub, lambda m: m == b"\x01SPY", timeout_s=6.0)
    assert msg == b"\x01SPY", "publisher never re-learned the subscription after a consumer restart"


# ---------------------------------------------------------------------------
# Scenario 7 (MANDATORY): once every consumer on a topic has disconnected,
# the publisher receives the unsubscribe. Fails without the SubscriptionMirror
# count-mirroring (L2 + L3).
#
# The asymmetry that makes the mirror necessary, confirmed against libzmq
# 4.3.5's own source (src/xsub.cpp `xsub_t::xsend`): a SUBSCRIBE is always
# forwarded -- its branch calls `_dist.send_to_all()` unconditionally, so two
# consumers on "SPY" really do produce two separate `\x01SPY` frames at the
# publisher, not one. An UNSUBSCRIBE is gated -- forwarded only when
# `_subscriptions.rm()` reports the refcount reached zero. So with two
# consumers, ZMQ_XPUB_VERBOSE on the backend reports both subscribes but only
# the LAST of the two unsubscribes; without replaying it upstream as many
# times as subscribes were forwarded, the frontend XSUB's refcount would
# still read 1 (not 0) after both consumers leave, and the publisher would
# never see the final `\x00SPY` it needs to know nobody is listening.
# ---------------------------------------------------------------------------


def test_unsubscribe_reaches_publisher_once_every_consumer_is_gone(hub: _HubHandle) -> None:
    pub = hub.new_publisher()
    con1 = hub.new_consumer()
    con2 = hub.new_consumer()
    con1.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    con2.setsockopt_string(zmq.SUBSCRIBE, "SPY")
    assert _wait_for(pub, lambda m: m == b"\x01SPY") == b"\x01SPY"
    assert _wait_for(pub, lambda m: m == b"\x01SPY") == b"\x01SPY"

    hub.disconnect(con1)
    # One consumer remains, so the topic must still read as live -- the
    # scenario is "ALL consumers gone", not "any one of them". Filtered for
    # the wake-nonce pair specifically: the hub sends one unprompted on its
    # own schedule regardless of this disconnect, and that is not a signal
    # that the topic went quiet.
    premature = _wait_for(
        pub, lambda m: m not in (_WAKE_SUBSCRIBE, _WAKE_UNSUBSCRIBE), timeout_s=0.5
    )
    assert premature is None, (
        f"unsubscribe forwarded too early, with a consumer still up: {premature!r}"
    )

    hub.disconnect(con2)
    msg = _wait_for(pub, lambda m: m == b"\x00SPY", timeout_s=6.0)
    assert msg == b"\x00SPY", "publisher never learned that every consumer left"


# ---------------------------------------------------------------------------
# Bind failure: spec item 9. A second hub colliding on the frontend port
# must fail its bind and report why, never hang or crash silently.
# ---------------------------------------------------------------------------


def test_serve_reports_and_returns_false_on_frontend_bind_conflict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx1 = zmq.Context()
    frontend = _free_endpoint()
    stop1 = threading.Event()
    ready1 = threading.Event()
    t1 = threading.Thread(
        target=serve,
        kwargs={
            "ctx": ctx1,
            "frontend": frontend,
            "backend": _free_endpoint(),
            "stop": stop1,
            "ready": ready1,
        },
        daemon=True,
    )
    t1.start()
    assert ready1.wait(timeout=_TIMEOUT_S)

    ctx2 = zmq.Context()
    stop2 = threading.Event()
    result: list[bool] = []

    def _run_second_hub() -> None:
        result.append(serve(ctx2, frontend, _free_endpoint(), stop=stop2))

    t2 = threading.Thread(target=_run_second_hub, daemon=True)
    with caplog.at_level(logging.ERROR, logger="tradestation_data.hub"):
        t2.start()
        t2.join(timeout=_TIMEOUT_S)

    assert result == [False], "a colliding bind must make serve() return False, not hang or raise"
    failures = [r for r in caplog.records if r.getMessage() == "hub_bind_failed"]
    assert failures, "a bind conflict must produce a readable hub_bind_failed log line"
    hint = failures[0].hint  # type: ignore[attr-defined]
    assert "ts2py-hub" in hint and "DLL" in hint, (
        f"hint should name both likely causes (another hub, or a DLL still bound "
        f"directly), got: {hint!r}"
    )

    stop1.set()
    t1.join(timeout=_TIMEOUT_S)
    ctx1.term()
    ctx2.term()


# ---------------------------------------------------------------------------
# Pure decision logic -- no socket, no thread, exercised directly.
# ---------------------------------------------------------------------------


def test_wake_scheduler_pop_due_is_false_before_the_delay_elapses() -> None:
    s = WakeScheduler(delays=(1.0, 2.0))
    s.schedule(now=100.0)
    assert s.pop_due(100.5) is False
    assert s.pop_due(101.0) is True
    assert s.pop_due(101.0) is False  # already popped, nothing left due
    assert s.pop_due(102.0) is True


def test_wake_scheduler_coalesces_simultaneously_due_entries() -> None:
    """Two schedule() calls due at the same instant fire as ONE wake, not two
    -- the wake pair carries no per-event payload, so nothing is lost."""
    s = WakeScheduler(delays=(0.1,))
    s.schedule(now=0.0)
    s.schedule(now=0.0)
    assert s.pop_due(0.2) is True
    assert s.pop_due(0.2) is False


def test_subscription_mirror_replays_recorded_count_on_unsubscribe() -> None:
    m = SubscriptionMirror()
    m.record_subscribe(b"SPY")
    m.record_subscribe(b"SPY")
    assert m.live_topic_count == 1
    assert m.record_unsubscribe(b"SPY") == 2
    assert m.live_topic_count == 0  # cleared


def test_subscription_mirror_unrecorded_unsubscribe_still_forwards_one() -> None:
    """Under-forwarding (0 frames) is the silent failure this class exists to
    prevent, so an untracked topic still yields one \\x00 rather than none."""
    m = SubscriptionMirror()
    assert m.record_unsubscribe(b"NEVER_SUBSCRIBED") == 1


def test_subscription_mirror_topics_are_independent() -> None:
    m = SubscriptionMirror()
    m.record_subscribe(b"SPY")
    m.record_subscribe(b"QQQ")
    m.record_subscribe(b"QQQ")
    assert m.live_topic_count == 2
    assert m.record_unsubscribe(b"SPY") == 1
    assert m.live_topic_count == 1


def test_decide_subscription_forward_subscribe_is_verbatim_and_recorded() -> None:
    m = SubscriptionMirror()
    assert decide_subscription_forward(m, b"\x01SPY") == [b"\x01SPY"]
    assert m.live_topic_count == 1


def test_decide_subscription_forward_unsubscribe_expands_to_recorded_count() -> None:
    m = SubscriptionMirror()
    decide_subscription_forward(m, b"\x01SPY")
    decide_subscription_forward(m, b"\x01SPY")
    assert decide_subscription_forward(m, b"\x00SPY") == [b"\x00SPY", b"\x00SPY"]
    assert m.live_topic_count == 0


def test_decide_subscription_forward_ignores_an_empty_message() -> None:
    m = SubscriptionMirror()
    assert decide_subscription_forward(m, b"") == []
    assert m.live_topic_count == 0
