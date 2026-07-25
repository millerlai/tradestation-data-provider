"""Check this binding against the shared contract fixtures.

Every binding, in any language, must pass the same files in
``contract/fixtures/``. These tests are the Python side of that bar, and the
template for what a future Go or Rust binding has to satisfy.

The fixtures hold real DLL output; the expectations were derived from
``contract/semantics.md`` by hand rather than produced by this code, so a
failure here means the binding and the contract genuinely disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import load_case
from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.wire.el_subscriber import TradeStationELProvider

CASES = ["smoke", "noquote", "v1_legacy"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decode_all(frames):
    """Run frames through the binding's parser, returning events and the provider."""
    provider = TradeStationELProvider()
    return [provider._parse_payload(topic, payload) for topic, payload in frames], provider


@pytest.mark.parametrize("case", CASES)
def test_event_count_matches(case: str) -> None:
    frames, expected = load_case(case)
    events, _ = _decode_all(frames)
    assert len(events) == len(expected["events"])


@pytest.mark.parametrize("case", CASES)
def test_every_field_matches_the_contract(case: str) -> None:
    frames, expected = load_case(case)
    events, _ = _decode_all(frames)

    for i, (event, want) in enumerate(zip(events, expected["events"], strict=True)):
        where = f"{case}[{i}] {want['symbol']}"

        assert event.symbol == want["symbol"], where

        if want["kind"] == "tick":
            assert isinstance(event, Tick), where
            assert _iso(event.timestamp) == want["timestamp"], where
            assert event.price == pytest.approx(want["price"]), where
            assert event.volume == pytest.approx(want["volume"]), where
            assert event.tick_count == want["tick_count"], where

            # semantics.md §3 — the wire always carries numbers here, so a
            # null expectation is the binding's job, not the publisher's.
            if want["bid"] is None:
                assert event.bid is None, f"{where}: index symbol must invalidate bid"
                assert event.ask is None, f"{where}: index symbol must invalidate ask"
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
            # them on bar_1m, but this binding's Bar does not model them. The
            # expectation files still record what the wire said, so a binding
            # that does keep them has something to check against. See the
            # note in expected/smoke.json.
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


def test_v2_sequence_tracking_reports_no_loss() -> None:
    frames, expected = load_case("smoke")
    _events, provider = _decode_all(frames)
    assert provider.messages_lost == expected["expected_messages_lost"] == 0


def test_v1_cannot_report_loss_and_must_not_claim_none() -> None:
    """semantics.md §6.6 — a v1 feed yields "cannot tell", not "nothing lost"."""
    frames, expected = load_case("v1_legacy")
    _events, provider = _decode_all(frames)

    assert expected["expected_messages_lost"] is None
    # The counter reads 0 because nothing can be counted. What must hold is
    # that no seq was observed at all — otherwise a caller could mistake this
    # for a verified-clean feed.
    assert provider.messages_lost == 0
    assert provider._seq._sid is None, "v1 frames must not populate sequence state"


def test_v1_frames_are_accepted_not_rejected() -> None:
    """compat.md — refusing v1 would end collection for anyone on an older DLL."""
    frames, _expected = load_case("v1_legacy")
    events, _ = _decode_all(frames)
    assert len(events) == len(frames)
