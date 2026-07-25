"""Sink fixture classes used by test_sinks_registry.

Exposed as a top-level module via ``tests/conftest.py`` (it injects
this file into ``sys.modules`` under the name ``_sink_fixtures``) so
the registry's ``module:attr`` target strings can locate it without
``tests/`` being on ``pythonpath``.
"""

from __future__ import annotations

from tradestation_data.domain.bar import Bar
from tradestation_data.sinks.base import BaseSink, Sink


class FakeSink(BaseSink):
    def __init__(self, *, name: str, label: str = "default") -> None:
        self.name = name
        self.label = label
        self.bars: list[Bar] = []

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)


def fake_factory(*, name: str, label: str = "factory") -> Sink:
    return FakeSink(name=name, label=label)


# Module attribute that exists but is not callable — exercised by
# test_instantiate_sink_errors_when_target_not_callable.
NOT_CALLABLE = 42


class _NotASink:
    """Has no on_tick/on_bar/close — fails Sink runtime_checkable."""


def make_non_sink(*, name: str) -> object:
    return _NotASink()
