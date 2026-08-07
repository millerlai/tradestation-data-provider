"""Check this binding against the shared contract fixtures.

Every binding, in any language, must pass the same files in
``contract/fixtures/``. These tests are the Python side of that bar, and the
template for what a future Go or Rust binding has to satisfy.

The fixtures hold real DLL output; the expectations were derived from
``contract/semantics.md`` by hand rather than produced by this code, so a
failure here means the binding and the contract genuinely disagree.

**Frames go through the public receive path**, not through the payload
parser. The contract does not end at JSON decoding: §5's exact-topic filter
and "an unknown ``kind`` is skipped and logged, never raised" both live in
``events()``, and a suite that called ``_parse_payload`` directly could not
reach either — it would read a raising parser and a compliant skip as the
same thing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import zmq

from conftest import CONTRACT_DIR, load_case
from tradestation_data.wire.el_subscriber import CONTROL_TOPIC, TradeStationELProvider

# One protocol, one set of fixtures. The superseded wire versions are gone
# rather than kept as compatibility cases: a proto-1 binding refuses those
# frames outright, which is the whole point of renaming the version key.
CASES = ["smoke", "noquote", "bars", "session"]

# The chart-announcement fixture. Kept out of CASES because it holds no
# points at all: every assertion in the point suite iterates
# `expected["events"]`, which is empty here, so including it would run a row
# of tests that vacuously pass and read as coverage. Its own contract —
# announcements are consumed, reported, and never surfaced as market data —
# is checked in the section at the bottom of this file.
HELLO_CASE = "hello"

# EasyLanguage's reserved words, verbatim. Asserted as a group because the
# failure this guards against is a swap between two of them, which checking
# any one in isolation cannot see.
EL_QUANTITIES = ("el_volume", "el_ticks", "el_upticks", "el_downticks", "el_open_interest")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _ReplaySocket:
    """Hands recorded frames to ``events()`` and then ends the stream.

    ContextTerminated is how a real socket reports "the context went away",
    and ``events()`` returns cleanly on it — which is what lets a test drain
    the generator instead of cancelling it.
    """

    def __init__(self, frames: list[tuple[str, bytes]]) -> None:
        self._frames = list(frames)

    async def recv_multipart(self) -> tuple[bytes, bytes]:
        if not self._frames:
            raise zmq.error.ContextTerminated()
        topic, payload = self._frames.pop(0)
        return topic.encode("utf-8"), payload

    def close(self, linger: int = 0) -> None:  # pragma: no cover - never reached
        pass


async def _decode_all(
    frames: list[tuple[str, bytes]],
    *,
    subscribed: set[str] | None = None,
) -> tuple[list[Any], TradeStationELProvider]:
    """Drive the public receive path over the recorded frames."""
    provider = TradeStationELProvider()
    provider._socket = _ReplaySocket(frames)  # type: ignore[assignment]
    provider._subscribed = {t for t, _ in frames} if subscribed is None else subscribed
    events = [event async for event in provider.events()]
    provider._socket = None
    return events, provider


@pytest.mark.parametrize("case", CASES)
async def test_event_count_matches(case: str) -> None:
    frames, expected = load_case(case)
    events, _ = await _decode_all(frames)
    assert len(events) == len(expected["events"])


@pytest.mark.parametrize("case", CASES)
async def test_every_field_matches_the_contract(case: str) -> None:
    frames, expected = load_case(case)
    events, _ = await _decode_all(frames)

    for i, (event, want) in enumerate(zip(events, expected["events"], strict=True)):
        where = f"{case}[{i}] {want['symbol']}"

        assert event.symbol == want["symbol"], where
        # The quantity words are forwarded, never selected between. An
        # intraday el_volume that matched el_ticks would mean something in
        # this binding had "helpfully" reconciled them.
        for q in EL_QUANTITIES:
            assert getattr(event, q) == want[q], f"{where}: {q}"

        assert _iso(event.bar_time) == want["bar_time"], where
        assert event.open == pytest.approx(want["open"]), where
        assert event.high == pytest.approx(want["high"]), where
        assert event.low == pytest.approx(want["low"]), where
        assert event.close == pytest.approx(want["close"]), where

        # The chart's own words decide the storage partition, so a wrong one
        # is silent corruption rather than a cosmetic slip.
        assert event.bar_type == want["bar_type"], where
        assert event.bar_interval == want["bar_interval"], where
        assert event.category == want["category"], where

        # semantics.md §1 — the receive clock lands verbatim. On a tick chart
        # it is the only sub-minute ordering the rows have, so a binding that
        # dropped it could not be told apart from one that kept it without
        # this line.
        assert event.ts == pytest.approx(want["ts"]), where

        # semantics.md §3 — a quote is absent when the wire says null. Every
        # point carries the pair, bars included.
        if want["bid"] is None:
            assert event.bid is None, f"{where}: bid must be absent"
        else:
            assert event.bid == pytest.approx(want["bid"]), where
        if want["ask"] is None:
            assert event.ask is None, f"{where}: ask must be absent"
        else:
            assert event.ask == pytest.approx(want["ask"]), where


@pytest.mark.parametrize("case", CASES)
def test_every_point_carries_the_quote_pair(case: str) -> None:
    """bid/ask travel on every frame, whatever the chart is.

    They used to be sent only on a tick chart, because a live-quote function
    describes the moment of the call rather than the bar. That is a statement
    about what the number MEANS, and the publisher does not get to make those
    — a consumer that agrees can ignore the field.
    """
    import json as _json

    frames, _expected = load_case(case)
    for topic, payload in frames:
        raw = _json.loads(payload)
        assert "bid" in raw and "ask" in raw, f"{case} {topic}"


def test_the_binding_blanks_nobodys_quote(case_symbol: str = "VXX") -> None:
    """What the wire says about a quote is what lands. Nothing is discarded.

    There used to be a hard-coded list of index / breadth symbols whose
    bid/ask were nulled at parse time because their live numbers "mean
    nothing". `VXX` was on that list, and VXX is a tradeable ETN — measured
    live, 567,776 shares in one bar and a real two-sided quote. The list was
    a guess in both directions, and the judgement it encoded belongs to the
    consumer, who now has `category` to make it with.
    """
    import json as _json

    frames, expected = load_case("smoke")
    rows = [(t, p) for t, p in frames if t == case_symbol]
    assert rows, "fixture no longer contains this symbol"

    for _topic, payload in rows:
        raw = _json.loads(payload)
        assert raw["bid"] is not None and raw["bid"] != 0.0

    want = [e for e in expected["events"] if e["symbol"] == case_symbol]
    assert all(e["bid"] is not None for e in want), "a real quote must survive"


def test_an_absent_quote_is_null_on_the_wire_and_none_here() -> None:
    """§3.1 — the publisher spells "no quote" as null, and it lands as None."""
    import json as _json

    frames, expected = load_case("noquote")
    spy = [(t, p) for t, p in frames if t == "SPY"]
    assert spy, "fixture lost its non-index symbol"
    assert any(_json.loads(p)["bid"] is None for _t, p in spy)
    assert any(e["bid"] is None for e in expected["events"] if e["symbol"] == "SPY")


def test_session_edges_keep_the_publisher_s_own_times() -> None:
    """§2 — a bar's timestamp lands exactly as TradeStation sent it.

    The fixture publishes the session's first and last 1m bar as EL stamps
    them, 09:31 and 16:00. Both arrive unchanged.

    This test used to require 09:30 / 15:59: the binding shifted every bar
    back a minute and snapped it onto a grid, to turn EL's close time into a
    left label. That conversion was this binding's invention and it is gone.
    It was also lossy — on a 60-minute chart two published bars could snap
    onto one slot and the earlier one was overwritten. Whoever wants left
    labels now derives them downstream, where the chart's session template is
    known.
    """
    _frames, expected = load_case("session")
    buckets = [e["bar_time"] for e in expected["events"]]
    # 13:31Z / 20:00Z == 09:31 / 16:00 ET on 2026-04-20 (EDT).
    assert buckets == ["2026-04-20T13:31:00Z", "2026-04-20T20:00:00Z"]


@pytest.mark.parametrize("case", CASES)
async def test_sequenced_feed_reports_a_real_zero(case: str) -> None:
    """Every proto-1 frame carries `seq`, so "cannot tell" is no longer a state."""
    frames, expected = load_case(case)
    _events, provider = await _decode_all(frames)
    assert expected["expected_messages_lost"] == 0
    assert provider.gap_detection_available is True
    assert provider.messages_lost == 0


@pytest.mark.parametrize("case", CASES)
def test_every_frame_declares_this_protocol(case: str) -> None:
    """`proto` is what makes a superseded publisher's frame unreadable rather
    than misreadable, so the fixtures must keep proving it is really there."""
    import json as _json

    frames, expected = load_case(case)
    assert expected["proto"] == 2
    for _topic, payload in frames:
        assert _json.loads(payload)["proto"] == 2


# ---- rules that only exist on the public receive path ----------------------


async def test_prefix_matched_topic_is_dropped() -> None:
    """§5 — SUBSCRIBE is a prefix match, so a SPY subscription also gets SPYG.

    Replays the smoke frames with SPY's payloads re-topiced onto SPYG while
    only SPY is subscribed. Every SPYG frame must be dropped.
    """
    frames, _expected = load_case("smoke")
    spy = [(t, p) for t, p in frames if t == "SPY"]
    mixed = [("SPYG", p) for _t, p in spy] + spy

    events, _ = await _decode_all(mixed, subscribed={"SPY"})

    assert len(events) == len(spy)
    assert {e.symbol for e in events} == {"SPY"}


async def test_a_frame_missing_a_required_field_is_skipped_not_fatal() -> None:
    """A frame this binding cannot read is logged and skipped, never raised.

    Only `events()` satisfies this; `_parse_payload` raises by design. A suite
    that stopped at the parser could not tell the two apart.

    There is no `kind` to be unknown any more — one frame shape means the way
    a frame can be unreadable is a field that is not there.
    """
    frames, _expected = load_case("smoke")
    bogus = (
        "SPY",
        b'{"proto":2,"seq":99,"sid":1,"ts":1.0,"ts_str":""}',
    )
    events, _ = await _decode_all([bogus, *frames])

    assert len(events) == len(frames), "the unreadable frame must not take the stream down"


async def test_a_frame_from_a_superseded_publisher_is_refused_not_misread() -> None:
    """The upgrade hazard, end to end.

    The DLL sits in the operator's TradeStation install and does not move
    when a package does, so "new binding, old DLL" is the ordinary failure.
    Under the old `v` key a restart at 1 would have made {"v":1} legal for
    both protocols; under `proto` the frame simply is not this protocol, and
    the binding drops it and keeps reading rather than filing fields that
    happen to line up.
    """
    frames, _expected = load_case("smoke")
    legacy = (
        "SPY",
        b'{"v":4,"pv":1,"kind":"tick","seq":1,"sid":1,"ts":1.0,'
        b'"px":450.0,"vol":100,"tc":5,"bid":449.99,"ask":450.01}',
    )
    events, _ = await _decode_all([legacy, *frames])

    assert len(events) == len(frames), "the legacy frame must be dropped, not decoded"


# ---- JSON Schema ----------------------------------------------------------


def _schema_path_for(topic: str) -> str:
    """The topic is the discriminator, exactly as it is on the wire.

    Two frame shapes, two schemas, and nothing in the payload tells them
    apart — that is deliberate. A `kind` field was removed from this
    protocol once and is not coming back, so a binding that guessed the
    shape from the payload's keys would be inventing a discriminator the
    wire does not have.
    """
    return "hello.schema.json" if topic == CONTROL_TOPIC else "point.schema.json"


@pytest.mark.parametrize("case", [*CASES, HELLO_CASE])
def test_recorded_frames_validate_against_the_published_schema(case: str) -> None:
    """Execute contract/*.schema.json instead of only shipping them.

    Both schemas set ``additionalProperties: false`` plus a ``required``
    list, so the moment the DLL adds or renames a field the schema is wrong —
    and until something ran it, nothing said so. That is exactly the drift
    contract/README.md promises this repo makes fail in CI.
    """
    import json as _json

    jsonschema = pytest.importorskip("jsonschema")

    frames, _expected = load_case(case)
    for topic, payload_bytes in frames:
        payload = _json.loads(payload_bytes)
        rel = _schema_path_for(topic)
        schema = _json.loads((CONTRACT_DIR / rel).read_text(encoding="utf-8"))
        try:
            jsonschema.validate(payload, schema)
        except jsonschema.ValidationError as exc:  # pragma: no cover - failure path
            pytest.fail(f"{case} {topic} does not match {rel}: {exc.message}")


# ---- chart announcements ---------------------------------------------------
#
# wire.md "hello — chart 宣告 frame" lists five obligations for a binding.
# Every one of them is checked here against the recorded fixture, because
# they are what a second binding in another language has to satisfy too.


async def test_announcements_are_consumed_and_never_surfaced() -> None:
    """Obligation 2: a hello is not a data point.

    `MarketEvent` is `Bar`. A binding that yielded these would hand its
    consumer a frame with no OHLC, no quantity words and no quote, and every
    downstream sink would need a second case for something that is not
    market data.
    """
    frames, expected = load_case(HELLO_CASE)
    assert frames, "the hello fixture must not be empty"
    assert all(topic == CONTROL_TOPIC for topic, _ in frames)

    events, provider = await _decode_all(frames, subscribed=set())
    assert events == []
    assert provider.frames_refused == 0
    assert expected["events"] == []


async def test_announced_charts_match_the_contract() -> None:
    """Obligations 1 and 3: subscribed unconditionally, and reported.

    The chart identity is `(symbol, bar_type, bar_interval)` — one symbol can
    legitimately be open on several charts at once, and they are different
    series that partition separately.
    """
    frames, expected = load_case(HELLO_CASE)
    _, provider = await _decode_all(frames, subscribed=set())

    want = {
        (a["symbol"], a["bar_type"], a["bar_interval"]): a["category"]
        for a in expected["announcements"]
    }
    assert provider.announced_charts == want


async def test_unsubscribed_chart_is_warned_about_not_reported_as_receiving(
    caplog,
) -> None:
    """Obligation 3, the half that matters.

    A chart on a symbol this consumer never subscribed to produces no data
    at all. Reporting it as "now receiving" would be false; saying nothing
    leaves an operator watching an empty partition with no explanation. The
    fixture announces SPY and QQQ, so subscribing to SPY alone exercises
    both branches in one pass.
    """
    frames, _ = load_case(HELLO_CASE)
    with caplog.at_level("INFO", logger="tradestation_data.wire.el_subscriber"):
        await _decode_all(frames, subscribed={"SPY"})

    receiving = [r for r in caplog.records if r.message == "chart_announced_now_receiving"]
    warned = [r for r in caplog.records if r.message == "chart_announced_but_not_subscribed"]
    assert [r.symbol for r in receiving] == ["SPY"]
    assert [r.symbol for r in warned] == ["QQQ"]
    assert warned[0].levelname == "WARNING"


async def test_control_topic_has_its_own_sequence() -> None:
    """The announcement counter is independent of every symbol's.

    Sharing a symbol's counter would make an announcement look like a gap to
    a consumer filtering on that symbol, and vice versa.
    """
    frames, expected = load_case(HELLO_CASE)
    import json as _json

    seqs = [_json.loads(payload)["seq"] for _, payload in frames]
    assert seqs == [a["seq"] for a in expected["announcements"]] == [1, 2]

    sids = {_json.loads(payload)["sid"] for _, payload in frames}
    assert len(sids) == 1, "one harness run is one publisher session"


async def test_malformed_announcement_is_refused_without_ending_the_stream() -> None:
    """Obligations 4 and 5.

    A hello carrying a JSON null symbol must not be coerced with `str()` into
    the string "None" and registered as a chart — that reads like a real
    symbol nobody subscribed to. And nothing here may raise: letting it
    escape kills the ingest task while the runtime sits on its stop event.
    """
    frames, _ = load_case(HELLO_CASE)
    good_topic, good_payload = frames[0]
    broken = [
        (good_topic, b"{not json"),
        (
            good_topic,
            b'{"proto":2,"seq":1,"sid":1,"ts":0.0,"symbol":null,'
            b'"category":2,"bar_type":1,"bar_interval":1}',
        ),
        (good_topic, good_payload),
    ]

    events, provider = await _decode_all(broken, subscribed=set())
    assert events == []
    assert provider.frames_refused == 2
    assert len(provider.announced_charts) == 1
