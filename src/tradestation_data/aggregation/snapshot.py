from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta

from tradestation_data.aggregation.session import (
    SessionPolicy,
    session_date_of,
    session_start_utc,
)
from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick

DEFAULT_RECENT_BARS: int = 100


@dataclass(slots=True)
class SymbolState:
    """
    In-memory snapshot of a symbol's recent market state.

    - `last_tick`        : most recent Tick received (any source)
    - `last_closed_bar`  : most recent 1-min bar closed by BarAggregator
    - `recent_bars`      : bounded deque of recent closed bars
                           (oldest→newest), filtered per SessionPolicy
    - `session_date`     : NY trading date the current buffer belongs to
    - `session_open_bar` : the first bar at/after 09:30 ET for
                           `session_date`, populated once crossed.
                           May remain None if the session opened while
                           the symbol was offline / we only have pre-
                           market data.
    """

    symbol: str
    last_tick: Tick | None = None
    last_closed_bar: Bar | None = None
    recent_bars: deque[Bar] = field(default_factory=deque)
    session_date: date | None = None
    session_open_bar: Bar | None = None


@dataclass(frozen=True, slots=True)
class SymbolView:
    """
    Immutable point-in-time view of a symbol's state.

    Returned by `MarketSnapshot.view_of()` — SubAgents spanning
    `await` points should consume views instead of `SymbolState`
    to avoid mutation races with concurrent ingestion updates.
    """

    symbol: str
    last_tick: Tick | None
    last_closed_bar: Bar | None
    recent_bars: tuple[Bar, ...]
    session_date: date | None = None
    session_open_bar: Bar | None = None


class MarketSnapshot:
    """
    Live in-memory view of every symbol the ingestion runtime has seen.

    Agents read from this; ingestion writes to this. See docs/design.md
    §3.3 and §3.9 — SubAgents pull current state instead of maintaining
    their own independent buffers.

    Session handling: per-symbol `SessionPolicy` controls how
    `recent_bars` behaves across the 09:30 ET boundary. Breadth indices
    reset each session; continuous symbols keep up to
    `pre_market_window_minutes` of history ahead of 09:30.

    Threading model: runtime is asyncio single-threaded, so plain
    `state_of()` is safe within a synchronous critical section. For
    coroutines that span `await` points (agents fanning out via
    `asyncio.gather`), call `view_of()` / `views()` to snapshot an
    immutable copy first.
    """

    def __init__(
        self,
        max_bars_per_symbol: int = DEFAULT_RECENT_BARS,
        *,
        symbol_policies: dict[str, SessionPolicy] | None = None,
        default_policy: SessionPolicy | None = None,
    ) -> None:
        self._max_bars = max_bars_per_symbol
        self._states: dict[str, SymbolState] = {}
        self._policies: dict[str, SessionPolicy] = dict(symbol_policies or {})
        # No policy at all → legacy behaviour (pure bar-count window).
        self._default_policy = default_policy

    def on_tick(self, tick: Tick) -> None:
        self._ensure_state(tick.symbol).last_tick = tick

    def on_bar(self, bar: Bar) -> None:
        st = self._ensure_state(bar.symbol)
        policy = self._policy_for(bar.symbol)

        bar_session = session_date_of(bar.bucket_start)

        if policy is not None:
            if (
                policy.session_reset
                and st.session_date is not None
                and bar_session != st.session_date
            ):
                st.recent_bars.clear()
                st.session_open_bar = None

            if not policy.session_reset and policy.pre_market_window_minutes is not None:
                self._evict_before_premarket_window(
                    st, bar_session, policy.pre_market_window_minutes
                )

        st.session_date = bar_session
        # Capture the first regular-session bar we see for this date.
        if st.session_open_bar is None and bar.bucket_start >= session_start_utc(bar_session):
            st.session_open_bar = bar
        elif (
            st.session_open_bar is not None
            and st.session_open_bar.bucket_start > bar.bucket_start
            and bar.bucket_start >= session_start_utc(bar_session)
        ):
            # Out-of-order ingest that precedes the currently recorded
            # open — keep the earliest.
            st.session_open_bar = bar

        st.last_closed_bar = bar
        st.recent_bars.append(bar)

    def state_of(self, symbol: str) -> SymbolState | None:
        return self._states.get(symbol)

    def view_of(self, symbol: str) -> SymbolView | None:
        """Return an immutable snapshot of `symbol`'s state, or None."""
        st = self._states.get(symbol)
        if st is None:
            return None
        return SymbolView(
            symbol=symbol,
            last_tick=st.last_tick,
            last_closed_bar=st.last_closed_bar,
            recent_bars=tuple(st.recent_bars),
            session_date=st.session_date,
            session_open_bar=st.session_open_bar,
        )

    def views(self, symbols: list[str] | None = None) -> dict[str, SymbolView]:
        """Batch-snapshot: immutable views for every known (or requested) symbol."""
        keys = symbols if symbols is not None else list(self._states.keys())
        out: dict[str, SymbolView] = {}
        for sym in keys:
            v = self.view_of(sym)
            if v is not None:
                out[sym] = v
        return out

    def symbols(self) -> list[str]:
        return list(self._states.keys())

    def _ensure_state(self, symbol: str) -> SymbolState:
        st = self._states.get(symbol)
        if st is None:
            st = SymbolState(symbol=symbol, recent_bars=deque(maxlen=self._max_bars))
            self._states[symbol] = st
        return st

    def _policy_for(self, symbol: str) -> SessionPolicy | None:
        return self._policies.get(symbol, self._default_policy)

    @staticmethod
    def _evict_before_premarket_window(
        st: SymbolState,
        bar_session: date,
        pre_market_minutes: int,
    ) -> None:
        cutoff = session_start_utc(bar_session) - timedelta(minutes=pre_market_minutes)
        while st.recent_bars and st.recent_bars[0].bucket_start < cutoff:
            st.recent_bars.popleft()
