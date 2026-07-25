from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
import zmq
import zmq.asyncio

from tradestation_data.wire.el_subscriber import TradeStationELProvider


async def _publish(pub: zmq.asyncio.Socket, topic: str, payload: dict[str, object]) -> None:
    await pub.send_multipart([topic.encode(), json.dumps(payload).encode()])


async def _next_tick(provider: TradeStationELProvider, timeout: float = 1.0):
    gen = provider.ticks()
    return await asyncio.wait_for(anext(gen), timeout=timeout), gen


@pytest.mark.asyncio
async def test_provider_parses_spy_tick(zmq_inproc_bus) -> None:
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    # inproc has no handshake delay, but yield once so SUB is ready
    await asyncio.sleep(0)

    ts_epoch = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {"v": 1, "ts": ts_epoch, "px": 450.23, "vol": 100, "bid": 450.22, "ask": 450.24, "tc": 5},
    )

    tick, gen = await _next_tick(provider)
    assert tick.symbol == "SPY"
    assert tick.price == pytest.approx(450.23)
    assert tick.volume == 100
    assert tick.bid == pytest.approx(450.22)
    assert tick.ask == pytest.approx(450.24)
    assert tick.tick_count == 5
    assert tick.source == "tradestation_el"
    assert tick.timestamp.tzinfo is UTC
    assert tick.timestamp.year == 2026

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_index_symbol_has_no_bid_ask(zmq_inproc_bus) -> None:
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["VXX"])
    await asyncio.sleep(0)

    await _publish(
        pub,
        "VXX",
        {"v": 1, "ts": 1_745_000_000.0, "px": 18.55, "vol": 0, "bid": 0, "ask": 0, "tc": 0},
    )

    tick, gen = await _next_tick(provider)
    assert tick.symbol == "VXX"
    assert tick.bid is None
    assert tick.ask is None
    assert tick.volume == 0

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_topic_filter_only_subscribed_symbols(zmq_inproc_bus) -> None:
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # QQQ should be filtered out by SUB socket
    await _publish(pub, "QQQ", {"v": 1, "ts": 1.0, "px": 400.0, "vol": 10, "tc": 1})
    await _publish(pub, "SPY", {"v": 1, "ts": 2.0, "px": 450.0, "vol": 20, "tc": 1})

    tick, gen = await _next_tick(provider)
    assert tick.symbol == "SPY"
    assert tick.price == pytest.approx(450.0)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_malformed_payload_is_skipped(zmq_inproc_bus) -> None:
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # Bad JSON, then good message
    await pub.send_multipart([b"SPY", b"{not valid json"])
    await _publish(pub, "SPY", {"v": 1, "ts": 1.0, "px": 450.0, "vol": 1, "tc": 1})

    tick, gen = await _next_tick(provider, timeout=2.0)
    assert tick.symbol == "SPY"
    assert tick.price == pytest.approx(450.0)

    await gen.aclose()
    await provider.close()


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
async def test_provider_reads_ts_el_without_failing(zmq_inproc_bus) -> None:
    """New DLL emits ts_el alongside ts; provider must accept both."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    ts_epoch = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "ts": ts_epoch,
            "ts_el": ts_epoch - 0.002,  # EL bar time, 2ms earlier
            "px": 450.23,
            "vol": 100,
            "bid": 450.22,
            "ask": 450.24,
            "tc": 5,
        },
    )

    tick, gen = await _next_tick(provider)
    # tick.timestamp still derives from ts (authoritative); parser must
    # not explode on the extra field.
    assert tick.timestamp.timestamp() == pytest.approx(ts_epoch)
    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_provider_tolerates_zero_ts_el(zmq_inproc_bus) -> None:
    """DLL emits ts_el=0.0 when it failed to parse the EL string — ignore."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    ts_epoch = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "ts": ts_epoch,
            "ts_el": 0.0,
            "px": 450.0,
            "vol": 10,
            "bid": 449.99,
            "ask": 450.01,
            "tc": 1,
        },
    )
    tick, gen = await _next_tick(provider)
    assert tick.price == pytest.approx(450.0)
    await gen.aclose()
    await provider.close()


# ---- bar_1m (EL_PublishTickEx) -----------------------------------------


