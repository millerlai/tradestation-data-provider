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
    An OHLC bar, exactly as TradeStation published it.

    Every bar reaching this type came off the wire. Nothing in this binding
    computes one — no tick aggregation, no rollup from a finer interval — so
    there is no provenance to record and no derived-versus-native distinction
    to defend. A consumer that wants 5-minute bars either charts them in
    TradeStation, or builds them itself from what is stored here.

    `bucket_start` is UTC and LEFT-labelled: the bar covers
    ``[bucket_start, bucket_start + timeframe)``. EasyLanguage stamps a bar
    with its CLOSE time, so the wire is right-labelled and the subscriber
    steps back one interval before aligning — see contract/semantics.md §2.
    `bucket_start_et` is the same instant in America/New_York.

    `timeframe` names the interval ("1m", "5m", ...). It is not a cosmetic
    label: the storage layer partitions on it, so a bar whose timeframe is
    wrong is filed under the wrong interval where nothing downstream can tell.
    Per-interval bucket alignment is contract/semantics.md §2.2.

    The five ``el_*`` fields are EasyLanguage's reserved words verbatim; see
    Tick for why they are prefixed and why ``el_volume`` is not the volume on
    an intraday bar.

    No bid/ask. A live-quote function describes the moment of the call, which
    on a bar is its last print rather than the bar, so the wire does not carry
    one to model.
    """

    symbol: str
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    el_volume: int
    el_ticks: int
    el_upticks: int
    el_downticks: int
    el_open_interest: int
    timeframe: str = "1m"

    @property
    def bucket_start_et(self) -> datetime:
        """Return ``bucket_start`` converted to America/New_York."""
        return self.bucket_start.astimezone(_ET_TZ)
