from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.callback import CallbackSink, get_sink


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


def test_callback_sink_dispatch_per_symbol() -> None:
    sink = CallbackSink(name="cb1")
    spy_bars: list[Bar] = []
    aapl_bars: list[Bar] = []
    sink.on("SPY", "bar", spy_bars.append)
    sink.on("AAPL", "bar", aapl_bars.append)

    sink.on_bar(_bar("SPY"))
    sink.on_bar(_bar("AAPL"))
    sink.on_bar(_bar("MSFT"))  # no handler — silently ignored

    assert len(spy_bars) == 1 and spy_bars[0].symbol == "SPY"
    assert len(aapl_bars) == 1 and aapl_bars[0].symbol == "AAPL"


def test_callback_sink_on_any_receives_every_symbol() -> None:
    sink = CallbackSink(name="cb2")
    seen: list[str] = []
    sink.on_any("tick", lambda t: seen.append(t.symbol))

    for sym in ("SPY", "AAPL", "MSFT"):
        sink.on_tick(_tick(sym))

    assert seen == ["SPY", "AAPL", "MSFT"]


def test_callback_sink_off_removes_handler() -> None:
    sink = CallbackSink(name="cb3")
    calls: list[Bar] = []
    handle = sink.on("SPY", "bar", calls.append)

    sink.on_bar(_bar())
    assert len(calls) == 1
    assert sink.off(handle) is True
    sink.on_bar(_bar())
    assert len(calls) == 1  # handler removed, no further calls
    # Double-off is a benign no-op.
    assert sink.off(handle) is False


def test_callback_sink_invalid_kind_raises() -> None:
    sink = CallbackSink(name="cb4")
    with pytest.raises(ValueError, match="kind must be 'tick' or 'bar'"):
        sink.on("SPY", "candle", lambda _: None)  # type: ignore[arg-type]


def test_callback_sink_non_callable_raises() -> None:
    sink = CallbackSink(name="cb5")
    with pytest.raises(TypeError, match="must be callable"):
        sink.on("SPY", "bar", "not_a_function")  # type: ignore[arg-type]


def test_callback_sink_callback_exception_isolated(caplog) -> None:
    sink = CallbackSink(name="cb6")
    survivors: list[Bar] = []

    def _boom(_bar_arg: Bar) -> None:
        raise RuntimeError("user error")

    sink.on_any("bar", _boom)
    sink.on_any("bar", survivors.append)

    with caplog.at_level(logging.ERROR):
        sink.on_bar(_bar())

    assert len(survivors) == 1
    assert any("callback_failed" in r.message for r in caplog.records)


def test_get_sink_returns_registered_instance() -> None:
    sink = CallbackSink(name="cb7")
    assert get_sink("cb7") is sink


def test_get_sink_raises_when_unknown() -> None:
    with pytest.raises(KeyError, match="cb_does_not_exist"):
        get_sink("cb_does_not_exist")


def test_close_clears_registry_and_handlers() -> None:
    sink = CallbackSink(name="cb8")
    calls: list[Bar] = []
    sink.on_any("bar", calls.append)
    sink.close()
    # Lookup fails after close — registry entry was eagerly removed.
    with pytest.raises(KeyError):
        get_sink("cb8")
    # Any further events post-close don't reach the (cleared) handlers.
    sink.on_bar(_bar())
    assert calls == []