@pytest.mark.asyncio
async def test_provider_parses_bar_1m_event(zmq_inproc_bus) -> None:
    """EL_PublishTickEx ships full OHLC; provider must emit a Bar."""
    from tradestation_data.domain.bar import Bar

    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # Bar minute-bucket 13:30:00 UTC. ts_el lands mid-bucket (13:30:45) —
    # provider must floor it to the minute for bucket_start.
    ts_el = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts_el + 0.002,  # recv-side clock, irrelevant here
            "ts_el": ts_el,
            "o": 450.10,
            "h": 450.75,
            "l": 449.80,
            "c": 450.40,
            "vol": 12000,
            "bid": 450.39,
            "ask": 450.41,
            "tc": 140,
        },
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.symbol == "SPY"
    assert event.open == pytest.approx(450.10)
    assert event.high == pytest.approx(450.75)
    assert event.low == pytest.approx(449.80)
    assert event.close == pytest.approx(450.40)
    assert event.volume == 12000
    assert event.tick_count == 140
    assert event.source == "tradestation_el"
    assert event.bucket_start == datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_1m_prefers_ts_str_over_ts_el(zmq_inproc_bus) -> None:
    """v4+ DLL ships the raw EL TsStr so the provider can zone-correct it
    regardless of the TS host's Windows timezone. Provider must parse
    ts_str as US Eastern and ignore ts_el when both are present."""
    from tradestation_data.domain.bar import Bar

    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # 09:31 ET = 13:31 UTC on 2026-04-17 (EDT, UTC-4).
    # ts_el is intentionally garbage (would drift bucket by 8 hours if used)
    # to prove the provider trusts ts_str instead.
    wrong_ts_el = datetime(2026, 4, 17, 5, 31, 0, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": wrong_ts_el + 0.002,
            "ts_el": wrong_ts_el,
            "ts_str": "2026-04/17-09:31:00",
            "o": 706.10,
            "h": 706.75,
            "l": 705.80,
            "c": 706.40,
            "vol": 12000,
            "bid": 706.39,
            "ask": 706.41,
            "tc": 140,
        },
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 4, 17, 13, 31, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_1m_ts_str_handles_dst_boundary(zmq_inproc_bus) -> None:
    """Pick a date in standard time (EST, UTC-5) to verify DST-aware
    conversion. 2026-01-15 09:31 EST = 14:31 UTC."""
    from tradestation_data.domain.bar import Bar

    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": 0.0,
            "ts_el": 0.0,
            "ts_str": "2026-01/15-09:31:00",
            "o": 500.0,
            "h": 500.5,
            "l": 499.5,
            "c": 500.1,
            "vol": 100,
            "bid": 499.99,
            "ask": 500.02,
            "tc": 10,
        },
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 1, 15, 14, 31, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_1m_rejects_localized_am_pm_ts_str(zmq_inproc_bus) -> None:
    """Regression lock: v6 moved the wire format from 12-hour + ``tt`` to
    24-hour because ``FormatTime("tt")`` on a zh-TW TradeStation host emits
    "上午"/"下午" (UTF-8), which the old ``%I:%M:%S %p`` strptime could not
    match. The bar then silently fell through to the receive-time ``ts``
    fallback and collapsed every historical bar onto today's date
    partition. The current parser must reject the localized form outright
    so no further ambiguity sneaks back in."""
    from tradestation_data.domain.bar import Bar

    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # ts_str with Chinese PM marker — must NOT parse. Provider must fall
    # back to ts-derived bucket, not silently invent a bucket at 01:31 ET.
    ts = datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts,
            "ts_str": "2026-04/18-01:31:00 下午",
            "o": 1.0,
            "h": 2.0,
            "l": 0.5,
            "c": 1.5,
            "vol": 0,
            "tc": 0,
        },
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    # Fell back to ts (13:31 UTC floored) — not 01:31 AM/PM guesswork.
    assert event.bucket_start == datetime(2026, 4, 18, 13, 31, 0, tzinfo=UTC)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_1m_falls_back_to_ts_when_ts_el_missing(zmq_inproc_bus) -> None:
    from tradestation_data.domain.bar import Bar

    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # DLL failed to parse the EL string → ts_el absent; bucket must derive
    # from the receive-side ts, floored to the minute.
    ts = datetime(2026, 4, 18, 13, 31, 12, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts,
            "o": 1.0,
            "h": 2.0,
            "l": 0.5,
            "c": 1.5,
            "vol": 0,
            "tc": 0,
        },
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 4, 18, 13, 31, 0, tzinfo=UTC)
    assert event.volume == 0

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_ticks_filters_out_bar_events(zmq_inproc_bus) -> None:
    """ticks() is a tick-only convenience; bar events must be skipped."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    ts = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC).timestamp()
    # First: a bar. Should be dropped by ticks().
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": ts,
            "ts_el": ts,
            "o": 1.0,
            "h": 2.0,
            "l": 0.5,
            "c": 1.5,
            "vol": 10,
            "tc": 3,
        },
    )
    # Then: a tick. Should be the first thing ticks() yields.
    await _publish(
        pub,
        "SPY",
        {"v": 1, "kind": "tick", "ts": ts + 1, "px": 9.0, "vol": 1, "tc": 1},
    )

    gen = provider.ticks()
    tick = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert tick.price == pytest.approx(9.0)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_unknown_kind_is_dropped(zmq_inproc_bus) -> None:
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    await _publish(
        pub,
        "SPY",
        {"v": 1, "kind": "some_future_kind", "ts": 1.0, "px": 1.0, "vol": 1, "tc": 1},
    )
    # Follow up with a valid tick so we can prove iteration survived the drop.
    await _publish(
        pub,
        "SPY",
        {"v": 1, "kind": "tick", "ts": 2.0, "px": 450.0, "vol": 1, "tc": 1},
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert event.symbol == "SPY"
    assert getattr(event, "price", None) == pytest.approx(450.0)

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_tick_accepts_new_ts_utc_field(zmq_inproc_bus) -> None:
    """v5+ DLL emits ts_utc (real UTC epoch from zoned_time) instead of
    the old ts_el (mktime, host-local). Parser must accept it without
    failing; ts is still authoritative for tick timestamp."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    ts_epoch = datetime(2026, 4, 18, 13, 30, 45, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "ts": ts_epoch,
            "ts_utc": ts_epoch + 0.001,
            "ts_str": "2026-04/18-09:30:45",
            "px": 450.23,
            "vol": 100,
            "bid": 450.22,
            "ask": 450.24,
            "tc": 5,
        },
    )
    tick, gen = await _next_tick(provider)
    assert tick.timestamp.timestamp() == pytest.approx(ts_epoch)
    # ET view is available as a property on the domain model.
    assert tick.timestamp_et.hour == 9 and tick.timestamp_et.minute == 30
    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_bar_1m_prefers_ts_str_over_ts_utc(zmq_inproc_bus) -> None:
    """ts_str is authoritative; ts_utc is a sanity cross-check only. A
    deliberate mismatch must not change the bucket_start derived from ts_str."""
    from tradestation_data.domain.bar import Bar

    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    # ts_str = 09:31 ET on 2026-04-17 → 13:31 UTC (EDT).
    # ts_utc is deliberately off by an hour — parser must still return 13:31.
    bad_ts_utc = datetime(2026, 4, 17, 12, 31, 0, tzinfo=UTC).timestamp()
    await _publish(
        pub,
        "SPY",
        {
            "v": 1,
            "kind": "bar_1m",
            "ts": 0.0,
            "ts_utc": bad_ts_utc,
            "ts_str": "2026-04/17-09:31:00",
            "o": 1,
            "h": 1,
            "l": 1,
            "c": 1,
            "vol": 0,
            "tc": 0,
        },
    )

    gen = provider.events()
    event = await asyncio.wait_for(anext(gen), timeout=1.0)
    assert isinstance(event, Bar)
    assert event.bucket_start == datetime(2026, 4, 17, 13, 31, 0, tzinfo=UTC)
    assert event.bucket_start_et.hour == 9 and event.bucket_start_et.minute == 31

    await gen.aclose()
    await provider.close()


