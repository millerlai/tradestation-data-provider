"""The bucket grid — contract/semantics.md §2.2.

There used to be two implementations of this grid — a DuckDB expression on
the resampler's cache-miss path and the Python one here — and most of this
file existed to assert they agreed, because a disagreement raises nothing
and simply makes every `bucket_start` join match nothing.

Nothing resamples any more, so `align_bucket_start` is the only
implementation and there is no second opinion to check it against. What is
left are the grid's own invariants, which is what the cross-check was really
protecting: a label never later than what it labels, a fold that does not
merge two different hours, and the session open landing on an edge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradestation_data.domain.timeframe import Timeframe, align_bucket_start

# 2025-11-02 falls back (02:00 EDT -> 01:00 EST); 2025-03-09 springs forward.
_DST_DAYS = (datetime(2025, 11, 2, tzinfo=UTC), datetime(2025, 3, 9, tzinfo=UTC))


@pytest.mark.parametrize("timeframe", [str(tf) for tf in Timeframe])
def test_aligning_an_aligned_instant_is_a_no_op(timeframe: str) -> None:
    """Idempotence, which is what "lands on the grid" means operationally.

    Without a second implementation to diff against, this is the property
    that catches a grid drifting: if align(t) were not itself a grid edge,
    two bars covering the same span could get different labels depending on
    which instant inside them happened to be aligned first.
    """
    for base in _DST_DAYS:
        for i in range(0, 24 * 4, 3):  # every 45 min through the transition day
            ts = base + timedelta(minutes=15 * i)
            once = align_bucket_start(ts, timeframe)
            assert align_bucket_start(once, timeframe) == once, f"{timeframe} @ {ts.isoformat()}"


@pytest.mark.parametrize("timeframe", [str(tf) for tf in Timeframe])
def test_bucket_start_is_never_later_than_the_instant_it_labels(timeframe: str) -> None:
    """`bucket_start` covers [t, t+step) — a label after its own input breaks that.

    Bucketing on the ET wall clock used to do exactly this on a spring-forward
    day: 03:15 EDT came back labelled 03:30.
    """
    for base in _DST_DAYS:
        for i in range(24 * 4):
            ts = base + timedelta(minutes=15 * i)
            assert align_bucket_start(ts, timeframe) <= ts


def test_dst_fold_does_not_merge_two_different_hours() -> None:
    """01:15 EDT and 01:15 EST are an hour apart, not the same bucket.

    They read identically on the ET wall clock, so a locally-bucketed grid
    collapses them and emits one "1h" bar silently spanning two real hours.
    """
    edt = datetime(2025, 11, 2, 5, 15, tzinfo=UTC)  # 01:15 EDT
    est = datetime(2025, 11, 2, 6, 15, tzinfo=UTC)  # 01:15 EST
    assert align_bucket_start(edt, "1h") != align_bucket_start(est, "1h")


@pytest.mark.parametrize(
    "session_open",
    [
        datetime(2026, 1, 5, 14, 30, tzinfo=UTC),  # 09:30 EST
        datetime(2026, 4, 18, 13, 30, tzinfo=UTC),  # 09:30 EDT
    ],
)
def test_session_open_is_a_grid_edge_on_both_sides_of_dst(session_open: datetime) -> None:
    """The point of anchoring to 09:30 instead of the epoch.

    Epoch-aligned hours would start the RTH day at 09:00 ET, making the first
    bar cover half an hour while wearing a whole hour's timestamp.
    """
    for timeframe in ("1m", "5m", "15m", "30m", "1h"):
        assert align_bucket_start(session_open, timeframe) == session_open


def test_daily_bars_anchor_at_04_00_et_not_utc_midnight() -> None:
    """Session date boundary, matching aggregation.session.session_date_of."""
    # 20:30 ET on 2026-04-20 — after UTC midnight, still the same session.
    late = datetime(2026, 4, 21, 0, 30, tzinfo=UTC)
    assert align_bucket_start(late, "1d") == datetime(2026, 4, 20, 8, 0, tzinfo=UTC)  # 04:00 EDT
    # 03:30 ET belongs to the *previous* session.
    early = datetime(2026, 4, 21, 7, 30, tzinfo=UTC)
    assert align_bucket_start(early, "1d") == datetime(2026, 4, 20, 8, 0, tzinfo=UTC)
    # 04:30 ET opens the new one.
    opening = datetime(2026, 4, 21, 8, 30, tzinfo=UTC)
    assert align_bucket_start(opening, "1d") == datetime(2026, 4, 21, 8, 0, tzinfo=UTC)


def test_unknown_timeframe_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError):
        align_bucket_start(datetime(2026, 4, 20, tzinfo=UTC), "4h")
