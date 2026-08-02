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
from tradestation_data.wire.el_subscriber import TradeStationELProvider

# One protocol, one set of fixtures. The superseded wire versions are gone
# rather than kept as compatibility cases: a proto-1 binding refuses those
# frames outright, which is the whole point of renaming the version key.
CASES = ["smoke", "noquote", "bars", "session"]

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


def _schema_path_for(payload: dict[str, Any]) -> str:
    """One protocol and one frame shape, so one schema at the contract root."""
    return "point.schema.json"


@pytest.mark.parametrize("case", CASES)
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
        rel = _schema_path_for(payload)
        schema = _json.loads((CONTRACT_DIR / rel).read_text(encoding="utf-8"))
        try:
            jsonschema.validate(payload, schema)
        except jsonschema.ValidationError as exc:  # pragma: no cover - failure path
            pytest.fail(f"{case} {topic} does not match {rel}: {exc.message}")
