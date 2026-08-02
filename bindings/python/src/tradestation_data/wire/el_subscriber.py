from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import zmq
import zmq.asyncio

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.domain.timeframe import (
    SESSION_ANCHORED_TIMEFRAMES,
    SUPPORTED_TIMEFRAMES,
    align_bucket_start,
)
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

# Symbols TradeStation emits as indices / breadth, with no bid/ask/volume
# semantics. The wire carries bid/ask as plain floats for these (usually 0.0
# or stale), so the invalidation has to happen here — it is a contract rule,
# not a local convenience. See ../../../../contract/semantics.md §3.
DEFAULT_INDEX_SYMBOLS: frozenset[str] = frozenset(
    {"$TICK", "$ADD", "$VOLD", "$TRIN", "$PCVA", "VXX"}
)

# Timeframes this binding will accept on the wire live in
# domain.timeframe.SUPPORTED_TIMEFRAMES — deliberately the same vocabulary the
# storage layer partitions on. A `tf` we cannot place is a frame we must not
# file, because filing it under a default would put bars of one interval into
# another interval's partition.

# The protocol version, carried in `proto`. There is exactly one, and a frame
# without the key is not this protocol at all.
#
# The key is `proto` rather than `v` on purpose. The superseded wire used `v`
# and counted to 4; restarting at 1 under the same key would have made
# {"v":1} a legal opening for both protocols, and the frames would then have
# failed in the worst possible way — the old v1 bar used kind "bar_1m", which
# the unknown-kind rule skips silently, while an old v1 tick would have
# matched on shape and only diverged at field level. See contract/wire.md.
PROTO_VERSION = 1

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


