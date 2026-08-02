from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# TradeStation US-equity charts are always America/New_York; downstream
# analytics keys off session wall-clock (09:30-16:00 ET), so we expose an
# ET-aware view of ``bar_time`` as the primary time basis.
_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Bar:
    """One data point on a TradeStation chart, exactly as it was published.

    There is one type here, not a Tick and a Bar. TradeStation supplies the
    same reserved words for a point on any chart — a 1-tick series has
    Open = High = Low = Close, and a minute chart still answers
    InsideBid/InsideAsk — so splitting them meant dropping fields the chart
    had already provided, on a judgement about which numbers were meaningful
    where. That judgement belongs to the consumer.

    Nothing in this binding computes a point. No aggregation, no rollup from a
    finer interval, no bucket grid. A consumer wanting 5-minute bars either
    charts them in TradeStation or builds them from what is stored here.

    ``bar_time`` is the publisher's own timestamp, converted to UTC and
    floored to the minute — nothing else. EasyLanguage's ``Time`` is the
    point's CLOSE, so ``bar_time`` is a close time. It is NOT a left edge and
    is not snapped to any grid: this binding used to do both, and it cost a
    whole bar a day on a 60-minute chart, because TradeStation restarts its
    intraday grid at the RTH open and close and two published points could
    then land on one slot. See contract/semantics.md §2.

    ``bar_type`` / ``bar_interval`` / ``category`` are EasyLanguage's
    ``BarType``, ``BarInterval`` and ``Category``, verbatim. Nothing maps them
    to a timeframe name and nothing refuses a combination it does not
    recognise; the storage layer partitions on the raw pair. ``category`` says
    what the symbol is (2 = Stock, 0 = Future, 4 = Index, …) and is what makes
    contract/semantics.md §3.4 answerable at all — the meaning of the five
    ``el_*`` words depends on it.

    The five ``el_*`` fields are EasyLanguage's reserved words verbatim. The
    prefix is the point: ``el_volume`` is not "the volume" on an intraday
    chart, and a column called plain ``volume`` invites exactly the misreading
    that once cost this repo a systematically halved volume column.

    ``bid`` / ``ask`` are ``InsideBid`` / ``InsideAsk``, or None where the
    publisher had no quote to report. They travel on every point, bars
    included.
    """

    symbol: str
    bar_time: datetime
    bar_type: int
    bar_interval: int
    category: int
    open: float
    high: float
    low: float
    close: float
    el_volume: int
    el_ticks: int
    el_upticks: int
    el_downticks: int
    el_open_interest: int
    bid: float | None = None
    ask: float | None = None

    @property
    def bar_time_et(self) -> datetime:
        """Return ``bar_time`` converted to America/New_York."""
        return self.bar_time.astimezone(_ET_TZ)
