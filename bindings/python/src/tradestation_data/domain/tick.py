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
    A single market data point as received from a provider.

    See docs/design.md §4.

    `timestamp` is UTC. `timestamp_et` is the same instant expressed in
    America/New_York and is the authoritative basis for session-time
    decisions downstream. `bid` and `ask` are None for index/breadth
    symbols ($TICK, VXX, etc.) that have no quote. `volume` is 0 for
    the same reason.
    """

    symbol: str
    timestamp: datetime
    price: float
    volume: int
    bid: float | None
    ask: float | None
    tick_count: int
    source: str
    # Which publisher convention produced `volume` — wire v4's `pv`. None
    # when the wire did not say (v1/v2/v3), which is not the same as 0:
    # 0 is a publisher that declared itself undeclared, None is a wire that
    # had no field to declare with. Either way `volume` may be up-tick share
    # volume rather than the total. contract/v4/envelope.md.
    publisher_version: int | None = None

    @property
    def timestamp_et(self) -> datetime:
        """Return ``timestamp`` converted to America/New_York."""
        return self.timestamp.astimezone(_ET_TZ)
