from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import zmq
import zmq.asyncio

from tradestation_data.domain.bar import Bar
from tradestation_data.wire.base import MarketEvent

log = logging.getLogger(__name__)

# TradeStation US equity charts are always ET. EL's Date/Time reflect the
# chart timezone, so we treat the raw TsStr as ET explicitly rather than
# trusting the DLL's mktime() (which would interpret it via the TS host's
# Windows timezone — wrong whenever the operator's system isn't ET).
_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")

# EL's TsStr, and the same thing spelled for a human in the refusal message.
# One constant each so the parser and the error can never describe different
# formats — an error naming a format the code does not accept is worse than
# no error at all.
_EL_TS_FORMAT = "%Y-%m/%d-%H:%M:%S"
_EL_TS_FORMAT_HUMAN = "yyyy-MM/dd-HH:mm:ss (24-hour)"

# The binding no longer blanks anyone's quote.
#
# There used to be a hard-coded list of index / breadth symbols whose
# bid/ask were discarded at parse time, on the grounds that their live
# numbers mean nothing. Two things were wrong with it. It is a guess: a
# symbol nobody thought to list keeps its meaningless quote, and one listed
# by mistake loses a real one — measured, `VXX` was on it, and VXX is a
# tradeable ETN that reported 567,776 shares in a single bar. And it is an
# opinion about what a number means, which is the consumer's to hold.
#
# `category` now travels on every frame (4 = Index), so a consumer that
# wants the old behaviour has a fact to key off instead of a list.

# The protocol version, carried in `proto`. There is exactly one, and a frame
# without the key is not this protocol at all.
#
# The key is `proto` rather than `v` on purpose. The superseded wire used `v`
# and counted to 4; restarting at 1 under the same key would have made
# {"v":1} a legal opening for both protocols, and the frames would then have
# failed in the worst possible way — the old v1 bar used kind "bar_1m", which
# the unknown-kind rule skips silently, while an old v1 tick would have
# matched on shape and only diverged at field level. See contract/wire.md.
PROTO_VERSION = 2

# Where the publisher announces charts. NOT a symbol topic.
#
# EL_InitChart sends one hello frame per chart carrying symbol / category /
# bar_type / bar_interval, and it rides a fixed topic rather than the
# chart's own symbol because a consumer subscribes per symbol from a
# configured list — a chart on a symbol nobody asked for could not be
# announced on that symbol's topic, and that is exactly the case worth
# hearing about.
#
# The topic is also the discriminator: no `kind` field was added, and the
# point frame is byte-for-byte what it always was. The leading underscores
# keep it out of TradeStation's symbol space, which matters because ZMQ
# SUBSCRIBE is a prefix match.
CONTROL_TOPIC = "__ts2py__"

# The five quantity fields, EasyLanguage's reserved words verbatim. Read as
# REQUIRED, never with a default: a missing quantity must raise, because the
# alternative is writing a zero that is indistinguishable from a real one.
# That failure mode -- a plausible number nobody can audit after the fact --
# is the entire reason this protocol exists.
_QUANTITY_FIELDS = (
    "el_volume",
    "el_ticks",
    "el_upticks",
    "el_downticks",
    "el_open_interest",
)


def _quantities(data: dict[str, Any]) -> dict[str, int]:
    try:
        return {name: int(data[name]) for name in _QUANTITY_FIELDS}
    except KeyError as exc:
        raise ValueError(
            f"payload is missing {exc.args[0]!r}. This is likely a "
            f"publisher older than proto {PROTO_VERSION}; reinstall TS2Python.dll "
            f"and re-import the .ELD that shipped with it."
        ) from exc


