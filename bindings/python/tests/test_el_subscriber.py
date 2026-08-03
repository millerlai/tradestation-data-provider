from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import zmq
import zmq.asyncio

from tradestation_data.domain.bar import Bar
from tradestation_data.wire.el_subscriber import TradeStationELProvider

TS = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()


def _frame(bar_type: int = 1, bar_interval: int = 1, **over: object) -> dict[str, object]:
    """A complete proto-2 frame. One shape for every chart.

    `ts_str` is EL's `Time`, which is the point's CLOSE; the indicator
    forwards it verbatim and the binding lands it verbatim.
    """
    base: dict[str, object] = {
        "proto": 2,
        "seq": 1,
        "sid": 7001,
        "ts": datetime(2026, 4, 20, 13, 30, tzinfo=UTC).timestamp(),
        "ts_str": "2026-04/20-09:30:00",
        "bar_type": bar_type,
        "bar_interval": bar_interval,
        "category": 2,
        "o": 450.0,
        "h": 451.0,
        "l": 449.0,
        "c": 450.5,
        "el_volume": 6100,
        "el_ticks": 12000,
        "el_upticks": 6100,
        "el_downticks": 5900,
        "el_open_interest": 0,
        "bid": 450.22,
        "ask": 450.24,
    }
    base.update(over)
    return base


async def _publish(pub: zmq.asyncio.Socket, topic: str, payload: dict[str, object]) -> None:
    await pub.send_multipart([topic.encode(), json.dumps(payload).encode()])


async def _connected(zmq_inproc_bus, symbols: list[str]):
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(symbols)
    # inproc has no handshake delay, but yield once so SUB is ready
    await asyncio.sleep(0)
    return provider, pub


# ---- tick parsing ----------------------------------------------------------


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
    with pytest.raises(ValueError, match="proto=1"):
        provider._parse_payload("SPY", json.dumps(_frame(proto=1)).encode())


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
    frame = _frame()
    del frame[missing]
    with pytest.raises(ValueError, match=missing):
        provider._parse_payload("SPY", json.dumps(frame).encode())


def test_missing_quantity_on_a_bar_raises_too() -> None:
    provider = TradeStationELProvider(endpoint="inproc://missing-qty-bar")
    frame = _frame()
    del frame["el_open_interest"]
    with pytest.raises(ValueError, match="el_open_interest"):
        provider._parse_payload("SPY", json.dumps(frame).encode())


# ---- bar parsing: bar_time authority -----------------------------------


@pytest.mark.asyncio
async def test_bar_carries_ohlc_and_quantities(zmq_inproc_bus) -> None:
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _frame())

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
    # 09:31 ET = 13:31 UTC on 2026-04-17 (EDT). EL stamps the close and the
    # binding lands it verbatim.
    await _publish(
        pub,
        "SPY",
        _frame(
            ts=datetime(2026, 4, 17, 5, 31, 0, tzinfo=UTC).timestamp(),
            ts_str="2026-04/17-09:31:00",
        ),
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bar_time == datetime(2026, 4, 17, 13, 31, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_ts_str_handles_dst_boundary(zmq_inproc_bus) -> None:
    """Pick a date in standard time (EST, UTC-5) to verify DST-aware
    conversion. 2026-01-15 09:31 EST = 14:31 UTC."""
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _frame(ts=0.0, ts_str="2026-01/15-09:31:00"))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bar_time == datetime(2026, 1, 15, 14, 31, 0, tzinfo=UTC)

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
        _frame(
            ts=datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC).timestamp(),
            ts_str="2026-04/18-01:31:00 下午",
        ),
    )
    # A good frame behind it proves the refusal drops one frame, not the stream.
    # ts_str is ET: 09:31 EDT is the 09:30 bar's close, so it left-labels to
    # 09:30 EDT == 13:30 UTC.
    await _publish(pub, "SPY", _frame(seq=2, ts_str="2026-04/18-09:31:00"))

    gen = provider.events()
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        event = await asyncio.wait_for(anext(gen), timeout=1.0)

    # The localized frame never surfaced; the next one did.
    assert isinstance(event, Bar)
    assert event.bar_time == datetime(2026, 4, 18, 13, 31, 0, tzinfo=UTC)
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

    The fallback keeps its seconds, like the `ts_str` path does. It used to
    floor them away, matching the old §2.1 — which after that rule reversed
    would have left the two paths disagreeing, and in the worse direction:
    flooring is what collapses a sub-minute chart onto one `bar_time` per
    minute for the buffer to then discard.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    frame = _frame(ts=datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC).timestamp())
    del frame["ts_str"]
    await _publish(pub, "SPY", frame)

    gen = provider.events()
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        event = await asyncio.wait_for(anext(gen), timeout=1.0)

    assert isinstance(event, Bar)
    assert event.bar_time == datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC)
    assert any("ts_str_absent_using_recv_clock" in r.message for r in caplog.records)

    await gen.aclose()
    await provider.close()


