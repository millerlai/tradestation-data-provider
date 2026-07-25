from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# TradeStation US-equity charts are always America/New_York; downstream
# analytics keys off session wall-clock (09:30-16:00 ET), so we expose an
# ET-aware view of ``bucket_start`` as the primary time basis.
_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Bar:
    """
    A 1-minute OHLCV bar aggregated from ticks.

    See docs/design.md §3.6.4 for aggregation rules.

    `bucket_start` is UTC, aligned to the minute (second=0, microsecond=0).
    `bucket_start_et` is the same instant expressed in America/New_York and
    is the authoritative basis for session-time decisions downstream.
    `source` is either the original provider's source_id or "empty" for
    wall-clock-emitted empty bars.
    """

    symbol: str
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int
    source: str

    @property
    def bucket_start_et(self) -> datetime:
        """Return ``bucket_start`` converted to America/New_York."""
        return self.bucket_start.astimezone(_ET_TZ)