class _SequenceTracker:
    """Per-(sid, topic) gap detection.

    PUB/SUB drops silently at both high-water marks, so a missing message
    looks exactly like a quiet market. The publisher stamps a per-symbol
    monotonic ``seq`` and a per-process ``sid``; comparing them against what
    we expected is the only way to notice loss.

    Keyed by (sid, topic) rather than by topic alone, because the transport
    fans in from N TradeStation chart processes at once (one `orchart.exe`
    per open chart), each minting its own `sid`, all interleaved on this one
    socket. A single scalar `sid` — correct when there was exactly one
    publisher — read every OTHER process's frame as "the publisher
    restarted": `_expected` was cleared on every single frame, every frame
    re-established a fresh baseline, and no gap was ever reported.
    `messages_lost` read 0 forever — a stream that reads as perfect health
    while detection is silently dead. See contract/semantics.md §6.3.

    Sequences are per symbol because a subscriber may filter on one topic —
    a global counter's gaps would be indistinguishable from traffic it never
    asked for. ``tick`` and ``bar`` share a symbol's counter since they
    interleave on the same topic.

    A topic can have MORE THAN ONE LIVE sid at once, not just a superseded
    one replaced by a current one. ``__ts2py__`` is not the exception, it is
    the ordinary case: every open chart process sends its hello there, so N
    sids are live on that one topic simultaneously, interleaved frame by
    frame, for as long as N charts are open — this is steady state, not a
    transition. The same happens on a symbol topic when two TradeStation
    processes both have that chart open. Remembering only "the last sid
    seen" and discarding its predecessor's expectation on every alternation
    reintroduces the exact bug this class exists to fix, just narrowed to
    topics with more than one live publisher instead of every topic: A's
    frame evicts B's baseline, B's next frame evicts A's, and every single
    frame re-baselines instead of ever comparing against one. So this class
    remembers every sid ever seen per topic, and prunes nothing — see
    ``observe()``.
    """

    def __init__(self) -> None:
        # Every sid ever seen publishing each topic. Sets, not "the last
        # sid": see the class docstring for why a scalar is unsound here.
        self._sids_by_topic: dict[str, set[int]] = {}
        # Expected next seq, keyed by the (sid, topic) pair that produced
        # it. Entries are NEVER removed. A topic can have more than one LIVE
        # sid at once (see above), so there is no sid whose entry is ever
        # safe to evict on sight — the previous version tried, by popping
        # the just-superseded sid's entry, and that was the bug. A sid that
        # genuinely stops publishing just leaves its entry unmatched
        # forever; the cost is one int per (sid, topic) ever observed, which
        # for a consumer running a year across daily TradeStation restarts
        # is a few thousand entries — noise next to the alternative.
        self._expected: dict[tuple[int, str], int] = {}
        self.messages_lost = 0

    @property
    def gap_detection_available(self) -> bool:
        """True once at least one (sid, topic) pair has been observed.

        `_sids_by_topic` only ever gains entries, so non-empty means exactly
        "a sequenced frame has arrived, for some topic, at some point" —
        what the provider needs to tell "nothing was lost" from "loss
        cannot be detected here".
        """
        return bool(self._sids_by_topic)

    def observe(self, symbol: str, seq: int, sid: int) -> None:
        """Record one message, accumulating any gap into ``messages_lost``.

        Deliberately returns nothing: every gap is logged and counted here,
        and ``messages_lost`` is the accumulator callers read. A per-call
        return value would look like a hook something downstream acts on,
        and nothing does.
        """
        seen = self._sids_by_topic.setdefault(symbol, set())
        if sid not in seen:
            if seen:
                # A sid we have not seen before is now publishing a topic
                # that already had at least one other sid. That fact alone
                # does not say WHY: it is identical on the wire whether this
                # topic's one process restarted (old sid gone, new one
                # replacing it) or a second process joined it (old sid still
                # live too, e.g. two TradeStation processes with the same
                # chart open) — `seq` cannot tell the two apart, so neither
                # does this log. `known_sids` (before adding this one) is
                # everything already on record for the topic; read growth
                # over shrinkage as the more likely story on `__ts2py__`,
                # where N sids alive at once is the normal steady state.
                log.info(
                    "publisher_session_changed",
                    extra={"topic": symbol, "new_sid": sid, "known_sids": sorted(seen)},
                )
            seen.add(sid)

        key = (sid, symbol)
        expected = self._expected.get(key)
        self._expected[key] = seq + 1

        if expected is None:
            # First message seen for this (sid, topic) pair. A late
            # subscriber joining at seq=21 did not lose 20 messages — it was
            # not listening for them. Establish the baseline silently,
            # whether this is a brand-new topic or a new sid joining one
            # that is already live.
            log.debug("sequence_baseline", extra={"symbol": symbol, "seq": seq, "sid": sid})
            return

        if seq == expected:
            return

        if seq < expected:
            # TCP preserves per-publisher order, so this is a duplicate or a
            # replay rather than reordering. Do not rewind the expectation.
            log.warning(
                "sequence_regressed",
                extra={"symbol": symbol, "seq": seq, "expected": expected, "sid": sid},
            )
            self._expected[key] = expected
            return

        lost = seq - expected
        self.messages_lost += lost
        log.warning(
            "sequence_gap",
            extra={
                "symbol": symbol,
                "expected": expected,
                "received": seq,
                "lost": lost,
                "lost_total": self.messages_lost,
                "sid": sid,
            },
        )