# ---- timeframe on the bar --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bar_type", "bar_interval"),
    [(0, 1), (1, 1), (1, 5), (1, 15), (1, 30), (1, 60), (1, 2), (2, 1), (3, 1)],
)
async def test_point_carries_the_charts_own_words(
    zmq_inproc_bus, bar_type: int, bar_interval: int
) -> None:
    """BarType and BarInterval land verbatim, including pairs with no name.

    (1, 2) is a 2-minute chart and (3, 1) a weekly one: the DLL used to map
    the pair to a timeframe string and return -5 for anything it could not
    name, so those two published nothing at all.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _frame(bar_type, bar_interval))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert event.bar_type == bar_type
    assert event.bar_interval == bar_interval

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_prefix_matched_topic_is_dropped(zmq_inproc_bus) -> None:
    """semantics.md §5 — SUBSCRIBE is a prefix match; SPY also receives SPYG.

    Without an exact-equality pass the runtime would build a MarketSnapshot
    and start writing symbol=SPYG/ partitions for a symbol never subscribed.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPYG", _frame(c=1.0))
    await _publish(pub, "SPY", _frame(seq=2, c=450.0))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert event.symbol == "SPY", "SPYG frame must not surface on a SPY subscription"

    await gen.aclose()
    await provider.close()


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
            return (b"SPY", json.dumps(_frame()).encode())

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
            return (b"SPY", json.dumps(_frame()).encode())

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
async def test_null_quantity_drops_the_frame_instead_of_killing_the_stream(
    zmq_inproc_bus, caplog
) -> None:
    """A JSON null in a quantity raises TypeError, which was not caught.

    `_quantities` does `int(data[name])`. Before this, the TypeError left
    `events()`, killed the ingest task, and `IngestionRuntime.run()` never
    noticed because it sits on `_stop.wait()` and only awaits the tasks after
    stop is set. The process stayed up, the heartbeat kept logging, and
    nothing was ever ingested again. One frame must not be able to do that.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _frame(el_volume=None))
    await _publish(pub, "SPY", _frame(seq=2, c=452.0))

    gen = provider.events()
    with caplog.at_level("ERROR", logger="tradestation_data.wire.el_subscriber"):
        event = await asyncio.wait_for(anext(gen), timeout=1.0)

    assert event.close == pytest.approx(452.0), "the stream did not survive the bad frame"
    assert any("Dropping unparseable message" in r.message for r in caplog.records)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_non_object_payload_drops_the_frame_instead_of_killing_the_stream(
    zmq_inproc_bus, caplog
) -> None:
    """A payload that decodes to a list makes `data.get` raise AttributeError."""
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await pub.send_multipart([b"SPY", b"[]"])
    await _publish(pub, "SPY", _frame(seq=2, c=453.0))

    gen = provider.events()
    with caplog.at_level("ERROR", logger="tradestation_data.wire.el_subscriber"):
        event = await asyncio.wait_for(anext(gen), timeout=1.0)

    assert event.close == pytest.approx(453.0)
    assert any("Dropping unparseable message" in r.message for r in caplog.records)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_two_published_hours_do_not_collide(zmq_inproc_bus) -> None:
    """The 60-minute case that used to lose a bar every single day.

    A 1h chart restarts its grid at the RTH open and close, so a session
    yields fifteen bars, two of them short. Measured on live SPY 2026-07-31
    with a 06:00 session: EL published closes 09:00 (the full 08:00-09:00
    hour) and 09:30 (the 09:00-09:30 stub). The binding subtracted a minute
    and snapped both onto the 09:30-anchored hour grid, and both landed on
    08:30. `_handle_provider_bar` read the second as an intra-bar refresh of
    the first, so the whole 08:00-09:00 hour was replaced by the half-hour's
    numbers -- fifteen published, fourteen stored, every day, no error
    anywhere.

    No grid could have fixed it: the segment lengths follow the chart's own
    session template, which the wire does not carry. Landing the publisher's
    timestamp verbatim does.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _frame(1, 60, seq=1, ts_str="2026-07/31-09:00:00"))
    await _publish(pub, "SPY", _frame(1, 60, seq=2, ts_str="2026-07/31-09:30:00"))

    gen = provider.events()
    first = await asyncio.wait_for(anext(gen), timeout=1.0)
    second = await asyncio.wait_for(anext(gen), timeout=1.0)

    et = ZoneInfo("America/New_York")
    assert first.bar_time.astimezone(et).strftime("%H:%M") == "09:00"
    assert second.bar_time.astimezone(et).strftime("%H:%M") == "09:30"
    assert first.bar_time != second.bar_time

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bar_type", "bar_interval", "close_et"),
    [
        (1, 1, "09:31:00"),
        (1, 1, "16:00:00"),
        (1, 5, "09:35:00"),
        (1, 5, "16:00:00"),
        (1, 60, "10:30:00"),
        (1, 30, "16:00:00"),
        (2, 1, "09:30:00"),
    ],
)
async def test_every_chart_lands_its_timestamp_verbatim(
    zmq_inproc_bus, bar_type: int, bar_interval: int, close_et: str
) -> None:
    """One rule for every chart, daily included: whatever EL stamped.

    There is no interval-dependent shift and no grid, so there is nothing
    left that can differ between charts. Daily used to be a special case
    twice over — exempt from the shift, then replaced outright by its
    session's 04:00 ET anchor — and now it is not a case at all.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    await _publish(pub, "SPY", _frame(bar_type, bar_interval, ts_str=f"2026-04/20-{close_et}"))

    gen = provider.events()
    bar = await asyncio.wait_for(anext(gen), timeout=1.0)
    et = ZoneInfo("America/New_York")
    assert bar.bar_time.astimezone(et).strftime("%H:%M") == close_et[:5]

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_refused_frames_are_counted_separately_from_lost_ones(
    zmq_inproc_bus,
) -> None:
    """A link refusing everything reports zero lost, and that is correct.

    The documented upgrade order is binding first, then DLL, so there is a
    window where the old publisher is still running. Its frames carry
    seq/sid, so sequence tracking starts and reports no loss, while the proto
    gate throws every frame away and nothing is delivered. `messages_lost`
    answers "sent but never arrived" and these arrived, so 0 is the honest
    answer -- it is just not the whole answer. `frames_refused` is the rest
    of it, and the two must be read together.
    """
    provider, pub = await _connected(zmq_inproc_bus, ["SPY"])
    assert provider.frames_refused == 0

    for seq in (1, 2, 3):
        await _publish(pub, "SPY", _frame(seq=seq, proto=99))
    await _publish(pub, "SPY", _frame(seq=4, c=450.0))

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)

    assert event.close == pytest.approx(450.0)
    assert provider.frames_refused == 3, "every refused frame must be counted"
    assert provider.messages_lost == 0, (
        "refused frames arrived, so nothing was lost -- that is why the "
        "refusal count has to exist alongside it"
    )

    await gen.aclose()
    await provider.close()


def test_proto1_frame_without_seq_is_refused() -> None:
    """Both schemas mark `seq` required; nothing enforced it at runtime.

    A frame without one parsed normally, `sid` stayed None, and
    `messages_lost` returned None forever -- which an operator reads as
    "nothing to report" while PUB/SUB high-water-mark drops go uncounted.
    §6.6 exists to forbid exactly that conflation.
    """
    provider = TradeStationELProvider(endpoint="inproc://no-seq")
    frame = _frame()
    del frame["seq"]
    with pytest.raises(ValueError, match="no 'seq'"):
        provider._parse_payload("SPY", json.dumps(frame).encode())


def test_a_superseded_frame_still_gets_the_protocol_message_not_the_seq_one() -> None:
    """Ordering check: the message an operator can act on must win.

    A superseded publisher's frames DO carry seq, so they reach the seq check
    only if the proto gate let them past -- which it must not. Getting "no
    seq" here would point the operator at the wrong problem entirely.
    """
    provider = TradeStationELProvider(endpoint="inproc://legacy-order")
    legacy = {"v": 4, "kind": "tick", "seq": 1, "sid": 7001, "ts": TS, "px": 1.0}
    with pytest.raises(ValueError, match="proto=None"):
        provider._parse_payload("SPY", json.dumps(legacy).encode())


def test_dst_ambiguous_ts_str_is_reported_not_silently_resolved(caplog) -> None:
    """The wire cannot settle which of the two 01:30s a bar means.

    `ts_str` is a local wall-clock string with no offset and no fold bit, so
    on the fall-back date 01:30 ET names two different instants an hour apart
    and the frame does not say which. fold=0 is kept -- guessing differently
    would be no better founded -- but doing it silently produced a timestamp
    that looked entirely ordinary.
    """
    provider = TradeStationELProvider(endpoint="inproc://dst-fold")
    # 2026 DST ends Nov 1; the 01:00-02:00 ET hour repeats.
    frame = _frame(ts_str="2026-11/01-01:30:00")

    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        bar = provider._parse_payload("SPY", json.dumps(frame).encode())

    assert bar is not None
    assert any("el_timestamp_dst_ambiguous" in r.message for r in caplog.records)


def test_unambiguous_ts_str_says_nothing(caplog) -> None:
    """Every other day of the year must stay quiet."""
    provider = TradeStationELProvider(endpoint="inproc://dst-normal")
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        provider._parse_payload("SPY", json.dumps(_frame()).encode())
    assert not [r for r in caplog.records if "dst_ambiguous" in r.message]
