"""Coverage-record behaviour for the Tier-3 bar cache — contract/semantics.md §2.6.

Every test here is a failure that shipped, or nearly shipped, when completeness
was inferred from "does `date=<D>/bars.parquet` exist". The record exists so
that question has an answer its writer actually vouches for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar, derived_source
from tradestation_data.domain.tick import Tick
from tradestation_data.storage import HistoryStore, TickWriter
from tradestation_data.storage.bar_writer import BarWriter

OPEN = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)  # Mon 09:30 ET


def _tick(ts: datetime, price: float, symbol: str = "SPY") -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        volume=100,
        bid=None,
        ask=None,
        tick_count=1,
        source="tradestation_el",
    )


def _write_ticks(root: Path, ticks: list[Tick]) -> None:
    with TickWriter(root / "ticks") as w:
        for t in ticks:
            w.write(t)


def _minutes(start: datetime, n: int, base: float = 450.0, symbol: str = "SPY") -> list[Tick]:
    return [_tick(start + timedelta(minutes=i), base + i, symbol) for i in range(n)]


def _count_resamples(store: HistoryStore) -> dict[str, int]:
    calls = {"n": 0}
    original = store._resampler.resample

    def counted(*args: object, **kwargs: object) -> pl.DataFrame:
        calls["n"] += 1
        return original(*args, **kwargs)

    store._resampler.resample = counted  # type: ignore[method-assign]
    return calls


def _cache_dir(root: Path, tf: str = "5m", symbol: str = "SPY") -> Path:
    return root / "bars" / f"timeframe={tf}" / f"symbol={symbol}"


# ---- the live day ---------------------------------------------------------


def test_a_growing_day_is_rebuilt_rather_than_frozen(tmp_path: Path) -> None:
    """The failure §2.6 was written for.

    Query the morning of a live session and the day's partition is created
    holding only the morning. Ticks keep arriving. Inferring completeness from
    that file froze the day: the afternoon window answered 0 rows and a
    whole-day query kept returning the six morning bars.
    """
    _write_ticks(tmp_path, _minutes(OPEN, 30))
    store = HistoryStore(tmp_path)
    store.load_bars("SPY", OPEN, OPEN + timedelta(minutes=30), "5m")

    _write_ticks(tmp_path, _minutes(OPEN + timedelta(hours=2), 30, base=460.0))

    afternoon = store.load_bars("SPY", OPEN + timedelta(hours=2), OPEN + timedelta(hours=3), "5m")
    whole_day = store.load_bars("SPY", OPEN, OPEN + timedelta(hours=7), "5m")
    assert afternoon.height == 6
    assert whole_day.height == 12


def test_a_partly_cached_range_is_completed_not_truncated(tmp_path: Path) -> None:
    """Any single matching row used to count as a full hit.

    A multi-day query served whatever happened to be warm and dropped the rest
    — no error, no log line, just a short series. This is the defect the whole
    coverage record exists to close.
    """
    days = [OPEN + timedelta(days=i) for i in range(3)]
    _write_ticks(tmp_path, [t for d in days for t in _minutes(d, 10)])
    store = HistoryStore(tmp_path)

    store.load_bars("SPY", days[0], days[0] + timedelta(hours=1), "5m")  # warm day 1 only
    spanning = store.load_bars("SPY", days[0], days[2] + timedelta(hours=1), "5m")

    assert spanning["bucket_start_et"].dt.date().n_unique() == 3
    assert spanning.height == 6


def test_backfilled_ticks_are_picked_up_without_a_manual_rebuild(tmp_path: Path) -> None:
    """A day with no source is recorded as empty, not guessed about.

    The rejected design tried to tell a market holiday from an ingestion
    outage. They are identical in the data, so §2.6 fingerprints the source
    instead: when ticks appear the record stops matching and the day rebuilds.
    """
    monday = OPEN
    wednesday = OPEN + timedelta(days=2)
    _write_ticks(tmp_path, _minutes(monday, 10) + _minutes(wednesday, 10))
    store = HistoryStore(tmp_path)

    week = store.load_bars("SPY", monday, wednesday + timedelta(hours=1), "5m")
    assert week["bucket_start_et"].dt.date().n_unique() == 2

    # The operator backfills Tuesday from a TradeStation export.
    _write_ticks(tmp_path, _minutes(OPEN + timedelta(days=1), 10, base=470.0))

    tuesday = store.load_bars(
        "SPY", OPEN + timedelta(days=1), OPEN + timedelta(days=1, hours=1), "5m"
    )
    assert tuesday.height == 2, "backfilled ticks must be picked up by load_bars itself"
    after = store.load_bars("SPY", monday, wednesday + timedelta(hours=1), "5m")
    assert after["bucket_start_et"].dt.date().n_unique() == 3


# ---- days with no source --------------------------------------------------


def test_a_day_with_no_source_stops_rescanning_the_tick_tree(tmp_path: Path) -> None:
    """Absence is a fingerprint too, so it can be recorded and matched.

    Without that, days outside the ingested span stay permanently uncached and
    every repeat query re-resamples the whole span — the regression that sank
    the first attempt.
    """
    _write_ticks(tmp_path, _minutes(OPEN, 10))
    store = HistoryStore(tmp_path)
    early = OPEN - timedelta(days=5)

    store.load_bars("SPY", early, OPEN + timedelta(hours=1), "5m")
    calls = _count_resamples(store)
    store.load_bars("SPY", early, OPEN + timedelta(hours=1), "5m")
    store.load_bars("SPY", early, OPEN + timedelta(hours=1), "5m")
    assert calls["n"] == 0, "a fully recorded range must not touch the resampler again"


def test_no_zero_row_bar_file_is_ever_written(tmp_path: Path) -> None:
    """Empty days live in the record, never as a placeholder bar file.

    A 0-row file for `1m` lands in the native Tier-2 directory, which
    `clear_bar_cache` deliberately does not touch — the documented recovery
    path could not remove it.
    """
    _write_ticks(tmp_path, _minutes(OPEN, 10))
    store = HistoryStore(tmp_path)
    store.load_bars("SPY", OPEN - timedelta(days=3), OPEN + timedelta(days=3), "5m")
    store.load_bars("SPY", OPEN - timedelta(days=3), OPEN + timedelta(days=3), "1m")

    for parquet in (tmp_path / "bars").rglob("bars.parquet"):
        assert pl.read_parquet(parquet).height > 0, f"placeholder written at {parquet}"


def test_a_day_whose_only_source_is_1m_bars_still_builds(tmp_path: Path) -> None:
    """The 1m fallback must be decided per day, not for the whole span.

    Ticks get pruned while the 1m bars are kept — the retention shape CLAUDE.md
    describes. If the span-wide resample returns rows for *any* day, a
    span-wide `height == 0` check skips the fallback, and the tick-less day is
    recorded as permanently empty with its 1m bars sitting right there.
    """
    monday, tuesday, wednesday = (OPEN + timedelta(days=i) for i in range(3))
    _write_ticks(tmp_path, _minutes(monday, 10) + _minutes(wednesday, 10))
    with BarWriter(tmp_path / "bars") as w:
        for i in range(10):
            w.write(
                Bar(
                    symbol="SPY",
                    bucket_start=tuesday + timedelta(minutes=i),
                    open=1.0,
                    high=2.0,
                    low=0.5,
                    close=460.0 + i,
                    volume=10,
                    tick_count=3,
                    source="tradestation_el",
                    timeframe="1m",
                )
            )
    store = HistoryStore(tmp_path)
    store.load_bars("SPY", monday, wednesday + timedelta(hours=1), "5m")

    tue = store.load_bars("SPY", tuesday, tuesday + timedelta(hours=1), "5m")
    assert tue.height == 2, "the 1m bars for that day must still be rolled up"


# ---- the other writers ----------------------------------------------------


def test_a_partition_this_builder_did_not_write_is_rebuilt(tmp_path: Path) -> None:
    """An existing cache holds partitions written by older, window-scoped code.

    They carry no record, so they cannot be trusted as complete — otherwise
    upgrading would preserve the short-series bug forever.
    """
    _write_ticks(tmp_path, _minutes(OPEN, 60))
    partial = pa.table(
        {
            "bucket_start": [OPEN],
            "open": [450.0],
            "high": [451.0],
            "low": [449.0],
            "close": [450.5],
            "volume": [100],
            "tick_count": [3],
            "source": [derived_source("ticks")],
        }
    )
    out = _cache_dir(tmp_path) / "date=2026-04-20" / "bars.parquet"
    out.parent.mkdir(parents=True)
    pq.write_table(partial, out)

    df = HistoryStore(tmp_path).load_bars("SPY", OPEN, OPEN + timedelta(hours=1), "5m")
    assert df.height == 12, "a partition with no coverage record must be rebuilt, not trusted"


def test_native_partitions_are_neither_rebuilt_nor_overwritten(tmp_path: Path) -> None:
    """§2.3 rule 3 still wins: derived must never replace what came off the wire."""
    _write_ticks(tmp_path, _minutes(OPEN, 60))
    with BarWriter(tmp_path / "bars") as w:
        w.write(
            Bar(
                symbol="SPY",
                bucket_start=OPEN,
                open=1.0,
                high=2.0,
                low=0.5,
                close=999.0,
                volume=10,
                tick_count=3,
                source="tradestation_el",
                timeframe="5m",
            )
        )
    store = HistoryStore(tmp_path)
    df = store.load_bars("SPY", OPEN, OPEN + timedelta(hours=1), "5m")
    assert 999.0 in df["close"].to_list()
    assert "tradestation_el" in df["source"].to_list()

    stored = pl.read_parquet(_cache_dir(tmp_path) / "date=2026-04-20" / "bars.parquet")
    assert 999.0 in stored["close"].to_list(), "the native bar must survive on disk"


# ---- the record is discardable -------------------------------------------


def test_deleting_the_record_costs_a_recompute_and_nothing_else(tmp_path: Path) -> None:
    """§2.6 rule 4. A binding that never heard of the record must still be
    correct, so removing it may only cost work — never change an answer."""
    _write_ticks(tmp_path, _minutes(OPEN, 30))
    store = HistoryStore(tmp_path)
    window = (OPEN, OPEN + timedelta(hours=1))
    before = store.load_bars("SPY", *window, "5m")

    records = list((tmp_path / "bars").rglob("_coverage.json"))
    assert records, "the builder must leave a coverage record"
    for record in records:
        record.unlink()

    after = store.load_bars("SPY", *window, "5m")
    assert after.schema == before.schema
    assert after["close"].to_list() == before["close"].to_list()


def test_daily_has_no_coverage_record(tmp_path: Path) -> None:
    """§2.6 rule 5: `1d` is never locally computed, so it has nothing to cover."""
    with BarWriter(tmp_path / "bars") as w:
        w.write(
            Bar(
                symbol="SPY",
                bucket_start=datetime(2026, 4, 20, 8, 0, tzinfo=UTC),
                open=1.0,
                high=2.0,
                low=0.5,
                close=450.0,
                volume=10,
                tick_count=3,
                source="tradestation_el",
                timeframe="1d",
            )
        )
    store = HistoryStore(tmp_path)
    store.load_bars(
        "SPY", datetime(2026, 4, 19, tzinfo=UTC), datetime(2026, 4, 24, tzinfo=UTC), "1d"
    )
    assert not list((tmp_path / "bars" / "timeframe=1d").rglob("_coverage.json"))