class TradeStationELProvider:
    """
    Subscribes to events published by the TS2Python C++ DLL over ZeroMQ.

    Wire format (see ../../../../contract/wire.md):
      Frame 1: topic = symbol (UTF-8 bytes, e.g. b"SPY", b"VXX")
      Frame 2: JSON payload. One shape, whatever the chart is:

          {
            "proto":  2,
            "seq":    <int>,       # per-symbol, monotonic
            "sid":    <int>,       # publisher session; changes on restart
            "ts":     <float>,     # DLL receive clock, UTC epoch
            "ts_str": "<str>",     # EL Date+Time, ET wall clock. AUTHORITATIVE
            "bar_type":     <int>, # EL BarType, verbatim
            "bar_interval": <int>, # EL BarInterval, verbatim
            "category":     <int>, # EL Category, verbatim
            "o": <float>, "h": <float>, "l": <float>, "c": <float>,
            "el_volume": <int>, "el_ticks": <int>, "el_upticks": <int>,
            "el_downticks": <int>, "el_open_interest": <int>,
            "bid": <float|null>, "ask": <float|null>
          }

      There is no `kind` and no `tf`. The publisher used to split tick from
      bar and map BarType/BarInterval to a timeframe name, refusing any pair
      it could not name — three decisions taken off the wire, where nothing
      downstream could see them.

      One other topic exists, and the topic is what tells them apart:

          topic = "__ts2py__"    a chart announcing itself, from EL_InitChart
          {
            "proto": 2, "seq": <int>, "sid": <int>, "ts": <float>,
            "symbol": "<str>", "category": <int>,
            "bar_type": <int>, "bar_interval": <int>
          }

      Subscribing to it is not optional. The publisher's socket is XPUB, and
      EL_InitChart returns -7 — publishing nothing — until it sees a subscriber
      on this topic. A consumer that does not subscribe here leaves every
      TradeStation chart waiting indefinitely.
    """

    source_id = "tradestation_el"

    def __init__(
        self,
        # The hub's XPUB port. 5555 is the hub's XSUB side, where the chart
        # processes connect — a SUB pointed there is an incompatible socket
        # pair, which shows up as silence rather than an error.
        endpoint: str = "tcp://127.0.0.1:5556",
        *,
        context: zmq.asyncio.Context | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._ctx = context
        self._ctx_owned = context is None
        self._socket: zmq.asyncio.Socket | None = None
        self._subscribed: set[str] = set()
        self._closed = False
        self._seq = _SequenceTracker()
        self._frames_refused = 0
        # Every frame taken off the socket, hello and point alike, counted
        # before any parsing or filtering. See `frames_received` below.
        self._frames_received = 0
        # Charts the publisher has announced: (symbol, bar_type, bar_interval)
        # -> category. Exposed for tests and for anything that wants to know
        # what is actually attached rather than what was configured. The key
        # deliberately excludes `sid` — this property answers "what charts
        # are attached", and chart identity does not include which publisher
        # process attached it.
        self._announced_charts: dict[tuple[str, int, int], int] = {}
        # Which sids have announced each chart identity. A chart appearing
        # under a second, different sid means two TradeStation processes
        # have it open — both publish, so every point on that topic arrives
        # twice, and nothing in `seq` can reveal it (each process's stream
        # is internally contiguous). Tracked as a set rather than "the last
        # sid" so a chart's original sid re-announcing (the ordinary hub
        # replay-on-attach case) never re-triggers the warning; only a
        # genuinely new sid for a chart already claimed does.
        self._chart_sids: dict[tuple[str, int, int], set[int]] = {}

    @property
    def announced_charts(self) -> dict[tuple[str, int, int], int]:
        """What the publisher says is attached, keyed by chart identity.

        This is the workspace as TradeStation has it, which is not the same
        question as `symbols.yaml`: a chart here that is not subscribed
        produces no data, and a subscribed symbol with no chart here is not
        publishing at all.
        """
        return dict(self._announced_charts)

    @property
    def frames_refused(self) -> int:
        """Frames received and thrown away because they could not be parsed.

        Read this WITH `messages_lost`, never instead of it. They answer
        different questions and the pair is what tells you the link is
        healthy: `messages_lost` counts frames the publisher sent that never
        arrived, and a refused frame did arrive — so a stream in which every
        single frame was refused still reports zero lost, quite correctly,
        and reads as perfect health on its own.

        That is not hypothetical. The documented upgrade order is binding
        first, then DLL, so there is a window where the old DLL is still
        publishing. Its frames carry `seq`/`sid`, so sequence tracking starts
        normally and reports no loss, while the `proto` gate refuses every
        one of them and nothing is delivered.
        """
        return self._frames_refused

    @property
    def frames_received(self) -> int:
        """Every frame taken off the socket — hello and point alike.

        Counted before any parsing, topic filtering or proto check, so this
        is the one counter that answers "did the transport deliver anything
        at all". Its purpose is telling "the transport is dead" (this stays
        0 — no hub, wrong port, both ends bound instead of one bound one
        connected) apart from "the market is quiet" (frames — hellos at
        least — do arrive; specific symbols just are not trading). Neither
        `messages_lost` nor `frames_refused` can make that distinction: both
        require at least one frame to have arrived before they can report
        anything.
        """
        return self._frames_received

    @property
    def endpoint(self) -> str:
        """The address this provider connects to.

        Read-only, and touched by nothing inside this module after
        connect() — it exists so a caller (the ingestion runtime's
        `wire_silent` diagnostic) can name the endpoint in a log line
        without reaching into a private attribute.
        """
        return self._endpoint

    @property
    def gap_detection_available(self) -> bool:
        """True once a frame carrying ``seq``/``sid`` has arrived.

        Every frame in this protocol is sequenced, so this only distinguishes
        "counting has started" from "nothing has been received yet" — which
        still matters, because ``messages_lost == 0`` before the first frame
        is not a statement about the link. See semantics.md §6.6.
        """
        return self._seq.gap_detection_available

    @property
    def messages_lost(self) -> int | None:
        """Messages the publisher sent but this subscriber never received.

        ``None`` means *cannot tell*, and is not the same answer as ``0``.
        semantics.md §6.6 requires a caller to be able to separate the two:
        against a still-deployed v1 DLL there is no ``seq`` on the wire, so a
        plain 0 would let a whole trading day be filed as "verified complete"
        when gap detection was never running. Pair with
        ``gap_detection_available`` when the distinction needs a name.
        """
        if not self.gap_detection_available:
            return None
        return self._seq.messages_lost

    async def connect(self) -> None:
        if self._socket is not None:
            return
        if self._ctx is None:
            self._ctx = zmq.asyncio.Context()
            self._ctx_owned = True
        self._socket = self._ctx.socket(zmq.SUB)
        # Default RCVHWM is 1000 — PUB/SUB silently drops past that when the
        # subscriber falls behind (open-bell bursts, FOMC). Must be set
        # before connect(); changes after connect have no effect.
        self._socket.setsockopt(zmq.RCVHWM, 1_000_000)
        # The control topic is subscribed unconditionally, before any symbol.
        # The publisher's EL_InitChart blocks on a subscriber being attached HERE —
        # it returns -7 and publishes nothing until one is, so a consumer that
        # skipped this would leave every chart waiting forever.
        self._socket.setsockopt_string(zmq.SUBSCRIBE, CONTROL_TOPIC)
        self._socket.connect(self._endpoint)
        log.info("TradeStationELProvider connected to %s", self._endpoint)

    async def subscribe(self, symbols: list[str]) -> None:
        if self._socket is None:
            raise RuntimeError("connect() must be called before subscribe()")
        for sym in symbols:
            if sym in self._subscribed:
                continue
            self._socket.setsockopt_string(zmq.SUBSCRIBE, sym)
            self._subscribed.add(sym)
            log.debug("Subscribed to topic %s", sym)

    async def events(self) -> AsyncIterator[MarketEvent]:
        """Yield every decoded point until close()."""
        if self._socket is None:
            raise RuntimeError("connect() must be called before events()")
        socket = self._socket
        while not self._closed:
            try:
                topic_bytes, payload_bytes = await socket.recv_multipart()
            except zmq.error.ContextTerminated:
                return
            except zmq.error.ZMQError as exc:
                # mypy can't see that close() may flip _closed from
                # another task during the await above, so it flags
                # the `return` as unreachable (the while loop's
                # condition already gates on `not self._closed`).
                # The check is correct under concurrency — silence
                # the false positive on the unreachable branch.
                if self._closed:
                    return  # type: ignore[unreachable]
                log.warning("zmq recv error: %s", exc)
                continue

            # Counted for every frame that actually made it off the socket,
            # before any parsing or topic filtering below — a refused or
            # dropped-as-mismatched frame still proves the transport itself
            # is alive. See `frames_received`.
            self._frames_received += 1

            topic = topic_bytes.decode("utf-8", errors="replace")
            if topic == CONTROL_TOPIC:
                # A chart announcing itself. Handled here rather than yielded:
                # MarketEvent is Bar, and a hello is not a data point — it
                # carries no OHLC, no quantities and no quote. Nothing
                # downstream of the provider needs to grow a second case.
                self._handle_hello(payload_bytes)
                continue

            symbol = topic
            # semantics.md §5: ZMQ SUBSCRIBE is a prefix match, so a
            # subscription to "SPY" also delivers every SPYG frame from the
            # same publisher. Without an exact-equality pass the binding would
            # decode those, hand them to the runtime, and start writing
            # data/ticks/symbol=SPYG/ for a symbol nobody asked for.
            if symbol not in self._subscribed:
                log.debug("topic_prefix_mismatch_dropped", extra={"topic": symbol})
                continue
            try:
                event = self._parse_payload(symbol, payload_bytes)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                # The expected shape of a bad frame: a refused proto, a
                # missing quantity, malformed JSON.
                self._frames_refused += 1
                log.warning(
                    "Dropping malformed message for symbol=%s: %s (payload=%r)",
                    symbol,
                    exc,
                    payload_bytes[:200],
                )
                continue
            except Exception as exc:
                # Anything else is either an input shape nobody predicted or a
                # bug in the parser, and both used to be fatal in the worst
                # way: the exception left this generator, killed the ingest
                # task, and `run()` never noticed because it sits on
                # `self._stop.wait()` and only awaits the tasks after stop is
                # set. The process kept running, the heartbeat kept logging,
                # and nothing was ingested again until somebody noticed the
                # silence. `int(data[name])` on a JSON null raises TypeError;
                # a payload decoding to a non-object makes `data.get("seq")`
                # raise AttributeError. Neither was caught.
                #
                # One frame must never be able to end the stream. ERROR with
                # a traceback rather than the WARNING above, because unlike a
                # malformed frame this may well be our own defect and should
                # not read as routine.
                self._frames_refused += 1
                log.error(
                    "Dropping unparseable message for symbol=%s: %s (payload=%r)",
                    symbol,
                    exc,
                    payload_bytes[:200],
                    exc_info=True,
                )
                continue
            yield event

    def _handle_hello(self, payload: bytes) -> None:
        """Record and report one chart announcing itself.

        Sent by the publisher's ``EL_InitChart``, once per chart, and again for
        every chart whenever a subscriber attaches — so restarting this
        process re-learns the whole workspace without TradeStation being
        touched.

        Never raises. A malformed hello is worth a line in the log and
        nothing more: it is not a data point, so dropping it loses no
        market data, and letting it escape would kill the ingest task.
        """
        try:
            data = json.loads(payload)
            seq = data.get("seq")
            sid = int(data.get("sid", 0))
            if seq is not None:
                self._seq.observe(CONTROL_TOPIC, int(seq), sid)

            proto = data.get("proto")
            if proto != PROTO_VERSION:
                log.warning(
                    "chart_announcement_refused",
                    extra={"proto": proto, "expected": PROTO_VERSION},
                )
                return

            symbol = data["symbol"]
            # Not str(): a JSON null would become the string "None" and
            # register a chart under that name, which then reads as a real
            # symbol nobody subscribed to.
            if not isinstance(symbol, str) or not symbol:
                raise ValueError(f"hello carries a non-string symbol: {symbol!r}")
            category = int(data["category"])
            bar_type = int(data["bar_type"])
            bar_interval = int(data["bar_interval"])
        except Exception as exc:
            self._frames_refused += 1
            log.error(
                "Dropping unparseable chart announcement: %s (payload=%r)",
                exc,
                payload[:200],
                exc_info=True,
            )
            return

        fields = {
            "symbol": symbol,
            "category": category,
            "bar_type": bar_type,
            "bar_interval": bar_interval,
        }
        chart_key = (symbol, bar_type, bar_interval)
        self._announced_charts[chart_key] = category

        # `announced_charts`'s key deliberately excludes `sid` (chart identity
        # is not a publisher session), so a chart claimed by a second process
        # is invisible there. This is the only place that can still see it —
        # both processes would publish under the same topic with their own
        # internally-contiguous `seq`, so no gap ever appears either.
        #
        # SAY WHAT IS OBSERVABLE, NOT WHY. A sid this chart has not used before
        # has two causes and the wire cannot tell them apart: its process
        # restarted (the old sid is dead, nothing is duplicated), or a second
        # process opened the same chart (everything on that topic arrives
        # twice). Claiming the second one would put a false "your data is
        # duplicated" line against EVERY open chart on the routine event of
        # TradeStation restarting under a consumer that stayed up — and an
        # operator who learns to scroll past N of those will scroll past the
        # real one too. `_SequenceTracker.observe` hedges the identical signal
        # for the identical reason; see contract/semantics.md §6.3.
        known_sids = self._chart_sids.setdefault(chart_key, set())
        if known_sids and sid not in known_sids:
            log.warning(
                "chart_announced_under_new_sid",
                extra={
                    **fields,
                    "new_sid": sid,
                    "known_sids": sorted(known_sids),
                    "note": (
                        "this chart was announced under a sid it had not used "
                        "before - either its TradeStation process restarted "
                        "(nothing is duplicated) or a second process has the "
                        "same chart open (every point on this topic arrives "
                        "twice); the wire cannot tell these apart"
                    ),
                },
            )
        known_sids.add(sid)

        if symbol in self._subscribed:
            log.info("chart_announced_now_receiving", extra=fields)
            return

        # Announced, but this consumer never subscribed to that symbol, so
        # not one of its points will arrive. Saying "now receiving" here
        # would be false, and silence would leave an operator watching an
        # empty partition with no idea why.
        log.warning(
            "chart_announced_but_not_subscribed",
            extra={
                **fields,
                # ASCII only: this lands in a Windows console, where the
                # default codepage turns an em-dash into mojibake.
                "note": (
                    "no data will be received for this chart - the symbol is "
                    "not in symbols.yaml. Add it and restart."
                ),
            },
        )

    def _parse_payload(self, symbol: str, payload: bytes) -> MarketEvent:
        data = json.loads(payload)

        # Sequence accounting happens before the version gate on purpose. A
        # frame we refuse still occupied a slot in the publisher's per-symbol
        # counter; skipping observe() would leave `_expected` parked at the
        # last accepted seq, and the next accepted frame would then report a
        # fabricated gap. An operator upgrading the DLL past this binding
        # would watch a link that lost nothing report steady message loss.
        seq = data.get("seq")
        if seq is not None:
            self._seq.observe(symbol, int(seq), int(data.get("sid", 0)))

        proto = data.get("proto")
        if proto != PROTO_VERSION:
            # Absent is the common case and the informative one: a publisher
            # predating this protocol has no such key at all. Saying so beats
            # "unsupported version None", which reads like a corrupt frame.
            raise ValueError(
                f"payload declares proto={proto!r}, expected {PROTO_VERSION}. "
                f"A missing 'proto' means the publisher predates this protocol; "
                f"reinstall TS2Python.dll and re-import the .ELD that shipped "
                f"with it."
            )

        # `seq` is REQUIRED — both wire schemas say so and the conformance
        # suite validates the fixtures against them, but nothing enforced it
        # at runtime: `data.get("seq")` above skips silently when it is
        # absent. A proto-1 frame without one then parsed normally, `sid`
        # stayed None, `messages_lost` returned None forever, and the one-shot
        # warning that used to say so was deleted with
        # `_warned_no_gap_detection`. An operator running an alternate
        # publisher — or a DLL build where `reserve_seq` regressed — collects
        # a full day, reads a `messages_lost` of None as "nothing to report",
        # and files it verified-complete while high-water-mark drops went
        # uncounted. That is the conflation §6.6 exists to forbid.
        #
        # Checked here rather than beside observe() above so a superseded
        # publisher's frame — which does carry seq — still gets the protocol
        # message, which is the one its operator can act on.
        if "seq" not in data:
            raise ValueError(
                f"proto {PROTO_VERSION} payload carries no 'seq'. Every frame in "
                f"this protocol is sequenced; without it, loss cannot be detected "
                f"and a clean-looking run would be unverifiable."
            )

        return self._parse_point(symbol, data, payload)

    def _parse_point(self, symbol: str, data: dict[str, Any], payload: bytes) -> Bar:
        """One frame shape, one parse. No `kind`, no tf allow-list.

        The wire used to carry two shapes discriminated by `kind`, and the
        bar shape carried a `tf` string the DLL had derived from BarType and
        BarInterval — refusing, with rc -5, any combination it could not
        name. Both were the publisher deciding. `bar_type` and `bar_interval`
        now travel as EasyLanguage reports them and nothing is refused for
        being an interval this binding has no name for.
        """
        # Priority for bar_time (UTC) — semantics.md §1.1:
        #   1. ts_str (authoritative) — EL wall-clock string, parsed here
        #      as America/New_York. Zone-correct on any DLL host because
        #      we never rely on the host's system tz.
        #   2. ts — receive-side wall clock, last-resort only, and ONLY when
        #      the publisher sent no ts_str at all.
        #
        # "Absent" and "present but unparseable" are two different states and
        # must not share a path. The publisher no longer parses ts_str, so the
        # DLL-side format check that used to catch a bad string is gone; this
        # is the only place left that can notice. Falling back on a string we
        # could not read is what makes that failure silent AND wrong: `ts` is
        # the receive clock, so during a chart replay every point of a session
        # arrives within the same minute, collapses onto one bar_time, and the
        # runtime's dedupe discards all but one — a whole session reduced to a
        # single plausible-looking bar in today's partition. That is the zh-TW
        # FormatTime("tt") incident this repo already shipped once.
        bar_time: datetime | None = None

        ts_str_raw = data.get("ts_str")
        if isinstance(ts_str_raw, str) and ts_str_raw:
            bar_time = _parse_el_str_as_et(ts_str_raw)
            if bar_time is None:
                # Refuse. events() logs the payload and drops the frame; the
                # stream survives, and no invented timestamp reaches storage.
                raise ValueError(
                    f"payload carries an unparseable 'ts_str': {ts_str_raw!r}. "
                    f"Expected {_EL_TS_FORMAT_HUMAN}, read as America/New_York. "
                    f"A localised or reformatted time string from the indicator "
                    f"is the usual cause; the DLL no longer validates it. "
                    f"(payload={payload[:200]!r})"
                )

        if bar_time is None:
            # No ts_str at all. §1.1 allows the degradation but requires it be
            # recorded — an operator seeing this on every frame is looking at a
            # publisher that will collapse any replay onto one bucket.
            log.warning(
                "ts_str_absent_using_recv_clock",
                extra={"symbol": symbol},
            )
            bar_time = _floor_to_minute_utc(float(data["ts"]))

        # The timestamp is EasyLanguage's, verbatim. Nothing here shifts it or
        # snaps it to a grid — see the Bar docstring and semantics.md §2 for
        # the bar that cost.
        return Bar(
            symbol=symbol,
            bar_time=bar_time,
            bar_type=int(data["bar_type"]),
            bar_interval=int(data["bar_interval"]),
            category=int(data["category"]),
            open=float(data["o"]),
            high=float(data["h"]),
            low=float(data["l"]),
            close=float(data["c"]),
            bid=_quote_or_none(data.get("bid")),
            ask=_quote_or_none(data.get("ask")),
            ts=float(data["ts"]),
            **_quantities(data),
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        if self._ctx is not None and self._ctx_owned:
            self._ctx.term()
            self._ctx = None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _quote_or_none(value: object) -> float | None:
    """Read a bid/ask, treating "no quote" as absent however it is spelled.

    The publisher already sends null when EL had no quote to report
    (historical replay, or a symbol that never carries one). The
    non-positive check is belt-and-braces for any value that gets past it —
    a $0.00 quote is never a real one. See contract/semantics.md §3.
    """
    q = _optional_float(value)
    if q is None or q <= 0.0:
        return None
    return q


def _floor_to_minute_utc(epoch_seconds: float) -> datetime:
    ts = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return ts.replace(second=0, microsecond=0)


def _parse_el_str_as_et(s: str) -> datetime | None:
    """Parse EL TsStr ``yyyy-MM/dd-HH:mm:ss`` (24-hour) as ET, return
    UTC-aware datetime floored to the minute. Returns None on any parse
    failure — the caller refuses the frame rather than substituting a
    guess. DST is resolved by ZoneInfo from the parsed local fields.

    24-hour format is deliberate: the prior ``hh:mm:ss tt`` format broke
    on zh-TW Windows hosts where ``FormatTime("tt")`` emits localized
    AM/PM ("上午"/"下午") that neither C's sscanf nor Python's %p can
    match — every bar would then fall through to the receive-time ``ts``
    fallback and collapse onto today's date partition. That fallback is
    now a refusal, so the same regression fails loudly instead.
    """
    try:
        local = datetime.strptime(s, _EL_TS_FORMAT)
    except (TypeError, ValueError):
        return None
    aware_et = local.replace(tzinfo=_ET_TZ)
    _warn_if_dst_ambiguous(aware_et, s)
    utc_dt = aware_et.astimezone(UTC)
    return utc_dt.replace(second=0, microsecond=0)


def _warn_if_dst_ambiguous(aware_et: datetime, raw: str) -> None:
    """Say so when a local time does not name exactly one instant.

    `replace(tzinfo=...)` pins `fold=0`, so the repeated hour on the
    fall-back date resolves to its FIRST occurrence and the skipped hour on
    the spring-forward date resolves to an instant that never happened.
    Neither raises, and both produce a timestamp that looks entirely ordinary.

    fold=0 is kept rather than guessed at, because the wire genuinely cannot
    settle it: `ts_str` is a local wall-clock string with no offset and no
    fold bit, so the information required to pick the right instant is not
    present in the frame. A second binding faces the same choice, which is
    why the rule is written down in contract/semantics.md §2.0.1 rather than
    only here. What was wrong was doing it silently.

    Unreachable for a normal US equity session — the extended session runs
    04:00-20:00 ET and the repeated hour is 01:00-02:00 — but the binding
    accepts whatever the chart sends, and TradeStation offers 24-hour session
    templates.
    """
    if aware_et.utcoffset() == aware_et.replace(fold=1).utcoffset():
        return
    log.warning(
        "el_timestamp_dst_ambiguous",
        extra={
            "ts_str": raw,
            "resolved_utc": aware_et.astimezone(UTC).isoformat(),
            "note": "local time maps to two instants (or none); took fold=0",
        },
    )
