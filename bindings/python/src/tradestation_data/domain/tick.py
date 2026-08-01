from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# TradeStation US-equity feeds are always America/New_York; downstream
# session logic keys off ET wall-clock, so we expose an ET-aware view of
# ``timestamp`` as the primary time basis.
_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Tick:
    """
    A single trade print, exactly as the publisher sent it.

    `timestamp` is UTC — the DLL's receive-side clock, which
    contract/semantics.md §1 makes authoritative for a tick. `timestamp_et`
    is the same instant in America/New_York and is the basis for session-time
    decisions downstream.

    `bid` and `ask` are None when there is no quote: the wire says null
    during historical replay and for symbols that carry no quote at all, and
    the binding additionally blanks index/breadth symbols whose live numbers
    mean nothing (contract/semantics.md §3).

    THE FIVE ``el_*`` FIELDS ARE EASYLANGUAGE'S RESERVED WORDS, VERBATIM.
    Nothing between the chart and this dataclass selects, converts or
    reconciles them, and this binding must not either. That matters most for
    the first two, which swap meaning by chart type: on an intraday series
    ``el_volume`` is the UP-TICK share volume alone and ``el_ticks`` is the
    total, while on daily they are total shares and a trade count
    respectively. A consumer wanting "how much traded" reads the field the
    table in contract/semantics.md §3.4 names for its timeframe.

    The ``el_`` prefix is deliberate: it is there so a reader looks the word
    up instead of assuming, because ``volume`` on an intraday bar is not the
    volume. Reading it as such is a real bug this repo shipped, and the
    resulting numbers were systematically about half of what traded while
    looking entirely plausible.

    All five are 0 for index/breadth symbols, which have no traded volume at
    all. ``el_open_interest`` is 0 for stocks and ETFs generally.
    """

    symbol: str
    timestamp: datetime
    price: float
    el_volume: int
    el_ticks: int
    el_upticks: int
    el_downticks: int
    el_open_interest: int
    bid: float | None
    ask: float | None

    @property
    def timestamp_et(self) -> datetime:
        """Return ``timestamp`` converted to America/New_York."""
        return self.timestamp.astimezone(_ET_TZ)
