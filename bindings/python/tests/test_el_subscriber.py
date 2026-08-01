from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
import zmq
import zmq.asyncio

from tradestation_data.domain.bar import Bar
from tradestation_data.wire.el_subscriber import TradeStationELProvider

TS = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()


def _tick_frame(**over: object) -> dict[str, object]:
    """A complete proto-1 tick payload; override any field per test."""
    base: dict[str, object] = {
        "proto": 1,
        "kind": "tick",
        "seq": 1,
        "sid": 7001,
        "ts": TS,
        "ts_str": "2026-04/18-09:30:45",
        "px": 450.23,
        "el_volume": 300,
        "el_ticks": 812,
        "el_upticks": 300,
        "el_downticks": 512,
        "el_open_interest": 0,
        "bid": 450.22,
        "ask": 450.24,
    }
    base.update(over)
    return base


def _bar_frame(tf: str = "1m", **over: object) -> dict[str, object]:
    """A complete proto-1 bar payload. `ts_str` is right-labelled — EL's
    `Time` is the bar's close and the indicator forwards it verbatim."""
    base: dict[str, object] = {
        "proto": 1,
        "kind": "bar",
        "tf": tf,
        "seq": 1,
        "sid": 7001,
        "ts": datetime(2026, 4, 20, 13, 30, tzinfo=UTC).timestamp(),
        "ts_str": "2026-04/20-09:30:00",
        "o": 450.0,
        "h": 451.0,
        "l": 449.0,
        "c": 450.5,
        "el_volume": 6100,
        "el_ticks": 12000,
        "el_upticks": 6100,
        "el_downticks": 5900,
        "el_open_interest": 0,
    }
    base.update(over)
    return base


async def _publish(pub: zmq.asyncio.Socket, topic: str, payload: dict[str, object]) -> None:
    await pub.send_multipart([topic.encode(), json.dumps(payload).encode()])


async def _next_tick(provider: TradeStationELProvider, timeout: float = 1.0):
    gen = provider.ticks()
    return await asyncio.wait_for(anext(gen), timeout=timeout), gen


async def _connected(zmq_inproc_bus, symbols: list[str]):
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(symbols)
    # inproc has no handshake delay, but yield once so SUB is ready
    await asyncio.sleep(0)
    return provider, pub


