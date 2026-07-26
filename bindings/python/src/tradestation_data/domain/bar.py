from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# TradeStation US-equity charts are always America/New_York; downstream
# analytics keys off session wall-clock (09:30-16:00 ET), so we expose an
# ET-aware view of ``bucket_start`` as the primary time basis.
_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")

# ---- provenance ----------------------------------------------------------
#
# A bar that arrived over the wire and a bar we computed are not
# interchangeable, and they used to be indistinguishable on disk: both
# carried source="tradestation_el", because resampling copied the field
# through with first(source). A derived rollup could then overwrite a native
# bar with nothing to show it had happened.
#
# Daily is where this matters most: TradeStation's daily bar carries the
# exchange's official OHLC and is split/dividend adjusted, neither of which
# can be reconstructed by summing ticks. A derived 1d bar is an approximation
# wearing the same shape. See contract/semantics.md §2.3.
SOURCE_DERIVED_PREFIX = "derived:"


def derived_source(origin: str) -> str:
    """Provenance marker for a computed bar, e.g. ``derived:ticks``."""
    return f"{SOURCE_DERIVED_PREFIX}{origin}"


def is_derived(source: str) -> bool:
    return source.startswith(SOURCE_DERIVED_PREFIX)


@dataclass(frozen=True, slots=True)
class Bar:
    """
    An OHLCV bar.

    `bucket_start` is UTC, aligned to the minute (second=0, microsecond=0).
    `bucket_start_et` is the same instant expressed in America/New_York and
    is the authoritative basis for session-time decisions downstream.
    `source` carries provenance: the original provider's source_id for a bar
    that arrived over the wire, or ``derived:<origin>`` for one this binding
    computed (``derived:ticks``, ``derived:1m``, ``derived:empty`` for a
    wall-clock-emitted empty bar). contract/semantics.md §2.3 makes the
    distinction a contract rule — derived data must never overwrite native.

    `timeframe` names the interval this bar covers ("1m", "5m", ... ). It
    defaults to "1m" because that is what the tick aggregator produces and
    what wire v2 could express; a bar decoded from wire v3 carries whatever
    the publisher said. It is not a cosmetic label — the storage layer
    partitions on it, so a bar whose timeframe is wrong is filed under the
    wrong interval and silently corrupts anything derived from that
    partition. Bucket alignment per interval is contract/semantics.md §2.2.
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
    timeframe: str = "1m"

    @property
    def bucket_start_et(self) -> datetime:
        """Return ``bucket_start`` converted to America/New_York."""
        return self.bucket_start.astimezone(_ET_TZ)
