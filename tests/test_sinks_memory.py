from __future__ import annotations

from datetime import UTC, datetime

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.memory import InMemorySink


def _tick(symbol: str = "SPY") -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=datetime(2026, 4, 18, 13, 30, tzinfo=UTC),
        price=1.0,
        volume=1,
        bid=None,
        ask=None,
        tick_count=1,
        source="t",
    )


def _bar(symbol: str = "SPY") -> Bar:
    return Bar(
        symbol=symbol,
        bucket_start=datetime(2026, 4, 18, 13, 30, tzinfo=UTC),
        open=1.0,
        high=1.5,
        low=0.5,
        close=1.2,
        volume=10,
        vwap=1.1,
        tick_count=3,
        source="t",
    )


def test_in_memory_sink_buffers_per_symbol_and_global() -> None:
    sink = InMemorySink(name="mem")
    sink.on_tick(_tick("SPY"))
    sink.on_tick(_tick("AAPL"))
    sink.on_bar(_bar("SPY"))

    assert len(sink.ticks("SPY")) == 1
    assert len(sink.ticks("AAPL")) == 1
    assert len(sink.ticks()) == 2  # all-symbols view
    assert len(sink.bars("SPY")) == 1
    assert len(sink.bars("AAPL")) == 0
    assert set(sink.symbols()) == {"SPY", "AAPL"}


def test_in_memory_sink_respects_max_cap() -> None:
    sink = InMemorySink(name="mem", max_per_symbol=2)
    for _ in range(5):
        sink.on_tick(_tick("SPY"))
    # Per-symbol deque caps at 2, oldest dropped.
    assert len(sink.ticks("SPY")) == 2
    # Cross-symbol view uses the same cap.
    assert len(sink.ticks()) == 2


def test_in_memory_sink_clear_resets_state() -> None:
    sink = InMemorySink(name="mem")
    sink.on_tick(_tick())
    sink.on_bar(_bar())
    sink.clear()
    assert sink.ticks() == []
    assert sink.bars() == []


def test_in_memory_sink_does_not_advertise_flush() -> None:
    sink = InMemorySink(name="mem")
    assert sink.should_flush() is False
    sink.flush()
    sink.close()
