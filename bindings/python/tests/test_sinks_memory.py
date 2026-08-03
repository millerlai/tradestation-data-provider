from __future__ import annotations

from datetime import UTC, datetime

from tradestation_data.domain.bar import Bar
from tradestation_data.sinks.memory import InMemorySink


def _bar(symbol: str = "SPY") -> Bar:
    return Bar(
        symbol=symbol,
        bar_time=datetime(2026, 4, 18, 13, 30, tzinfo=UTC),
        open=1.0,
        high=1.5,
        low=0.5,
        close=1.2,
        el_volume=10,
        el_ticks=20,
        el_upticks=10,
        el_downticks=10,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )


def test_in_memory_sink_does_not_advertise_flush() -> None:
    sink = InMemorySink(name="mem")
    assert sink.should_flush() is False
    sink.flush()
    sink.close()