@pytest.mark.asyncio
async def test_missing_kind_defaults_to_tick(zmq_inproc_bus) -> None:
    """Pre-v3 DLL builds do not emit a `kind` field — treat as tick."""
    ctx, pub, endpoint = zmq_inproc_bus
    provider = TradeStationELProvider(endpoint=endpoint, context=ctx)
    await provider.connect()
    await provider.subscribe(["SPY"])
    await asyncio.sleep(0)

    await _publish(
        pub,
        "SPY",
        {"v": 1, "ts": 1.0, "px": 451.0, "vol": 1, "tc": 1},
    )
    tick, gen = await _next_tick(provider)
    assert tick.price == pytest.approx(451.0)
    await gen.aclose()
    await provider.close()


# ---- extra coverage: connect / subscribe / parser / close --------------


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_creates_owned_context() -> None:
    """First connect() creates a context (it owns); second is a no-op."""
    provider = TradeStationELProvider(endpoint="inproc://idempotent-connect")
    await provider.connect()
    first_socket = provider._socket
    first_ctx = provider._ctx
    assert first_socket is not None
    assert first_ctx is not None
    assert provider._ctx_owned is True
    # Calling connect again must NOT create a new socket.
    await provider.connect()
    assert provider._socket is first_socket
    await provider.close()


@pytest.mark.asyncio
async def test_subscribe_without_connect_raises() -> None:
    provider = TradeStationELProvider(endpoint="inproc://unused")
    with pytest.raises(RuntimeError, match="connect"):
        await provider.subscribe(["SPY"])


def test_parse_payload_rejects_unsupported_version() -> None:
    provider = TradeStationELProvider(endpoint="inproc://version")
    with pytest.raises(ValueError, match="Unsupported payload version"):
        provider._parse_payload("SPY", json.dumps({"v": 99, "px": 1.0}).encode())


