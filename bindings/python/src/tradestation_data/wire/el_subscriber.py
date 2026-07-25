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
from tradestation_data.wire.base import MarketEvent

log = logging.getLogger(__name__)

# TradeStation US equity charts are always ET. EL's Date/Time reflect the
# chart timezone, so we treat the raw TsStr as ET explicitly rather than
# trusting the DLL's mktime() (which would interpret it via the TS host's
# Windows timezone — wrong whenever the operator's system isn't ET).
_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")

# Default set of symbols emitted by TradeStation as indices / breadth with
# no bid/ask/volume semantics. See docs/design.md §6.
DEFAULT_INDEX_SYMBOLS: frozenset[str] = frozenset(
    {"$TICK", "$ADD", "$VOLD", "$TRIN", "$PCVA", "VXX"}
)


class TradeStationELProvider:
    """
    Subscribes to events published by the TS2Python C++ DLL over ZeroMQ.

    Wire format (see docs/design.md §5):
      Frame 1: topic = symbol (UTF-8 bytes, e.g. b"SPY", b"VXX")
      Frame 2: JSON payload. Two shapes, discriminated by ``kind``:

        Tick (EL_PublishTick, default kind):
          {
            "v":      1,
            "kind":   "tick",     # optional — omitted in pre-v3 DLL builds
            "ts":     <float>,    # DLL receive time, unix epoch UTC
            "ts_utc": <float>,    # ET→UTC conversion done in the DLL via
                                  # std::chrono::zoned_time. 0.0 on parse fail.
                                  # v5+ DLL only; earlier builds shipped
                                  # ``ts_el`` here (host-local mktime, wrong
                                  # on non-ET hosts) and are no longer trusted.
            "ts_str": "<str>",    # Raw EL timestamp "yyyy-MM/dd-HH:mm:ss" 24h
                                  # in America/New_York wall-clock.
            "px":     <float>,    # last trade price
            "vol":    <int>,
            "bid":    <float>,    # null/ignored for index symbols
            "ask":    <float>,
            "tc":     <int>
          }

        Bar (EL_PublishTickEx, 1-min OHLC):
          {
            "v":      1,
            "kind":   "bar_1m",
            "ts":     <float>,
            "ts_utc": <float>,
            "ts_str": "<str>",
            "o":      <float>,
            "h":      <float>,
            "l":      <float>,
            "c":      <float>,
            "vol":    <int>,
            "bid":    <float>,
            "ask":    <float>,
            "tc":     <int>
          }

    The DLL stamps ``ts`` when the EL call lands — for live ticks this is
    within a millisecond of the exchange event. ``bar.bucket_start`` is
    parsed from ``ts_str`` as ET (authoritative); ``ts_utc`` from the DLL
    is used only as a sanity cross-check. Ticks still use ``ts`` as the
    authoritative receive-side timestamp.
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
            try:
                event = self._parse_payload(symbol, payload_bytes)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                log.warning(
                    "Dropping malformed message for symbol=%s: %s (payload=%r)",
                    symbol,
                    exc,
                    payload_bytes[:200],
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
        version = data.get("v", 1)
        if version != 1:
            raise ValueError(f"Unsupported payload version: {version}")

        kind = data.get("kind", "tick")
        if kind == "bar_1m":
            return self._parse_bar(symbol, data)
        if kind == "tick":
            return self._parse_tick(symbol, data)
        raise ValueError(f"Unknown event kind: {kind!r}")

    def _parse_tick(self, symbol: str, data: dict[str, Any]) -> Tick:
        ts_epoch = float(data["ts"])
        timestamp = datetime.fromtimestamp(ts_epoch, tz=UTC)

        # Cross-check: the DLL also converts the EL-supplied ET string to
        # UTC via std::chrono::zoned_time and ships it as ts_utc. A large
        # delta from the receive-side ts typically means the TS host clock
        # is skewed; log once per occurrence but never raise.
        ts_utc = _optional_float(data.get("ts_utc"))
        if ts_utc is not None and ts_utc > 0.0 and abs(ts_utc - ts_epoch) > 5.0:
            log.debug(
                "ts_utc drifts from recv ts by %.2fs (symbol=%s ts=%.3f ts_utc=%.3f)",
                ts_utc - ts_epoch,
                symbol,
                ts_epoch,
                ts_utc,
            )

        is_index = symbol in self._index_symbols
        return Tick(
            symbol=symbol,
            timestamp=timestamp,
            price=float(data["px"]),
            volume=int(data.get("vol", 0)),
            bid=None if is_index else _optional_float(data.get("bid")),
            ask=None if is_index else _optional_float(data.get("ask")),
            tick_count=int(data.get("tc", 0)),
            source=self.source_id,
        )

    def _parse_bar(self, symbol: str, data: dict[str, Any]) -> Bar:
        # Priority for bucket_start (UTC):
        #   1. ts_str (authoritative) — EL wall-clock string, parsed here
        #      as America/New_York. Zone-correct on any DLL host because
        #      we never rely on the host's system tz.
        #   2. ts — receive-side wall clock, last-resort only. During
        #      historical replay every bar shares one ts and would collapse
        #      onto a single bucket, so we only fall back to it when ts_str
        #      is absent AND the ts_utc cross-check is missing too.
        bucket_start: datetime | None = None

        ts_str_raw = data.get("ts_str")
        if isinstance(ts_str_raw, str) and ts_str_raw:
            bucket_start = _parse_el_str_as_et(ts_str_raw)

        # Sanity cross-check against the DLL's own ET→UTC conversion. A
        # >5s drift between the two parses is almost always a DST table
        # mismatch or a malformed ts_str; record it for later diagnosis
        # but keep the ts_str answer authoritative.
        ts_utc = _optional_float(data.get("ts_utc"))
        if bucket_start is not None and ts_utc is not None and ts_utc > 0.0:
            ts_utc_floor = _floor_to_minute_utc(ts_utc)
            if ts_utc_floor != bucket_start:
                log.debug(
                    "bar ts_str vs ts_utc mismatch (symbol=%s ts_str=%r → %s, ts_utc=%.3f → %s)",
                    symbol,
                    ts_str_raw,
                    bucket_start.isoformat(),
                    ts_utc,
                    ts_utc_floor.isoformat(),
                )

        if bucket_start is None:
            bucket_start = _floor_to_minute_utc(float(data["ts"]))

        return Bar(
            symbol=symbol,
            bucket_start=bucket_start,
            open=float(data["o"]),
            high=float(data["h"]),
            low=float(data["l"]),
            close=float(data["c"]),
            volume=int(data.get("vol", 0)),
            tick_count=int(data.get("tc", 0)),
            source=self.source_id,
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


def _floor_to_minute_utc(epoch_seconds: float) -> datetime:
    ts = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return ts.replace(second=0, microsecond=0) - timedelta(0)


def _parse_el_str_as_et(s: str) -> datetime | None:
    """Parse EL TsStr ``yyyy-MM/dd-HH:mm:ss`` (24-hour) as ET, return
    UTC-aware datetime floored to the minute. Returns None on any parse
    failure. DST is resolved by ZoneInfo from the parsed local fields.

    24-hour format is deliberate: the prior ``hh:mm:ss tt`` format broke
    on zh-TW Windows hosts where ``FormatTime("tt")`` emits localized
    AM/PM ("上午"/"下午") that neither C's sscanf nor Python's %p can
    match — every bar would then fall through to the receive-time ``ts``
    fallback and collapse onto today's date partition.
    """
    try:
        local = datetime.strptime(s, "%Y-%m/%d-%H:%M:%S")
    except (TypeError, ValueError):
        return None
    aware_et = local.replace(tzinfo=_ET_TZ)
    utc_dt = aware_et.astimezone(UTC)
    return utc_dt.replace(second=0, microsecond=0)
