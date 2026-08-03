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

DEFAULT_RECENT_BARS: int = 100


@dataclass(slots=True)
class SymbolState:
    """
    In-memory snapshot of a symbol's recent market state.

    - `last_closed_bar`  : most recent closed bar the publisher shipped
    - `recent_bars`      : bounded deque of recent closed bars
                           (oldest→newest), filtered per SessionPolicy
    - `session_date`     : NY trading date the current buffer belongs to
    - `session_open_bar` : the first regular-session point for
                           `session_date`, populated once crossed. Its
                           `bar_time` is a CLOSE, so the test is strictly
                           after 09:30 ET — a point closing exactly at
                           09:30 is the last pre-market one.
                           May remain None if the session opened while
                           the symbol was offline / we only have pre-
                           market data.
    """

    symbol: str
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
    `recent_bars` behaves across a session boundary. Breadth indices are
    cleared twice — once when the session date rolls over at 04:00 ET
    (yesterday's bars) and again when the first regular-session bar
    arrives (this session's pre-market bars), so they start RTH empty.
    Continuous symbols are never cleared but keep only
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

    def on_bar(self, bar: Bar) -> None:
        st = self._ensure_state(bar.symbol)
        policy = self._policy_for(bar.symbol)

        bar_session = session_date_of(bar.bar_time)
        session_open = session_start_utc(bar_session)

        if policy is not None:
            if (
                policy.session_reset
                and st.session_date is not None
                and bar_session != st.session_date
            ):
                st.recent_bars.clear()
                st.session_open_bar = None

            # A `session_reset` symbol keeps NO pre-market history once the
            # regular session starts — contract/semantics.md §4.1.
            #
            # This clear is separate from the one above and both are needed.
            # The rollover clear fires at the 04:00 ET session-date boundary
            # (`session_date_of`), which is what drops YESTERDAY's bars; it
            # cannot drop this session's own pre-market bars, because those
            # already carry today's session date. Keyed on the rollover alone,
            # a breadth symbol arrived at 09:31 still holding everything back
            # to 04:00 — and since the eviction path below is skipped for
            # these symbols, it retained MORE pre-market history than SPY,
            # inverting the reason the policy exists.
            #
            # `session_open_bar is None` is the "we have not crossed the open
            # yet this session" latch, so this fires exactly once per session,
            # on the first RTH bar. STRICTLY greater, matching the assignment
            # below: bar_time is a CLOSE, so a bar closing exactly at 09:30 is
            # the last PRE-market bar and goes with the rest.
            if policy.session_reset and st.session_open_bar is None and bar.bar_time > session_open:
                st.recent_bars.clear()

            if not policy.session_reset and policy.pre_market_window_minutes is not None:
                self._evict_before_premarket_window(
                    st, bar_session, policy.pre_market_window_minutes
                )

        st.session_date = bar_session
        # Capture the first regular-session bar we see for this date.
        # STRICTLY greater: bar_time is the bar's CLOSE, so the pre-market
        # bar ending exactly at 09:30 carries bar_time == session start and
        # must not win — the first RTH bar is the one closing after it.
        if st.session_open_bar is None and bar.bar_time > session_open:
            st.session_open_bar = bar
        elif (
            st.session_open_bar is not None
            and st.session_open_bar.bar_time > bar.bar_time
            and bar.bar_time > session_open
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
        while st.recent_bars and st.recent_bars[0].bar_time < cutoff:
            st.recent_bars.popleft()