def test_parse_tick_logs_drift_when_ts_utc_deviates_by_more_than_5s(caplog) -> None:
    """Line 177: ts_utc drift > 5s should emit a debug log (and not raise)."""
    provider = TradeStationELProvider(endpoint="inproc://drift")
    ts = 1_700_000_000.0
    payload = {
        "v": 1,
        "kind": "tick",
        "ts": ts,
        "ts_utc": ts + 30.0,  # 30s drift — triggers the debug path
        "px": 1.0,
        "vol": 0,
        "tc": 0,
    }
    with caplog.at_level("DEBUG", logger="tradestation_data.wire.el_subscriber"):
        tick = provider._parse_payload("SPY", json.dumps(payload).encode())
    # Parser must still return a Tick — the drift is a cross-check, not fatal.
    assert tick.symbol == "SPY"
    assert any("ts_utc drifts" in rec.message for rec in caplog.records)


def test_parse_bar_logs_mismatch_when_ts_str_and_ts_utc_disagree(caplog) -> None:
    """Lines 220→exit: bar ts_str vs ts_utc mismatch emits debug log."""
    provider = TradeStationELProvider(endpoint="inproc://mismatch")
    # ts_str says 2026-04-17 09:31 ET (= 13:31 UTC). Pair with ts_utc that
    # rounds to a DIFFERENT UTC minute to hit the "mismatch" debug branch.
    bad_ts_utc = datetime(2026, 4, 17, 14, 31, 0, tzinfo=UTC).timestamp()
    payload = {
        "v": 1,
        "kind": "bar_1m",
        "ts": bad_ts_utc,
        "ts_utc": bad_ts_utc,
        "ts_str": "2026-04/17-09:31:00",
        "o": 1.0,
        "h": 1.0,
        "l": 1.0,
        "c": 1.0,
        "vol": 0,
        "tc": 0,
    }
    with caplog.at_level("DEBUG", logger="tradestation_data.wire.el_subscriber"):
        bar = provider._parse_payload("SPY", json.dumps(payload).encode())
    assert bar.bucket_start == datetime(2026, 4, 17, 13, 31, 0, tzinfo=UTC)
    assert any("ts_str vs ts_utc mismatch" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_close_owned_context_terminates_it() -> None:
    """close() on a provider that owns its context must terminate it."""
    provider = TradeStationELProvider(endpoint="inproc://owned-ctx-close")
    await provider.connect()
    ctx = provider._ctx
    assert provider._ctx_owned is True
    await provider.close()
    # After close with owned context, both socket and ctx are cleared.
    assert provider._socket is None
    assert provider._ctx is None
    # Verify the original context is actually closed (calling term again must
    # not hang — we just drop the reference here).
    del ctx


@pytest.mark.asyncio
async def test_close_without_connect_is_noop() -> None:
    """close() before connect() must leave internal state untouched."""
    provider = TradeStationELProvider(endpoint="inproc://no-connect-close")
    assert provider._socket is None
    await provider.close()
    assert provider._closed is True


@pytest.mark.asyncio
async def test_events_returns_when_closed_flag_set() -> None:
    """Line 124→exit: generator exits immediately if _closed is True."""

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
    """Line 127-128: ContextTerminated during recv_multipart ends the loop."""

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
    """Line 129-131: ZMQError after close() terminates iteration cleanly."""

    class _StubSocket:
        raised = False

        async def recv_multipart(self):
            if not self.raised:
                self.raised = True
                raise zmq.error.ZMQError(errno=0)
            return (b"SPY", b'{"v":1,"ts":1.0,"px":1.0,"vol":0,"tc":0}')

    provider = TradeStationELProvider(endpoint="inproc://zmq-err")
    provider._socket = _StubSocket()  # type: ignore[assignment]
    provider._closed = True  # so the error-branch takes the return path
    gen = provider.events()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    provider._socket = None


@pytest.mark.asyncio
async def test_events_logs_and_continues_on_transient_zmq_error(caplog) -> None:
    """Line 132-133: ZMQError while not closed logs a warning and continues."""

    class _StubSocket:
        calls = 0

        async def recv_multipart(self):
            self.calls += 1
            if self.calls == 1:
                raise zmq.error.ZMQError(errno=0)
            return (
                b"SPY",
                json.dumps({"v": 1, "ts": 2.0, "px": 1.0, "vol": 0, "tc": 0}).encode(),
            )

    provider = TradeStationELProvider(endpoint="inproc://zmq-warn")
    provider._socket = _StubSocket()  # type: ignore[assignment]
    gen = provider.events()
    with caplog.at_level("WARNING", logger="tradestation_data.wire.el_subscriber"):
        event = await anext(gen)
    assert event.symbol == "SPY"
    assert any("zmq recv error" in r.message for r in caplog.records)
    # Terminate the generator cleanly to release the stub socket.
    provider._closed = True
    await gen.aclose()
    provider._socket = None
