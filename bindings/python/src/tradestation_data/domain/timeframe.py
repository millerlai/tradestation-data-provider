"""The timeframe vocabulary, and the bucket grid every layer must agree on.

`tf` on the wire and `timeframe=` in the Parquet layout are deliberately the
same set of strings: a value one layer can name and another cannot is a bar
filed under the wrong interval, and nothing downstream can detect that. The
enum lives here so adding a member is one edit rather than several — the wire
allow-list, the minutes table and the storage layout are all derived from it.

Bucket alignment is contract/semantics.md §2.2, not a local choice.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"


TIMEFRAME_MINUTES: dict[str, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.D1: 60 * 24,
}

# What a binding will accept in the wire's `tf` field. A frame naming an
# interval we cannot place must be refused, not filed under a default.
SUPPORTED_TIMEFRAMES: frozenset[str] = frozenset(tf.value for tf in Timeframe)

# Intervals coarse enough that one file per calendar day would hold a row or
# two. A closed Parquet file costs ~2.9 KB of schema and footer whatever it
# holds, so a `1d` day partition spent 2,903 bytes carrying about 60 of data.
# These live in a single file per symbol, rewritten whole on each flush.
SINGLE_FILE_TIMEFRAMES: frozenset[str] = frozenset({Timeframe.D1})

# Intervals whose bucket is decided by the session anchor rather than by the
# publisher's timestamp: `align_bucket_start` discards the time-of-day and
# returns that session's 04:00 ET edge. A publisher's right-hand bar label
# therefore needs no shift onto its left edge for these — and must not get
# one, since subtracting a whole interval would land the bar in the previous
# session. See contract/semantics.md §2.2.
SESSION_ANCHORED_TIMEFRAMES: frozenset[str] = frozenset({Timeframe.D1})

_ET_TZ: ZoneInfo = ZoneInfo("America/New_York")

# 09:30 ET on 2000-01-03, expressed in UTC. Any date works; the time-of-day
# is what sets the grid.
#
# Intraday grids are laid out from this fixed *UTC* origin rather than from
# an ET wall-clock anchor. 1m/5m/15m/30m/1h all divide the one-hour DST shift
# evenly, so a UTC grid still lands on 09:30 ET in both EST and EDT — while
# staying unambiguous inside the DST fold. Bucketing in local time does not:
# on a fall-back day 01:15 EDT and 01:15 EST are an hour apart yet read the
# same on the wall clock, so both collapse into one bucket and the resulting
# "1h" bar silently spans two real hours.
_INTRADAY_ORIGIN_UTC: datetime = datetime(2000, 1, 3, 14, 30, tzinfo=UTC)

# 04:00 ET, matching aggregation.session.PRE_SESSION_CUTOFF_LOCAL: the
# extended session runs 04:00 -> 20:00 ET and anything earlier belongs to the
# previous session date. A daily bar must use the same boundary, or the
# session logic and the daily rollup disagree about which day a bar is in.
#
# Unlike the intraday grid, daily has to be laid out on the ET clock: a
# calendar day is 23 or 25 hours twice a year. That is safe here because
# 04:00 ET sits outside the 01:00-02:00 fold, so the edge converts back to
# UTC unambiguously.
_DAILY_ANCHOR_LOCAL: time = time(4, 0)


def timeframe_to_minutes(timeframe: str | Timeframe) -> int:
    tf = str(timeframe)
    if tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Valid: {list(TIMEFRAME_MINUTES)}")
    return TIMEFRAME_MINUTES[tf]


def align_bucket_start(ts_utc: datetime, timeframe: str | Timeframe) -> datetime:
    """Floor a UTC instant onto the bucket grid for `timeframe`.

    Note this does NOT apply the right-label-to-left-label shift of
    contract/semantics.md §2. That is the caller's job, because it is skipped
    for SESSION_ANCHORED_TIMEFRAMES: alignment there discards the time-of-day
    for the 04:00 ET anchor anyway, and subtracting an interval first would
    move the bar into the previous session.
    """
    tf = str(timeframe)
    if tf == Timeframe.D1:
        local = ts_utc.astimezone(_ET_TZ)
        day = local.date()
        if local.time() < _DAILY_ANCHOR_LOCAL:
            day -= timedelta(days=1)
        return datetime.combine(day, _DAILY_ANCHOR_LOCAL, tzinfo=_ET_TZ).astimezone(UTC)

    step = timedelta(minutes=timeframe_to_minutes(tf))
    return _INTRADAY_ORIGIN_UTC + (ts_utc - _INTRADAY_ORIGIN_UTC) // step * step
