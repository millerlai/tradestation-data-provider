"""In-memory sink — append every event to a list.

Use cases:
  * unit tests that need to assert on emitted ticks/bars
  * notebook exploration ("run for 30s, give me the bars")

Not suitable for long-running production: unbounded memory growth.
Use :class:`tradestation_data.sinks.callback.CallbackSink` when the
goal is to *consume* events rather than retain them.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.base import BaseSink


class InMemorySink(BaseSink):
    """Buffer every tick and bar in memory.

    ``max_per_symbol`` caps each per-symbol deque (and the global
    ``all_*`` deques) so a long run does not OOM the process — when
    the cap is reached, the oldest entry is dropped. ``None`` means
    unbounded (the test/notebook default).
    """

    def __init__(
        self,
        *,
        name: str,
        max_per_symbol: int | None = None,
    ) -> None:
        self.name = name
        self._max = max_per_symbol
        self._ticks_by_symbol: dict[str, deque[Tick]] = defaultdict(self._mkdeque)
        self._bars_by_symbol: dict[str, deque[Bar]] = defaultdict(self._mkdeque)
        # Cross-symbol views for "give me everything" queries.
        self._all_ticks: deque[Tick] = self._mkdeque()
        self._all_bars: deque[Bar] = self._mkdeque()

    def _mkdeque(self) -> deque:  # type: ignore[type-arg]
        return deque(maxlen=self._max)

    # ---- Sink protocol ---------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        self._ticks_by_symbol[tick.symbol].append(tick)
        self._all_ticks.append(tick)

    def on_bar(self, bar: Bar) -> None:
        self._bars_by_symbol[bar.symbol].append(bar)
        self._all_bars.append(bar)

    # ---- query API -------------------------------------------------------

    def ticks(self, symbol: str | None = None) -> list[Tick]:
        """Return buffered ticks for ``symbol`` (or all symbols if None)."""
        if symbol is None:
            return list(self._all_ticks)
        return list(self._ticks_by_symbol.get(symbol, ()))

    def bars(self, symbol: str | None = None) -> list[Bar]:
        if symbol is None:
            return list(self._all_bars)
        return list(self._bars_by_symbol.get(symbol, ()))

    def symbols(self) -> Iterable[str]:
        return set(self._ticks_by_symbol) | set(self._bars_by_symbol)

    def clear(self) -> None:
        self._ticks_by_symbol.clear()
        self._bars_by_symbol.clear()
        self._all_ticks.clear()
        self._all_bars.clear()