# ---- tick parsing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_parses_spy_tick(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _tick_frame())

    tick, gen = await _next_tick(provider)
    assert tick.symbol == "SPY"
    assert tick.price == pytest.approx(450.23)
    assert tick.bid == pytest.approx(450.22)
    assert tick.ask == pytest.approx(450.24)
    assert tick.timestamp.tzinfo is UTC
    assert tick.timestamp.year == 2026

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_tick_carries_all_five_el_quantities_verbatim(zmq_inproc_bus) -> None:
    """Each reserved word gets its own column and none is reconciled.

    The distinct values matter: reading `el_ticks` as a trade count or
    `el_volume` as total share volume is exactly the misreading this
    protocol removed, and equal placeholders would not catch a swap.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _tick_frame())

    tick, gen = await _next_tick(provider)
    assert tick.el_volume == 300
    assert tick.el_ticks == 812
    assert tick.el_upticks == 300
    assert tick.el_downticks == 512
    assert tick.el_open_interest == 0

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_index_symbol_has_no_bid_ask(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["VXX"])
    await _publish(
        pub,
        "VXX",
        _tick_frame(
            px=18.55,
            el_volume=0,
            el_ticks=0,
            el_upticks=0,
            el_downticks=0,
            bid=18.5,
            ask=18.6,
        ),
    )

    tick, gen = await _next_tick(provider)
    assert tick.symbol == "VXX"
    # Live numbers that still mean nothing — §3.2, and the DLL cannot know it.
    assert tick.bid is None
    assert tick.ask is None
    assert tick.el_volume == 0

    await gen.aclose()
    await provider.close()


# ---- the version gate ------------------------------------------------------


def test_payload_without_proto_is_refused_with_an_actionable_message() -> None:
    """The only frame a superseded publisher can send is one with no `proto`.

    The message has to name the fix, because the operator sees this after
    upgrading the binding alone: the DLL lives in their TradeStation install
    and does not move when a package does.
    """
    provider = TradeStationELProvider(endpoint="inproc://no-proto")
    legacy = {"v": 4, "kind": "tick", "ts": TS, "px": 1.0, "vol": 100, "tc": 5}
    with pytest.raises(ValueError, match="proto=None") as exc:
        provider._parse_payload("SPY", json.dumps(legacy).encode())
    assert "TS2Python.dll" in str(exc.value)
    assert ".ELD" in str(exc.value)


def test_payload_declaring_another_proto_is_refused() -> None:
    provider = TradeStationELProvider(endpoint="inproc://future-proto")
    with pytest.raises(ValueError, match="proto=2"):
        provider._parse_payload("SPY", json.dumps(_tick_frame(proto=2)).encode())


@pytest.mark.asyncio
async def test_refused_frame_does_not_end_the_stream(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", {"v": 4, "kind": "tick", "ts": TS, "px": 1.0})
    await _publish(pub, "SPY", _tick_frame(seq=2, px=452.0))

    tick, gen = await _next_tick(provider)
    assert tick.price == pytest.approx(452.0)

    await gen.aclose()
    await provider.close()


# ---- quantities are required, never defaulted ------------------------------


@pytest.mark.parametrize(
    "missing",
    ["el_volume", "el_ticks", "el_upticks", "el_downticks", "el_open_interest"],
)
def test_missing_quantity_raises_rather_than_writing_zero(missing: str) -> None:
    """A defaulted quantity is a plausible number nobody can audit later.

    `.get(name, 0)` would put a 0 on disk that is indistinguishable from a
    symbol that genuinely did not trade, so every one of the five is read as
    required and the frame is dropped instead.
    """
    provider = TradeStationELProvider(endpoint="inproc://missing-qty")
    frame = _tick_frame()
    del frame[missing]
    with pytest.raises(ValueError, match=missing):
        provider._parse_payload("SPY", json.dumps(frame).encode())


def test_missing_quantity_on_a_bar_raises_too() -> None:
    provider = TradeStationELProvider(endpoint="inproc://missing-qty-bar")
    frame = _bar_frame()
    del frame["el_open_interest"]
    with pytest.raises(ValueError, match="el_open_interest"):
        provider._parse_payload("SPY", json.dumps(frame).encode())


# ---- bar parsing: bucket_start authority -----------------------------------


@pytest.mark.asyncio
async def test_bar_carries_ohlc_and_quantities(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _bar_frame())

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.open == pytest.approx(450.0)
    assert event.high == pytest.approx(451.0)
    assert event.low == pytest.approx(449.0)
    assert event.close == pytest.approx(450.5)
    assert event.el_volume == 6100
    assert event.el_ticks == 12000
    assert event.el_upticks == 6100
    assert event.el_downticks == 5900
    assert event.el_open_interest == 0

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_prefers_ts_str_over_ts(zmq_inproc_bus) -> None:
    """`ts_str` is authoritative; `ts` is the DLL's receive clock.

    They are made to disagree by hours here so a parser that quietly used
    `ts` cannot pass. During historical replay every bar shares one `ts` and
    would collapse onto a single bucket.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    # 09:31 ET = 13:31 UTC on 2026-04-17 (EDT). EL stamps the close, so that
    # frame is the left-labelled 09:30 bar → 13:30 UTC.
    await _publish(
        pub,
        "SPY",
        _bar_frame(
            ts=datetime(2026, 4, 17, 5, 31, 0, tzinfo=UTC).timestamp(),
            ts_str="2026-04/17-09:31:00",
        ),
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 4, 17, 13, 30, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_ts_str_handles_dst_boundary(zmq_inproc_bus) -> None:
    """Pick a date in standard time (EST, UTC-5) to verify DST-aware
    conversion. 2026-01-15 09:31 EST = 14:31 UTC, left-labelled 14:30."""
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _bar_frame(ts=0.0, ts_str="2026-01/15-09:31:00"))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 1, 15, 14, 30, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_with_localized_am_pm_ts_str_is_refused(zmq_inproc_bus, caplog) -> None:
    """A `ts_str` the parser cannot read gets the frame dropped, not guessed.

    The wire moved from 12-hour + ``tt`` to 24-hour because
    ``FormatTime("tt")`` on a zh-TW TradeStation host emits "上午"/"下午"
    (UTF-8), which ``%I:%M:%S %p`` could not match. Every bar then fell
    through to the receive-time ``ts`` — and on a chart replay that is one
    instant for the whole session, so 390 bars collapsed onto one bucket and
    the runtime's dedupe kept exactly one. A full day, gone, every number in
    the survivor plausible.

    Falling back on a string we could not read is the bug. The publisher no
    longer parses ts_str either (`ts_utc` is gone), so this is the only layer
    that can notice. It refuses, and says why.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(
        pub,
        "SPY",
        _bar_frame(
            ts=datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC).timestamp(),
            ts_str="2026-04/18-01:31:00 下午",
        ),
    )
    # A good frame behind it proves the refusal drops one frame, not the stream.
    # ts_str is ET: 09:31 EDT is the 09:30 bar's close, so it left-labels to
    # 09:30 EDT == 13:30 UTC.
    await _publish(pub, "SPY", _bar_frame(seq=2, ts_str="2026-04/18-09:31:00"))

    gen = provider.events()
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        event = await asyncio.wait_for(anext(gen), timeout=1.0)

    # The localized frame never surfaced; the next one did.
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)
    assert any("unparseable 'ts_str'" in r.message for r in caplog.records), (
        "the refusal must name the field, or an operator cannot act on it"
    )

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_absent_ts_str_falls_back_but_says_so(zmq_inproc_bus, caplog) -> None:
    """No `ts_str` at all is a degradation, not a lie — allowed, but logged.

    semantics.md §1.1 permits the receive-clock fallback when the publisher
    sent no string. It requires the binding to record it, because an operator
    seeing this on every frame is watching a publisher that will collapse any
    replay onto a single bucket.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    frame = _bar_frame(ts=datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC).timestamp())
    del frame["ts_str"]
    await _publish(pub, "SPY", frame)

    gen = provider.events()
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        event = await asyncio.wait_for(anext(gen), timeout=1.0)

    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)
    assert any("bar_ts_str_absent_using_recv_clock" in r.message for r in caplog.records)

    await gen.aclose()
    await provider.close()


