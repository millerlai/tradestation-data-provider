"""Session boundary logic for US equity markets.

Bars from TradeStation arrive in UTC, but the *trading session* is a
human concept anchored to America/New_York 09:30 open. Some symbols
(breadth indices like $ADD, $TICK) reset at the session open and are
meaningless if you carry yesterday's bars into today's window.
Continuous symbols (SPY, VXX, mega caps) have meaningful pre-market
bars but should still drop data older than a configurable pre-open
window.

**Two different boundaries live here and they are not interchangeable:**

  - 04:00 ET (`PRE_SESSION_CUTOFF_LOCAL`) decides which trading date a
    bar belongs to. It is a *labelling* rule.
  - 09:30 ET (`SESSION_OPEN_LOCAL`) is the regular-session open. It is
    what `session_open_bar` and the pre-market window measure against.

Conflating them is a real bug this module already shipped once: a
breadth reset keyed on the session date alone fires at 04:00, which
drops yesterday's bars but leaves every one of today's pre-market bars
in place — so the symbols whose pre-market data is meaningless retained
more of it than SPY did. See MarketSnapshot.on_bar.

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

# Bars with bar_time before 04:00 ET are treated as belonging to the
# *previous* session — TradeStation's extended session runs 04:00→20:00 ET.
# We never receive overnight futures data, so this line keeps pre-market
# bars (04:00→09:30) attached to the upcoming regular session.
PRE_SESSION_CUTOFF_LOCAL = time(4, 0)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Per-symbol retention rule consumed by MarketSnapshot.

    The two fields drive two different clears, and a `session_reset` symbol
    gets BOTH:

    - ``session_reset`` — drop yesterday's bars when the session date rolls
      over (04:00 ET, see `session_date_of`), AND drop this session's own
      pre-market bars when the first regular-session bar arrives. The second
      clear is what makes "no pre-market" true; the rollover alone cannot do
      it, because pre-market bars already carry today's session date.
    - ``pre_market_window_minutes`` — a rolling window for symbols that keep
      pre-market history. ``None`` means unlimited. **Not consulted when
      ``session_reset`` is True**, since nothing pre-market survives the open
      for those symbols anyway.
    """

    session_reset: bool
    pre_market_window_minutes: int | None  # None = unlimited pre-market

    @staticmethod
    def for_category(category: str) -> SessionPolicy:
        """Default policy derived from symbols.yaml `category`.

        Breadth indices reset daily and start the regular session empty —
        a pre-market $TICK reading is not comparable to an RTH one, so
        carrying it across the open would corrupt any range or extreme
        computed over the session. Everything else keeps up to 60 min of
        pre-market history — enough for gap/opening-range context without
        letting yesterday's prints pollute today's window.

        Keying on the category rather than a symbol list is deliberate:
        $TICK / $ADD / $VOLD / $TRIN / $PCVA all share one rule, and a new
        breadth symbol inherits it from symbols.yaml with no code change.
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
