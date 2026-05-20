"""Session boundary logic for US equity markets.

Bars from TradeStation arrive in UTC, but the *trading session* is a
human concept anchored to America/New_York 09:30 open. Some symbols
(breadth indices like $ADD, $TICK) reset at the session open and are
meaningless if you carry yesterday's bars into today's window.
Continuous symbols (SPY, VXX, mega caps) have meaningful pre-market
bars but should still drop data older than a configurable pre-open
window.

This module is the single source of truth for:
  - what trading date a given UTC timestamp belongs to
  - when that session opens (as UTC)
  - per-symbol retention policy (session_reset, pre_market_window)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SESSION_OPEN_LOCAL = time(9, 30)
SESSION_CLOSE_LOCAL = time(16, 0)

# Bars with bucket_start before 04:00 ET are treated as belonging to the
# *previous* session — TradeStation's extended session runs 04:00→20:00 ET.
# We never receive overnight futures data, so this line keeps pre-market
# bars (04:00→09:30) attached to the upcoming regular session.
PRE_SESSION_CUTOFF_LOCAL = time(4, 0)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Per-symbol retention rule consumed by MarketSnapshot."""

    session_reset: bool
    pre_market_window_minutes: int | None  # None = unlimited pre-market

    @staticmethod
    def for_category(category: str) -> SessionPolicy:
        """Default policy derived from symbols.yaml `category`.

        Breadth indices reset daily. Everything else keeps up to 60 min
        of pre-market history — enough for gap/opening-range context
        without letting yesterday's prints pollute today's window.
        """
        if category == "breadth":
            return SessionPolicy(session_reset=True, pre_market_window_minutes=None)
        return SessionPolicy(session_reset=False, pre_market_window_minutes=60)


def session_date_of(ts: datetime) -> date:
    """Return the NY trading date for a UTC (or tz-aware) timestamp.

    Bars between 04:00 ET today and 04:00 ET tomorrow map to today's
    session date. Before 04:00 ET is bucketed into the *previous*
    session so stray overnight ticks don't open a new one.
    """
    local = ts.astimezone(NY)
    if local.time() < PRE_SESSION_CUTOFF_LOCAL:
        return (local - timedelta(days=1)).date()
    return local.date()


def session_start_utc(session_date: date) -> datetime:
    """Regular-session open (09:30 ET) for `session_date`, as UTC."""
    local = datetime.combine(session_date, SESSION_OPEN_LOCAL, tzinfo=NY)
    return local.astimezone(ZoneInfo("UTC"))