# ---- timeframe on the bar --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tf", ["1m", "5m", "15m", "30m", "1h", "1d"])
async def test_bar_carries_its_timeframe(zmq_inproc_bus, tf: str) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _bar_frame(tf))

    gen = provider.events()
    bar = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert bar.timeframe == tf

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_with_unknown_timeframe_is_refused(zmq_inproc_bus) -> None:
    """A tf we cannot place must not be filed under a default.

    Defaulting would put one interval's bars into another's partition, which
    is precisely the corruption this field exists to prevent.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _bar_frame("4h"))
    await _publish(pub, "SPY", _bar_frame("5m", seq=2))

    gen = provider.events()
    bar = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert bar.timeframe == "5m"  # the 4h frame was dropped, stream continued

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_without_tf_is_refused_not_defaulted(zmq_inproc_bus) -> None:
    """Filing an unknown interval as 1m puts it in the 1-minute partition,
    where nothing downstream can tell it apart from real minute data."""
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    headless = _bar_frame("5m")
    del headless["tf"]
    await _publish(pub, "SPY", headless)
    await _publish(pub, "SPY", _bar_frame("5m", seq=2))

    gen = provider.events()
    bar = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert bar.timeframe == "5m"  # the tf-less frame was dropped, not filed as 1m

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_daily_bar_is_aligned_to_the_session_anchor(zmq_inproc_bus) -> None:
    """§2.2 — EL stamps a daily bar with its chart's own time, not 04:00 ET.

    Left as sent, one trading day could land as two rows in
    bars/timeframe=1d/ with different bucket_starts, and every join
    downstream would double-count it.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _bar_frame("1d"))  # ts_str is 09:30 ET

    gen = provider.events()
    bar = await asyncio.wait_for(anext(gen), timeout=1.0)
    # 04:00 EDT on the same session date. Session-anchored frames skip the
    # left-label step, which would otherwise move the bar a day earlier.
    assert bar.bucket_start == datetime(2026, 4, 20, 8, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


# ---- routing: topics, kinds, malformed frames ------------------------------


@pytest.mark.asyncio
async def test_topic_filter_only_subscribed_symbols(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    # QQQ should be filtered out by the SUB socket
    await _publish(pub, "QQQ", _tick_frame(px=400.0))
    await _publish(pub, "SPY", _tick_frame(px=450.0))

    tick, gen = await _next_tick(provider)
    assert tick.symbol == "SPY"
    assert tick.price == pytest.approx(450.0)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_prefix_matched_topic_is_dropped(zmq_inproc_bus) -> None:
    """semantics.md §5 — SUBSCRIBE is a prefix match; SPY also receives SPYG.

    Without an exact-equality pass the runtime would build a MarketSnapshot
    and start writing symbol=SPYG/ partitions for a symbol never subscribed.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPYG", _tick_frame(px=1.0))
    await _publish(pub, "SPY", _tick_frame(seq=2, px=450.0))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert event.symbol == "SPY", "SPYG frame must not surface on a SPY subscription"

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_malformed_payload_is_skipped(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await pub.send_multipart([b"SPY", b"{not valid json"])
    await _publish(pub, "SPY", _tick_frame(px=450.0))

    tick, gen = await _next_tick(provider, timeout=2.0)
    assert tick.symbol == "SPY"
    assert tick.price == pytest.approx(450.0)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_unknown_kind_is_dropped(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _tick_frame(kind="some_future_kind"))
    await _publish(pub, "SPY", _tick_frame(seq=2, px=450.0))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert getattr(event, "price", None) == pytest.approx(450.0)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_ticks_filters_out_bar_events(zmq_inproc_bus) -> None:
    """ticks() is a tick-only convenience; bar events must be skipped."""
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _bar_frame())
    await _publish(pub, "SPY", _tick_frame(seq=2, px=9.0))

    gen = provider.ticks()
    tick = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert tick.price == pytest.approx(9.0)

    await gen.aclose()
    await provider.close()


# ---- connect / subscribe / close lifecycle ---------------------------------


@pytest.mark.asyncio
async def test_ticks_requires_connect(zmq_inproc_bus) -> None:
    _, _, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint)
    with pytest.raises(RuntimeError):
        _ = provider.ticks().__aiter__()
        await anext(provider.ticks())


@pytest.mark.asyncio
async def test_subscribe_is_idempotent(zmq_inproc_bus) -> None:
    ctx, _pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY", "SPY", "QQQ"])
    await provider.subscribe(["SPY"])
    assert provider._subscribed == {"SPY", "QQQ"}
    await provider.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(zmq_inproc_bus) -> None:
    ctx, _, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.close()
    await provider.close()  # must not raise


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_creates_owned_context() -> None:
    """First connect() creates a context (it owns); second is a no-op."""
    provider = TradeStationELProvider(endpoint="inproc://idempotent-connect")
    await provider.connect()
    first_socket = provider._socket
    assert first_socket is not None
    assert provider._ctx is not None
    assert provider._ctx_owned is True
    await provider.connect()
    assert provider._socket is first_socket
    await provider.close()


@pytest.mark.asyncio
async def test_subscribe_without_connect_raises() -> None:
    provider = TradeStationELProvider(endpoint="inproc://unused")
    with pytest.raises(RuntimeError, match="connect"):
        await provider.subscribe(["SPY"])


@pytest.mark.asyncio
async def test_close_owned_context_terminates_it() -> None:
    """close() on a provider that owns its context must terminate it."""
    provider = TradeStationELProvider(endpoint="inproc://owned-ctx-close")
    await provider.connect()
    ctx = provider._ctx
    assert provider._ctx_owned is True
    await provider.close()
    assert provider._socket is None
    assert provider._ctx is None
    del ctx


@pytest.mark.asyncio
async def test_close_without_connect_is_noop() -> None:
    """close() before connect() must leave internal state untouched."""
    provider = TradeStationELProvider(endpoint="inproc://no-connect-close")
    assert provider._socket is None
    await provider.close()
    assert provider._closed is True


# ---- events() error handling -----------------------------------------------


@pytest.mark.asyncio
async def test_events_returns_when_closed_flag_set() -> None:
    class _StubSocket:
        async def recv_multipart(self):  # pragma: no cover - not reached
            raise AssertionError("recv must not be called when _closed is set")

    provider = TradeStationELProvider(endpoint="inproc://stub-closed")
    provider._socket = _StubSocket()  # type: ignore[assignment]
    provider._closed = True
    gen = provider.events()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    provider._socket = None  # avoid __del__ warning


@pytest.mark.asyncio
async def test_events_returns_on_context_terminated() -> None:
    class _StubSocket:
        async def recv_multipart(self):
            raise zmq.error.ContextTerminated()

    provider = TradeStationELProvider(endpoint="inproc://ctx-term")
    provider._socket = _StubSocket()  # type: ignore[assignment]
    gen = provider.events()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    provider._socket = None


@pytest.mark.asyncio
async def test_events_returns_on_zmq_error_after_close() -> None:
    class _StubSocket:
        raised = False

        async def recv_multipart(self):
            if not self.raised:
                self.raised = True
                raise zmq.error.ZMQError(errno=0)
            return (b"SPY", json.dumps(_tick_frame()).encode())

    provider = TradeStationELProvider(endpoint="inproc://zmq-err")
    provider._socket = _StubSocket()  # type: ignore[assignment]
    provider._closed = True  # so the error-branch takes the return path
    gen = provider.events()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    provider._socket = None


@pytest.mark.asyncio
async def test_events_logs_and_continues_on_transient_zmq_error(caplog) -> None:
    """A ZMQError while still open logs and retries rather than ending.

    The stub must answer with a frame this protocol accepts: a refused one
    is dropped and recv is called again, which against a stub that always
    replies the same way is an unbounded loop rather than a test failure.
    """

    class _StubSocket:
        calls = 0

        async def recv_multipart(self):
            self.calls += 1
            if self.calls == 1:
                raise zmq.error.ZMQError(errno=0)
            return (b"SPY", json.dumps(_tick_frame()).encode())

    provider = TradeStationELProvider(endpoint="inproc://zmq-warn")
    provider._socket = _StubSocket()  # type: ignore[assignment]
    # events() drops topics that are not an exact subscription match
    # (semantics.md §5), and the stub bypasses subscribe().
    provider._subscribed = {"SPY"}
    gen = provider.events()
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        event = await anext(gen)
    assert event.symbol == "SPY"
    assert any("zmq recv error" in r.message for r in caplog.records)
    # Terminate the generator cleanly to release the stub socket.
    provider._closed = True
    await gen.aclose()
    provider._socket = None


# ---- sequence tracking / gap detection -------------------------------------


def _tracker():
    from tradestation_data.wire.el_subscriber import _SequenceTracker

    return _SequenceTracker()


def test_sequence_first_message_sets_baseline_without_reporting_loss() -> None:
    """A late subscriber joining mid-stream did not lose what it never asked for."""
    t = _tracker()
    t.observe("SPY", 21, 7001)
    assert t.messages_lost == 0


def test_sequence_contiguous_reports_no_loss() -> None:
    t = _tracker()
    t.observe("SPY", 1, 7001)
    t.observe("SPY", 2, 7001)
    t.observe("SPY", 3, 7001)
    assert t.messages_lost == 0


def test_sequence_gap_is_counted() -> None:
    t = _tracker()
    t.observe("SPY", 1, 7001)
    t.observe("SPY", 5, 7001)  # 2, 3, 4 never arrived
    assert t.messages_lost == 3
    # Tracking resumes from the message that did arrive.
    t.observe("SPY", 6, 7001)
    assert t.messages_lost == 3


def test_sequence_is_tracked_per_symbol() -> None:
    """A gap on one topic must not be inferred from another topic's traffic."""
    t = _tracker()
    t.observe("SPY", 1, 7001)
    t.observe("QQQ", 1, 7001)
    t.observe("SPY", 2, 7001)
    t.observe("QQQ", 2, 7001)
    assert t.messages_lost == 0


def test_publisher_restart_resets_instead_of_reporting_huge_loss() -> None:
    """A new sid means counters restarted at the source, not that we lost data."""
    t = _tracker()
    t.observe("SPY", 900, 7001)
    t.observe("SPY", 901, 7001)
    t.observe("SPY", 1, 7002)
    assert t.messages_lost == 0
    t.observe("SPY", 2, 7002)
    assert t.messages_lost == 0


def test_sequence_regression_does_not_rewind_expectation() -> None:
    """Duplicates must not make the next real message look like a gap."""
    t = _tracker()
    t.observe("SPY", 1, 7001)
    t.observe("SPY", 2, 7001)
    t.observe("SPY", 2, 7001)  # duplicate
    assert t.messages_lost == 0
    t.observe("SPY", 3, 7001)  # still expected 3
    assert t.messages_lost == 0


@pytest.mark.asyncio
async def test_provider_exposes_messages_lost(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    assert provider.messages_lost is None  # nothing seen yet — cannot tell

    await _publish(pub, "SPY", _tick_frame(seq=1, px=450.0))
    tick, gen = await _next_tick(provider)
    assert tick.price == pytest.approx(450.0)
    assert provider.gap_detection_available is True
    assert provider.messages_lost == 0

    await _publish(pub, "SPY", _tick_frame(seq=4, px=451.0))
    tick2 = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert tick2.price == pytest.approx(451.0)
    assert provider.messages_lost == 2  # seq 2 and 3 dropped

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_refused_frame_still_counts_against_the_sequence(zmq_inproc_bus) -> None:
    """A frame we drop still occupied a slot in the publisher's counter.

    Skipping observe() on the refusal path would park `_expected` at the last
    accepted seq, so the next accepted frame would report a gap that never
    happened — an operator whose link lost nothing watching steady loss.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _tick_frame(seq=1, px=450.0))
    tick, gen = await _next_tick(provider)
    assert tick.price == pytest.approx(450.0)

    await _publish(pub, "SPY", _tick_frame(seq=2, proto=99))  # refused
    await _publish(pub, "SPY", _tick_frame(seq=3, px=452.0))
    tick3 = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert tick3.price == pytest.approx(452.0)
    assert provider.messages_lost == 0

    await gen.aclose()
    await provider.close()


# ---- quote availability (semantics.md §3) ---------------------------------


def _quote(v):
    from tradestation_data.wire.el_subscriber import _quote_or_none

    return _quote_or_none(v)


def test_null_quote_is_absent():
    """The DLL normalises a non-positive InsideBid/InsideAsk to JSON null."""
    assert _quote(None) is None


def test_non_positive_quote_is_absent_not_a_price():
    """Belt and braces on the binding side.

    Reading a 0 as a real $0.00 quote would put fabricated prices into the
    store for every non-live bar.
    """
    assert _quote(0.0) is None
    assert _quote(-1.0) is None


def test_real_quotes_pass_through():
    assert _quote(449.99) == pytest.approx(449.99)
    assert _quote(0.01) == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_history_replay_tick_has_no_quotes(zmq_inproc_bus) -> None:
    """End to end: a null quote must not reach the caller as a number."""
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _tick_frame(bid=None, ask=None))

    tick, gen = await _next_tick(provider)
    assert tick.price == pytest.approx(450.23)
    assert tick.bid is None
    assert tick.ask is None

    await gen.aclose()
    await provider.close()
