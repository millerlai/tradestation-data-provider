"""Check this binding against the shared contract fixtures.

Every binding, in any language, must pass the same files in
``contract/fixtures/``. These tests are the Python side of that bar, and the
template for what a future Go or Rust binding has to satisfy.

The fixtures hold real DLL output; the expectations were derived from
``contract/semantics.md`` by hand rather than produced by this code, so a
failure here means the binding and the contract genuinely disagree.

**Frames go through the public receive path**, not through the payload
parser. The contract does not end at JSON decoding: §5's exact-topic filter
and compat.md's "an unknown ``kind`` is skipped and logged, never raised"
both live in ``events()``, and a suite that called ``_parse_payload``
directly could not reach either — it would read a raising parser and a
compliant skip as the same thing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import zmq

from conftest import CONTRACT_DIR, load_case
from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.wire.el_subscriber import TradeStationELProvider

# The unprefixed cases are the current wire version; the prefixed ones are
# superseded versions the DLL may still be emitting from a user's TradeStation
# install, which compat.md requires every binding to keep reading.
CURRENT_CASES = ["smoke", "noquote", "bars", "session"]
V3_CASES = ["v3_smoke", "v3_noquote", "v3_bars", "v3_session"]
CASES = [*CURRENT_CASES, *V3_CASES, "v1_legacy", "v1_noquote"]


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

        if want["kind"] == "tick":
            assert isinstance(event, Tick), where
            assert _iso(event.timestamp) == want["timestamp"], where
            assert event.price == pytest.approx(want["price"]), where
            assert event.volume == pytest.approx(want["volume"]), where
            assert event.tick_count == want["tick_count"], where

            # semantics.md §3 — a quote is absent when the wire says null,
            # when it is <= 0 (v1 had no null), or when the symbol is on the
            # index list. All three paths end here.
            if want["bid"] is None:
                assert event.bid is None, f"{where}: bid must be absent"
                assert event.ask is None, f"{where}: ask must be absent"
            else:
                assert event.bid == pytest.approx(want["bid"]), where
                assert event.ask == pytest.approx(want["ask"]), where
        else:
            assert isinstance(event, Bar), where
            assert _iso(event.bucket_start) == want["bucket_start"], where
            assert event.open == pytest.approx(want["open"]), where
            assert event.high == pytest.approx(want["high"]), where
            assert event.low == pytest.approx(want["low"]), where
            assert event.close == pytest.approx(want["close"]), where
            assert event.volume == pytest.approx(want["volume"]), where
            assert event.tick_count == want["tick_count"], where
            # The interval the bar covers decides its storage partition, so a
            # wrong one is silent corruption rather than a cosmetic slip.
            assert event.timeframe == want.get("timeframe", "1m"), where
            # bid/ask are deliberately not asserted for bars: the wire carries
            # them, but this binding's Bar does not model them. The expectation
            # files still record what the wire said, so a binding that does
            # keep them has something to check against. See the note in
            # expected/smoke.json.
            assert not hasattr(event, "bid"), (
                "Bar gained a bid field — assert it here and update the note "
                "in contract/fixtures/expected/smoke.json"
            )


def test_index_symbol_bid_ask_differs_from_the_wire() -> None:
    """Guard the guard: prove the null-ing is a real transform, not a no-op.

    If the DLL ever started sending nulls itself, the assertion above would
    still pass while the rule it is meant to protect had quietly stopped
    being exercised.
    """
    frames, expected = load_case("smoke")
    vxx_frames = [(t, p) for t, p in frames if t == "VXX"]
    assert vxx_frames, "fixture no longer contains an index symbol"

    import json as _json

    for _topic, payload in vxx_frames:
        raw = _json.loads(payload)
        assert raw["bid"] is not None and raw["bid"] != 0.0
        assert raw["ask"] is not None and raw["ask"] != 0.0

    vxx_expected = [e for e in expected["events"] if e["symbol"] == "VXX"]
    assert all(e["bid"] is None and e["ask"] is None for e in vxx_expected)


def test_non_index_symbol_carries_an_absent_quote_on_the_wire() -> None:
    """§3.1 has to be reachable without §3.2 rescuing the binding.

    A fixture whose only quote-less frames are breadth indices cannot tell a
    compliant binding from one that ignores the wire entirely: §3.2 blanks
    those symbols regardless. SPY is not on the index list, so it is the
    frame that actually tests the rule.
    """
    import json as _json

    for case, absent in (("noquote", None), ("v1_noquote", 0.0)):
        frames, expected = load_case(case)
        spy = [(t, p) for t, p in frames if t == "SPY"]
        assert spy, f"{case}: fixture lost its non-index symbol"
        for _topic, payload in spy:
            raw = _json.loads(payload)
            assert raw["bid"] == absent, f"{case}: wire no longer spells absence as {absent!r}"
        for want in expected["events"]:
            if want["symbol"] == "SPY" and want["kind"] == "tick":
                assert want["bid"] is None and want["ask"] is None


def test_session_edges_are_left_labelled() -> None:
    """§2 — an RTH 1m session runs 09:30 … 15:59, and 16:00 is not a bar.

    Right-labelling passes every other fixture here and differs by exactly one
    bar, which is what makes it the classic silent market-data bug.
    """
    _frames, expected = load_case("session")
    buckets = [e["bucket_start"] for e in expected["events"]]
    # 13:30Z / 19:59Z == 09:30 / 15:59 ET on 2026-04-20 (EDT).
    assert buckets == ["2026-04-20T13:30:00Z", "2026-04-20T19:59:00Z"]


def test_v2_sequence_tracking_reports_no_loss() -> None:
    _frames, expected = load_case("smoke")
    assert expected["expected_messages_lost"] == 0


async def test_v1_cannot_report_loss_and_must_not_claim_none() -> None:
    """semantics.md §6.6 — a v1 feed yields "cannot tell", not "nothing lost"."""
    frames, expected = load_case("v1_legacy")
    _events, provider = await _decode_all(frames)

    assert expected["expected_messages_lost"] is None
    # The distinction has to be reachable from the public API: a caller that
    # can only read 0 would file the whole day as verified-complete when gap
    # detection was never running at all.
    assert provider.messages_lost is None
    assert provider.gap_detection_available is False


@pytest.mark.parametrize("case", ["smoke", "v3_smoke"])
async def test_seq_bearing_feed_reports_a_real_zero(case: str) -> None:
    frames, _expected = load_case(case)
    _events, provider = await _decode_all(frames)
    assert provider.gap_detection_available is True
    assert provider.messages_lost == 0


@pytest.mark.parametrize("case", CURRENT_CASES)
def test_current_wire_declares_a_publisher_convention(case: str) -> None:
    """v4 — `pv` says which rules produced the numbers, not just where they are.

    Recorded from the harness, which declares 1 through EL_Init2. Without a
    frame that actually carries the field, nothing would notice the DLL
    dropping it again — and its absence is silently read as "the pre-§3.4
    convention", which is a real answer rather than an error.
    """
    import json as _json

    frames, expected = load_case(case)
    assert expected["wire_version"] == 4
    for _topic, payload in frames:
        raw = _json.loads(payload)
        assert raw["v"] == 4
        assert raw["pv"] == expected["publisher_version"]


@pytest.mark.parametrize("case", V3_CASES)
def test_superseded_wire_carries_no_publisher_convention(case: str) -> None:
    """The other half: v3 has no `pv`, and that is what "undeclared" looks like.

    A binding must read those frames as the pre-§3.4 convention rather than
    refusing them, so the fixtures have to keep proving the field is genuinely
    absent — not merely unasserted.
    """
    import json as _json

    frames, expected = load_case(case)
    assert expected["wire_version"] == 3
    assert "publisher_version" not in expected
    for _topic, payload in frames:
        assert "pv" not in _json.loads(payload)


async def test_v1_frames_are_accepted_not_rejected() -> None:
    """compat.md — refusing v1 would end collection for anyone on an older DLL."""
    frames, _expected = load_case("v1_legacy")
    events, _ = await _decode_all(frames)
    assert len(events) == len(frames)


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


async def test_unknown_kind_is_skipped_and_the_stream_continues() -> None:
    """compat.md — an unrecognised `kind` is logged and skipped, never raised.

    Only `events()` satisfies this; `_parse_payload` raises by design. A suite
    that stopped at the parser could not tell the two apart.
    """
    frames, _expected = load_case("smoke")
    bogus = (
        "SPY",
        b'{"v":3,"kind":"depth","seq":99,"sid":1,"ts":1.0,"ts_utc":0.0,"ts_str":""}',
    )
    events, _ = await _decode_all([bogus, *frames])

    assert len(events) == len(frames), "the unknown frame must not take the stream down"


# ---- JSON Schema ----------------------------------------------------------


def _schema_path_for(payload: dict[str, Any]) -> str:
    version = payload.get("v", 1)
    kind = payload.get("kind", "tick")
    stem = "bar" if version >= 3 and kind == "bar" else kind
    return f"v{version}/{stem}.schema.json"


@pytest.mark.parametrize("case", CASES)
def test_recorded_frames_validate_against_the_published_schema(case: str) -> None:
    """Execute contract/v*/*.schema.json instead of only shipping them.

    Every v3 schema sets ``additionalProperties: false`` plus a ``required``
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
