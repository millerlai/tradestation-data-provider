from __future__ import annotations

import logging
from datetime import UTC, datetime

from tradestation_data.domain.bar import Bar
from tradestation_data.sinks.base import BaseSink, Sink
from tradestation_data.sinks.pipeline import SinkPipeline


def _bar(symbol: str = "SPY") -> Bar:
    return Bar(
        symbol=symbol,
        bar_time=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
        open=450.0,
        high=451.0,
        low=449.5,
        close=450.5,
        el_volume=1000,
        el_ticks=2000,
        el_upticks=1000,
        el_downticks=1000,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )


class _RecordingSink(BaseSink):
    """Captures every call for assertion purposes."""

    def __init__(self, name: str = "rec") -> None:
        self.name = name
        self.bars: list[Bar] = []
        self.flush_calls = 0
        self.close_calls = 0
        self._wants_flush = False

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)

    def should_flush(self) -> bool:
        return self._wants_flush

    def flush(self) -> None:
        self.flush_calls += 1
        self._wants_flush = False

    def close(self) -> None:
        self.close_calls += 1


def test_baseSink_satisfies_protocol() -> None:
    assert isinstance(_RecordingSink(), Sink)


def test_pipeline_broadcasts_to_every_sink_in_declaration_order() -> None:
    a = _RecordingSink("a")
    b = _RecordingSink("b")
    pipe = SinkPipeline([a, b])

    bar = _bar()
    pipe.on_bar(bar)

    assert a.bars == [bar]
    assert list(pipe) == [a, b]
    assert len(pipe) == 2


def test_pipeline_isolates_per_sink_exception(caplog) -> None:
    """One sink raising must not stop later sinks from receiving the event."""

    class _Boom(BaseSink):
        name = "boom"

        def on_bar(self, bar: Bar) -> None:
            raise RuntimeError("nope")

    boom = _Boom()
    after = _RecordingSink("after")
    pipe = SinkPipeline([boom, after])

    with caplog.at_level(logging.ERROR):
        pipe.on_bar(_bar())

    assert len(after.bars) == 1
    msgs = [r.message for r in caplog.records]
    assert any("sink_on_bar_failed" in m for m in msgs)


def test_pipeline_flush_only_when_requested() -> None:
    a = _RecordingSink("a")
    b = _RecordingSink("b")
    pipe = SinkPipeline([a, b])

    # Nobody wants flush yet.
    assert pipe.has_pending_flush() is False
    pipe.flush_pending()
    assert a.flush_calls == 0 and b.flush_calls == 0

    # b raises its hand.
    b._wants_flush = True
    assert pipe.has_pending_flush() is True
    pipe.flush_pending()
    assert a.flush_calls == 0
    assert b.flush_calls == 1
    # b cleared its flag inside flush(), so pipeline now reports clean.
    assert pipe.has_pending_flush() is False


def test_pipeline_close_is_idempotent_and_swallows_errors(caplog) -> None:
    class _BadClose(BaseSink):
        name = "bad_close"
        closed = False

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close-boom")

    bad = _BadClose()
    good = _RecordingSink("good")
    pipe = SinkPipeline([bad, good])

    with caplog.at_level(logging.ERROR):
        pipe.close()
        pipe.close()  # idempotent — second call is a no-op

    assert bad.closed is True
    assert good.close_calls == 1  # not called twice
    assert any("sink_close_failed" in r.message for r in caplog.records)


def test_pipeline_get_finds_sink_by_name() -> None:
    a = _RecordingSink("first")
    b = _RecordingSink("second")
    pipe = SinkPipeline([a, b])

    assert pipe.get("first") is a
    assert pipe.get("second") is b
    assert pipe.get("missing") is None


def test_should_flush_exception_is_isolated(caplog) -> None:
    """A sink raising in should_flush() does not crash the loop."""

    class _BoomShouldFlush(BaseSink):
        name = "boom_sf"

        def should_flush(self) -> bool:
            raise RuntimeError("sf-boom")

    a = _BoomShouldFlush()
    b = _RecordingSink("b")
    b._wants_flush = True
    pipe = SinkPipeline([a, b])

    with caplog.at_level(logging.ERROR):
        # b still reports True → has_pending_flush returns True overall.
        assert pipe.has_pending_flush() is True
    assert any("sink_should_flush_failed" in r.message for r in caplog.records)


def test_empty_pipeline_is_safe_no_op() -> None:
    pipe = SinkPipeline()
    pipe.on_bar(_bar())
    assert pipe.has_pending_flush() is False
    pipe.flush_pending()
    pipe.close()


def test_pipeline_logs_include_sink_name_and_symbol(caplog) -> None:
    class _Boom(BaseSink):
        name = "named_boom"

        def on_bar(self, bar: Bar) -> None:
            raise RuntimeError("nope")

    pipe = SinkPipeline([_Boom()])
    with caplog.at_level(logging.ERROR):
        pipe.on_bar(_bar("AAPL"))

    rec = next(r for r in caplog.records if "sink_on" in r.message)
    assert getattr(rec, "sink", None) == "named_boom"
    assert getattr(rec, "symbol", None) == "AAPL"
