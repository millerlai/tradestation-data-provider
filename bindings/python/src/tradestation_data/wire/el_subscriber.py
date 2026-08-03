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
    """Per-symbol gap detection.

    PUB/SUB drops silently at both high-water marks, so a missing message
    looks exactly like a quiet market. The publisher stamps a per-symbol
    monotonic ``seq`` and a per-session ``sid``; comparing them against what
    we expected is the only way to notice loss.

    Sequences are per symbol because a subscriber may filter on one topic —
    a global counter's gaps would be indistinguishable from traffic it never
    asked for. ``tick`` and ``bar`` share a symbol's counter since they
    interleave on the same topic.

    ``sid`` stays None until a sequenced frame arrives, which is what lets
    the provider tell "nothing was lost" from "loss cannot be detected here".
    """

    def __init__(self) -> None:
        self.sid: int | None = None
        self._expected: dict[str, int] = {}
        self.messages_lost = 0

    def observe(self, symbol: str, seq: int, sid: int) -> None:
        """Record one message, accumulating any gap into ``messages_lost``.

        Deliberately returns nothing: every gap is logged and counted here,
        and ``messages_lost`` is the accumulator callers read. A per-call
        return value would look like a hook something downstream acts on,
        and nothing does.
        """
        if sid != self.sid:
            # New publisher session: counters restarted at the source, so a
            # low seq here is a restart rather than 4 billion lost messages.
            if self.sid is not None:
                log.info(
                    "publisher_session_changed",
                    extra={"old_sid": self.sid, "new_sid": sid, "symbol": symbol},
                )
            self.sid = sid
            self._expected = {}

        expected = self._expected.get(symbol)
        self._expected[symbol] = seq + 1

        if expected is None:
            # First message seen for this symbol. A late subscriber joining
            # at seq=21 did not lose 20 messages — it was not listening for
            # them. Establish the baseline silently.
            log.debug("sequence_baseline", extra={"symbol": symbol, "seq": seq})
            return

        if seq == expected:
            return

        if seq < expected:
            # TCP preserves per-publisher order, so this is a duplicate or a
            # replay rather than reordering. Do not rewind the expectation.
            log.warning(
                "sequence_regressed",
                extra={"symbol": symbol, "seq": seq, "expected": expected},
            )
            self._expected[symbol] = expected
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

    """

    source_id = "tradestation_el"

    def __init__(
        self,
        endpoint: str = "tcp://127.0.0.1:5555",
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
    def gap_detection_available(self) -> bool:
        """True once a frame carrying ``seq``/``sid`` has arrived.

        Every frame in this protocol is sequenced, so this only distinguishes
        "counting has started" from "nothing has been received yet" — which
        still matters, because ``messages_lost == 0`` before the first frame
        is not a statement about the link. See semantics.md §6.6.
        """
        return self._seq.sid is not None

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

            symbol = topic_bytes.decode("utf-8", errors="replace")
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
            bar_time = _recv_clock_utc(float(data["ts"]))

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


def _recv_clock_utc(epoch_seconds: float) -> datetime:
    """The `ts_str`-absent fallback: the receive clock, seconds and all.

    This used to floor to the minute, matching the old §2.1. Keeping that
    after §2.1 reversed would make the two paths answer the same question
    differently — and in the worse direction: on a sub-minute chart,
    flooring is what collapses a minute's bars onto one `bar_time`, which
    is exactly what `_handle_provider_bar`'s buffer then reads as an
    intra-bar update and drops. The fallback is degraded already; there is
    no reason to degrade it further.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)


def _parse_el_str_as_et(s: str) -> datetime | None:
    """Parse EL TsStr ``yyyy-MM/dd-HH:mm:ss`` (24-hour) as ET, return a
    UTC-aware datetime — **seconds included**. Returns None on any parse
    failure — the caller refuses the frame rather than substituting a
    guess. DST is resolved by ZoneInfo from the parsed local fields.

    The seconds used to be floored to zero here (semantics.md §2.1, since
    reversed). That was a no-op for as long as the publisher built TsStr
    from EL's ``Date``/``Time``, which carry no seconds at all — but the
    publisher now uses ``BarDateTime``, which does. Flooring a real value
    would collapse a 30-second chart's two bars per minute onto one
    ``bar_time``, and ``_handle_provider_bar`` would then read the second
    as an intra-bar update of the first and drop it. semantics.md §1.3
    has the live measurement.

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
    return aware_et.astimezone(UTC)


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