def _quantities(data: dict[str, Any], payload_kind: str) -> dict[str, int]:
    try:
        return {name: int(data[name]) for name in _QUANTITY_FIELDS}
    except KeyError as exc:
        raise ValueError(
            f"{payload_kind} payload is missing {exc.args[0]!r}. This is likely a "
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
      Frame 2: JSON payload. Two shapes, discriminated by ``kind``:

        Tick (EL_PublishTick):
          {
            "proto":  1,
            "seq":    <int>,      # monotonic sequence per symbol
            "sid":    <int>,      # publisher session id, epoch microseconds
            "kind":   "tick",
            "ts":     <float>,    # DLL receive time, unix epoch UTC
            "ts_str": "<str>",    # Raw EL timestamp "yyyy-MM/dd-HH:mm:ss"
            "px":     <float>,    # last trade price
            "el_volume": <int>, "el_ticks": <int>, "el_upticks": <int>,
            "el_downticks": <int>, "el_open_interest": <int>,
            "bid":    <float>,    # null when no quote
            "ask":    <float>
          }

        Bar (EL_PublishBar, complete OHLC):
          {
            "proto":  1,
            "seq":    <int>,
            "sid":    <int>,
            "kind":   "bar",
            "tf":     "<str>",    # timeframe, e.g. "1m", "5m", "1d"
            "ts":     <float>,
            "ts_str": "<str>",
            "o": <float>, "h": <float>, "l": <float>, "c": <float>,
            "el_volume": <int>, "el_ticks": <int>, "el_upticks": <int>,
            "el_downticks": <int>, "el_open_interest": <int>
          }

    The DLL stamps ``ts`` when the EL call lands — for live ticks this is
    within a millisecond of the exchange event, and semantics.md §1 makes it
    a tick's authoritative time. ``bar.bucket_start`` instead comes from
    ``ts_str``, parsed here as ET: it is the publisher's raw fact, and
    parsing it locally keeps the answer correct even if the DLL host's tz
    database is stale. Bars carry no quote.
    """

    source_id = "tradestation_el"

    def __init__(
        self,
        endpoint: str = "tcp://127.0.0.1:5555",
        *,
        index_symbols: frozenset[str] | None = None,
        context: zmq.asyncio.Context | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._index_symbols = index_symbols if index_symbols is not None else DEFAULT_INDEX_SYMBOLS
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
        """Yield every decoded event (Tick or Bar) until close()."""
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

    async def ticks(self) -> AsyncIterator[Tick]:
        """Tick-only convenience view. Silently drops Bar events."""
        async for event in self.events():
            if isinstance(event, Tick):
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

        kind = data.get("kind")
        if kind == "tick":
            return self._parse_tick(symbol, data)
        if kind == "bar":
            # An unknown interval must be refused, never defaulted. A bar
            # filed under the wrong timeframe= partition is undetectable
            # downstream — it looks exactly like real data at that interval.
            tf_val = data.get("tf")
            if not tf_val:
                raise ValueError(f"bar payload carries no 'tf': {payload!r}")
            tf = str(tf_val)
            if tf not in SUPPORTED_TIMEFRAMES:
                raise ValueError(f"Unsupported timeframe: {tf!r}")
            return self._parse_bar(symbol, data, tf)
        raise ValueError(f"Unknown event kind: {kind!r}")

    def _parse_tick(self, symbol: str, data: dict[str, Any]) -> Tick:
        timestamp = datetime.fromtimestamp(float(data["ts"]), tz=UTC)

        # Two independent reasons a quote can be meaningless, and both apply:
        # the wire says null when EL had none to report (§3.1), and an
        # index/breadth symbol's live numbers mean nothing even when present
        # (§3.2). The DLL cannot do the second — it holds no symbol taxonomy.
        is_index = symbol in self._index_symbols
        return Tick(
            symbol=symbol,
            timestamp=timestamp,
            price=float(data["px"]),
            bid=None if is_index else _quote_or_none(data.get("bid")),
            ask=None if is_index else _quote_or_none(data.get("ask")),
            **_quantities(data, "tick"),
        )

    def _parse_bar(self, symbol: str, data: dict[str, Any], timeframe: str = "1m") -> Bar:
        # Priority for bucket_start (UTC) — semantics.md §1.1:
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
        # the receive clock, so during a chart replay every bar of a session
        # arrives within the same minute, collapses onto one bucket_start, and
        # the runtime's dedupe discards all but one — a whole session reduced
        # to a single plausible-looking bar in today's partition. That is the
        # zh-TW FormatTime("tt") incident this repo already shipped once.
        bucket_start: datetime | None = None

        ts_str_raw = data.get("ts_str")
        if isinstance(ts_str_raw, str) and ts_str_raw:
            bucket_start = _parse_el_str_as_et(ts_str_raw)
            if bucket_start is None:
                # Refuse. events() logs the payload and drops the frame; the
                # stream survives, and no invented bucket reaches storage.
                raise ValueError(
                    f"bar payload carries an unparseable 'ts_str': {ts_str_raw!r}. "
                    f"Expected {_EL_TS_FORMAT_HUMAN}, read as America/New_York. "
                    f"A localised or reformatted time string from the indicator "
                    f"is the usual cause; the DLL no longer validates it."
                )

        if bucket_start is None:
            # No ts_str at all. §1.1 allows the degradation but requires it be
            # recorded — an operator seeing this on every frame is looking at a
            # publisher that will collapse any replay onto one bucket.
            log.warning(
                "bar_ts_str_absent_using_recv_clock",
                extra={"symbol": symbol, "timeframe": timeframe},
            )
            bucket_start = _floor_to_minute_utc(float(data["ts"]))

        # §2 — the wire is right-labelled, the contract is left-labelled.
        # EasyLanguage's `Time` is the bar's *close*, and TsStr is built from
        # it verbatim (EL/TS2Python_Exporter.el), so an RTH 1m session arrives
        # as 09:31…16:00 where §2 requires 09:30…15:59. Both are 390 bars and
        # both look correct in isolation — the whole series is simply shifted
        # one slot. Step back onto the left edge before the grid snap.
        #
        # Session-anchored frames are exempt: align_bucket_start replaces a 1d
        # timestamp with that session's 04:00 ET anchor outright, so a shift
        # here would only move the bar into the previous session.
        # Step back one MINUTE, not one whole `tf`. The bucket a bar belongs
        # to is the grid cell holding the instant just before its close, and
        # subtracting a full interval only finds that cell when the bar
        # actually spans one. A session-truncated bar does not: a 60-minute
        # RTH chart is six full bars plus a 15:30-16:00 stub that EL stamps
        # 16:00, and 16:00 minus 60m is 15:00, which floors onto the
        # 09:30-anchored hour grid at 14:30 — the *previous* bar's bucket.
        # `_handle_provider_bar` then reads the stub as an intra-bar refresh
        # and overwrites the real 14:30-15:30 hour with the half-hour's OHLC
        # and quantities.
        #
        # One minute is exact here rather than approximate: every candidate
        # is already minute-floored, by `_parse_el_str_as_et` or by
        # `_floor_to_minute_utc`. For a bar that does span its interval the
        # answer is identical to subtracting the interval, so this changes
        # nothing except the truncated case.
        if timeframe not in SESSION_ANCHORED_TIMEFRAMES:
            bucket_start -= timedelta(minutes=1)

        # §2.2 — the grid is the contract's, not the publisher's. EL stamps a
        # bar with its chart's own Date/Time, which for a daily bar is nowhere
        # near the 04:00 ET session anchor. Left alone, one trading day could
        # end up as two rows in bars/timeframe=1d/ with different
        # bucket_starts, and every join downstream would double-count it.
        bucket_start = align_bucket_start(bucket_start, timeframe)

        return Bar(
            symbol=symbol,
            bucket_start=bucket_start,
            open=float(data["o"]),
            high=float(data["h"]),
            low=float(data["l"]),
            close=float(data["c"]),
            timeframe=timeframe,
            **_quantities(data, "bar"),
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
    utc_dt = aware_et.astimezone(UTC)
    return utc_dt.replace(second=0, microsecond=0)
