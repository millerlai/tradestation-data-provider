from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tradestation_data.domain.tick import Tick
from tradestation_data.storage import HistoryStore, TickWriter

T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)


def _tick(symbol: str, ts: datetime, price: float, *, volume: int = 100) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        volume=volume,
        bid=None,
        ask=None,
        tick_count=1,
        source="tradestation_el",
    )


def _populate_ticks(root: Path, ticks: list[Tick]) -> None:
    with TickWriter(root / "ticks") as w:
        for t in ticks:
            w.write(t)


def test_load_ticks_returns_rows_in_window(tmp_path: Path) -> None:
    _populate_ticks(
        tmp_path,
        [
            _tick("SPY", T0 + timedelta(seconds=5), 450.0),
            _tick("SPY", T0 + timedelta(seconds=30), 450.5),
            _tick("SPY", T0 + timedelta(minutes=10), 451.0),
        ],
    )
    store = HistoryStore(tmp_path)
    df = store.load_ticks("SPY", T0, T0 + timedelta(minutes=5))
    assert df.height == 2
    assert df.select("price").to_series().to_list() == pytest.approx([450.0, 450.5])


def test_cache_miss_and_hit_agree_on_the_et_column(tmp_path: Path) -> None:
    """The first call must not hand back a frame one column short.

    A hit reads BAR_SCHEMA off disk, which always carries ``bucket_start_et``.
    When the miss path returned the raw resampler frame instead, identical
    caller code raised KeyError or worked depending on whether anyone had
    asked for that range before — the worst kind of intermittent.
    """
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
    store = HistoryStore(tmp_path)
    window = (T0, T0 + timedelta(minutes=5))

    miss = store.load_bars("SPY", *window, "5m")  # builds the cache
    hit = store.load_bars("SPY", *window, "5m")  # reads it back

    assert "bucket_start_et" in miss.columns
    assert "bucket_start_et" in hit.columns
    assert miss["bucket_start_et"].to_list() == hit["bucket_start_et"].to_list()
    assert miss["bucket_start"].to_list() == hit["bucket_start"].to_list()

    # Comparing instants alone is not enough, and this test used to do only
    # that. DuckDB re-labels every TIMESTAMPTZ with the session zone, so the
    # hit path handed back an ET column declared UTC: identical instants,
    # and `.dt.hour()` answering 13 instead of 9 — which of the two you got
    # depended on whether anyone had warmed that range before.
    assert miss.schema["bucket_start_et"] == hit.schema["bucket_start_et"]
    assert hit.schema["bucket_start_et"].time_zone == "America/New_York"
    assert miss["bucket_start_et"].dt.hour().to_list() == hit["bucket_start_et"].dt.hour().to_list()


def test_load_bars_cache_miss_builds_and_persists(tmp_path: Path) -> None:
    _populate_ticks(
        tmp_path,
        [
            _tick("SPY", T0 + timedelta(seconds=5), 450.0, volume=100),
            _tick("SPY", T0 + timedelta(seconds=55), 451.0, volume=100),
        ],
    )
    store = HistoryStore(tmp_path)
    df = store.load_bars("SPY", T0, T0 + timedelta(minutes=1), "5m")
    assert df.height == 1
    cache_file = (
        tmp_path / "bars" / "timeframe=5m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    )
    assert cache_file.exists()


def test_load_bars_cache_hit_does_not_touch_ticks(tmp_path: Path) -> None:
    _populate_ticks(
        tmp_path,
        [
            _tick("SPY", T0 + timedelta(seconds=5), 450.0),
            _tick("SPY", T0 + timedelta(seconds=55), 451.0),
        ],
    )
    store = HistoryStore(tmp_path)
    first = store.load_bars("SPY", T0, T0 + timedelta(minutes=1), "5m")

    # Nuke the ticks — a second call must still succeed (served from cache)
    for p in (tmp_path / "ticks").rglob("*.parquet"):
        p.unlink()

    second = store.load_bars("SPY", T0, T0 + timedelta(minutes=1), "5m")
    assert second.height == first.height
    assert second.select("close").to_series().to_list() == pytest.approx(
        first.select("close").to_series().to_list()
    )


def test_rebuild_bar_cache_overwrites(tmp_path: Path) -> None:
    _populate_ticks(
        tmp_path,
        [_tick("SPY", T0 + timedelta(seconds=5), 450.0)],
    )
    store = HistoryStore(tmp_path)
    df1 = store.load_bars("SPY", T0, T0 + timedelta(minutes=1), "5m")
    assert df1.select("close").to_series().to_list() == pytest.approx([450.0])

    # Replace ticks with different data and force rebuild
    for p in (tmp_path / "ticks").rglob("*.parquet"):
        p.unlink()
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 999.0)])

    df2 = store.rebuild_bar_cache("SPY", T0, T0 + timedelta(minutes=1), "5m")
    assert df2.select("close").to_series().to_list() == pytest.approx([999.0])


def test_load_bars_no_data_returns_empty(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    df = store.load_bars("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert df.height == 0
    assert isinstance(df, pl.DataFrame)


def test_load_ticks_empty_root_returns_empty_df(tmp_path: Path) -> None:
    """Covers line 46: no tick files → early-return empty DataFrame."""
    store = HistoryStore(tmp_path)
    df = store.load_ticks("SPY", T0, T0 + timedelta(hours=1))
    assert isinstance(df, pl.DataFrame)
    assert df.height == 0


# ---- empty result sets ----------------------------------------------------
#
# The file-existence guards only rule out "no partition at all". A partition
# that exists but holds nothing inside the window still has to come back as an
# empty frame: asking about a day a symbol did not trade is an ordinary
# question, and it used to abort the whole caller.


def test_load_ticks_partition_present_but_window_empty(tmp_path: Path) -> None:
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
    store = HistoryStore(tmp_path)
    df = store.load_ticks("SPY", T0 + timedelta(days=1), T0 + timedelta(days=2))
    assert isinstance(df, pl.DataFrame)
    assert df.height == 0


def test_load_cached_bars_partition_present_but_window_empty(tmp_path: Path) -> None:
    """`None` means "nothing stored"; a quiet window is 0 rows, not `None`.

    `audit_bar_cache` leans on that distinction — it substitutes an empty
    frame only when nothing is cached — so the two cases must stay tellable
    apart.
    """
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
    store = HistoryStore(tmp_path)
    store.load_bars("SPY", T0, T0 + timedelta(minutes=5), "5m")  # writes the cache

    # The cache file now exists, so the glob guard passes and the query runs.
    window = (T0 + timedelta(days=1), T0 + timedelta(days=2))
    quiet = store.load_cached_bars("SPY", *window, "5m")
    assert quiet is not None
    assert quiet.height == 0
    assert "bucket_start" in quiet.columns

    # Nothing stored at all still answers None.
    assert store.load_cached_bars("NOSUCH", *window, "5m") is None


def test_empty_and_populated_answers_share_one_schema(tmp_path: Path) -> None:
    """The three ways of having no data must be indistinguishable to a caller.

    "Never recorded", "recorded but quiet", and "has rows" differ only in
    height. When they differed in width or dtype, stacking a symbol loop with
    `pl.concat` raised ShapeError the first time one symbol had a quiet day,
    and `df["bucket_start_et"]` raised ColumnNotFoundError on the empty frame.
    """
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
    store = HistoryStore(tmp_path)
    quiet_window = (T0 + timedelta(days=1), T0 + timedelta(days=2))

    populated = store.load_ticks("SPY", T0, T0 + timedelta(minutes=5))
    quiet = store.load_ticks("SPY", *quiet_window)
    never = store.load_ticks("NOSUCH", T0, T0 + timedelta(minutes=5))
    assert populated.height > 0
    assert quiet.height == 0 and never.height == 0
    assert quiet.schema == populated.schema
    assert never.schema == populated.schema
    assert pl.concat([populated, quiet, never]).height == populated.height

    bars_populated = store.load_bars("SPY", T0, T0 + timedelta(minutes=5), "5m")
    bars_quiet = store.load_bars("SPY", *quiet_window, "5m")
    bars_never = store.load_bars("NOSUCH", T0, T0 + timedelta(minutes=5), "5m")
    assert bars_populated.height > 0
    assert bars_quiet.height == 0 and bars_never.height == 0
    assert bars_quiet.schema == bars_populated.schema
    assert bars_never.schema == bars_populated.schema
    assert pl.concat([bars_populated, bars_quiet, bars_never]).height == bars_populated.height


def test_daily_empty_answer_matches_the_daily_hit(tmp_path: Path) -> None:
    """`1d` returns through its own branch, and its layout has no `date=` level.

    A read that hits the single file carries the `timeframe` hive key, so the
    empty answer has to as well — otherwise the same stacking that works for
    intraday raises on the one timeframe that is data rather than cache.
    """
    from tradestation_data.domain.bar import Bar
    from tradestation_data.storage.bar_writer import BarWriter

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
    populated = store.load_bars(
        "SPY", datetime(2026, 4, 19, tzinfo=UTC), datetime(2026, 4, 24, tzinfo=UTC), "1d"
    )
    quiet = store.load_bars(
        "SPY", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 5, tzinfo=UTC), "1d"
    )
    assert populated.height == 1
    assert quiet.height == 0
    assert quiet.schema == populated.schema
    assert pl.concat([populated, quiet]).height == 1


def test_rebuilding_one_window_keeps_the_rest_of_the_day(tmp_path: Path) -> None:
    """A partition is a whole ET day; a build only covers the asked-for window.

    `pq.write_table` overwrites, so rebuilding the RTH window used to drop the
    pre-market bars cached by an earlier call — row count on disk falling with
    nothing raised, and unrecoverable once the Tier-1 ticks are pruned.
    """
    pre_open = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)  # 08:00 ET
    rth = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)  # 09:30 ET, same ET date
    _populate_ticks(
        tmp_path,
        [_tick("SPY", pre_open + timedelta(minutes=i), 440.0 + i) for i in range(20)]
        + [_tick("SPY", rth + timedelta(minutes=i), 450.0 + i) for i in range(60)],
    )
    store = HistoryStore(tmp_path)
    part = tmp_path / "bars" / "timeframe=5m" / "symbol=SPY" / "date=2026-04-20" / "bars.parquet"

    store.load_bars("SPY", pre_open, pre_open + timedelta(minutes=20), "5m")
    pre_rows = pl.read_parquet(part).height
    assert pre_rows == 4

    store.load_bars("SPY", rth, rth + timedelta(hours=1), "5m")
    after = pl.read_parquet(part)
    assert after.height == pre_rows + 12, "the pre-market bars must survive the RTH rebuild"
    assert after["bucket_start"].is_sorted()
    assert after["bucket_start"].n_unique() == after.height


def test_load_bars_falls_back_to_1m_cache_for_index_symbol(tmp_path: Path) -> None:
    """Covers lines 131-134: rollup from cached 1m bars when no ticks exist."""
    from tradestation_data.domain.bar import Bar
    from tradestation_data.storage.bar_writer import BarWriter

    # Populate 1m cache directly for '$TICK' (no tick files for this symbol).
    with BarWriter(tmp_path / "bars") as w:
        for i in range(5):
            w.write(
                Bar(
                    symbol="$TICK",
                    bucket_start=T0 + timedelta(minutes=i),
                    open=100.0 + i,
                    high=110.0 + i,
                    low=90.0 + i,
                    close=105.0 + i,
                    volume=0,
                    tick_count=1,
                    source="tradestation_el",
                )
            )
    store = HistoryStore(tmp_path)
    df = store.load_bars("$TICK", T0, T0 + timedelta(minutes=5), "5m")
    assert df.height == 1


def test_rebuild_bar_cache_missing_dir_is_noop(tmp_path: Path) -> None:
    """Covers line 155: _delete_cache short-circuits when dir absent."""
    _populate_ticks(
        tmp_path,
        [_tick("SPY", T0 + timedelta(seconds=5), 450.0)],
    )
    store = HistoryStore(tmp_path)
    # No prior cache exists for 5m → rebuild should simply build fresh.
    df = store.rebuild_bar_cache("SPY", T0, T0 + timedelta(minutes=1), "5m")
    assert df.height == 1


def test_different_timeframes_are_independent_caches(tmp_path: Path) -> None:
    ticks: list[Tick] = []
    for i in range(15):
        ticks.append(_tick("SPY", T0 + timedelta(minutes=i, seconds=5), 450.0 + i * 0.1))
    _populate_ticks(tmp_path, ticks)

    store = HistoryStore(tmp_path)
    df_5m = store.load_bars("SPY", T0, T0 + timedelta(minutes=15), "5m")
    df_15m = store.load_bars("SPY", T0, T0 + timedelta(minutes=15), "15m")
    assert df_5m.height == 3
    assert df_15m.height == 1
    assert (tmp_path / "bars" / "timeframe=5m").exists()
    assert (tmp_path / "bars" / "timeframe=15m").exists()


# ---- native vs derived provenance -----------------------------------------


def test_resampled_bars_are_stamped_as_derived(tmp_path: Path) -> None:
    """Provenance has to be visible on disk, or the guard below cannot work."""
    from tradestation_data.domain.bar import is_derived
    from tradestation_data.storage.resampler import Resampler

    root = tmp_path / "ticks"
    t0 = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
    with TickWriter(root) as w:
        for i in range(3):
            w.write(
                Tick(
                    symbol="SPY",
                    timestamp=t0 + timedelta(seconds=20 * i),
                    price=450.0 + i,
                    volume=100,
                    bid=None,
                    ask=None,
                    tick_count=1,
                    source="tradestation_el",
                )
            )

    df = Resampler(root).resample("SPY", t0 - timedelta(hours=1), t0 + timedelta(hours=1), "5m")
    assert df.height > 0
    assert all(is_derived(s) for s in df["source"].to_list())


def test_derived_bars_never_overwrite_native_ones(tmp_path: Path) -> None:
    """Any charted interval can be native, not just 1m: a 5-minute chart
    publishes native 5m bars into the same directory the resampler writes.

    pq.write_table overwrites, and a range that is only partly cached
    rebuilds the whole span, so without the guard one missing bucket would
    take the native ones beside it with it. (`1d` cannot reach this path at
    all any more — see test_daily_is_never_derived.)
    """
    from tradestation_data.domain.bar import Bar
    from tradestation_data.storage.bar_writer import BarWriter

    root = tmp_path / "store"
    bars_root = root / "bars"
    ticks_root = root / "ticks"

    native_close = 999.0
    day = datetime(2026, 4, 20, 14, 0, tzinfo=UTC)  # 10:00 ET — on the 5m grid
    with BarWriter(bars_root) as w:
        w.write(
            Bar(
                symbol="SPY",
                bucket_start=day,
                open=1.0,
                high=2.0,
                low=0.5,
                close=native_close,
                volume=10,
                tick_count=3,
                source="tradestation_el",
                timeframe="5m",
            )
        )

    # Ticks that would produce a completely different daily bar.
    with TickWriter(ticks_root) as w:
        for i in range(3):
            w.write(
                Tick(
                    symbol="SPY",
                    timestamp=datetime(2026, 4, 20, 14, 0, tzinfo=UTC) + timedelta(minutes=i),
                    price=100.0 + i,
                    volume=5,
                    bid=None,
                    ask=None,
                    tick_count=1,
                    source="tradestation_el",
                )
            )

    store = HistoryStore(root)
    # rebuild_bar_cache deletes before it rebuilds. Unlink-then-write is an
    # overwrite spelled differently, so the eviction has to respect the guard
    # too — otherwise it hands the write side an empty directory to wave
    # through.
    store.rebuild_bar_cache(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 22, tzinfo=UTC),
        "5m",
    )

    out = store.load_bars(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 22, tzinfo=UTC),
        "5m",
    )
    assert native_close in out["close"].to_list()
    assert "tradestation_el" in out["source"].to_list()
    assert list((bars_root / "timeframe=5m").rglob("bars.parquet")), (
        "the native partition file must survive rebuild_bar_cache"
    )


def test_cache_partitions_on_the_et_date_like_the_writer(tmp_path: Path) -> None:
    """An EST evening bar must land in the date= dir BarWriter would use.

    BarWriter partitions on ``bucket_start_et.date()``; the cache used to
    split on the UTC date. From 19:00 ET onwards in EST those differ, and the
    two failure modes are both silent: the native-data guard inspects a file
    that isn't there, and ``_load_cached_bars`` globs ``date=*``, so a second
    derived copy comes back as duplicate rows.
    """
    root = tmp_path / "store"
    # 19:30 ET on 2026-01-05 (EST, UTC-5) == 2026-01-06 00:30 UTC.
    ts = datetime(2026, 1, 6, 0, 30, tzinfo=UTC)
    with TickWriter(root / "ticks") as w:
        for i in range(3):
            w.write(
                Tick(
                    symbol="SPY",
                    timestamp=ts + timedelta(seconds=10 * i),
                    price=100.0 + i,
                    volume=5,
                    bid=None,
                    ask=None,
                    tick_count=1,
                    source="tradestation_el",
                )
            )

    store = HistoryStore(root)
    df = store.load_bars("SPY", ts - timedelta(hours=2), ts + timedelta(hours=2), "1m")
    assert df.height == 1

    cache_dir = root / "bars" / "timeframe=1m" / "symbol=SPY"
    assert [p.name for p in sorted(cache_dir.glob("date=*"))] == ["date=2026-01-05"]


def test_daily_is_never_derived(tmp_path: Path) -> None:
    """`1d` is published, not computed.

    TradeStation's daily bar carries the exchange's official close and the
    split/dividend adjustment; a rollup of minutes reproduces neither and is
    indistinguishable from the real thing once on disk. An empty answer is
    the truthful one — see contract/semantics.md 2.3.
    """
    from tradestation_data.domain.tick import Tick
    from tradestation_data.storage.tick_writer import TickWriter

    root = tmp_path / "store"
    with TickWriter(root / "ticks") as w:
        for i in range(5):
            w.write(
                Tick(
                    symbol="SPY",
                    timestamp=datetime(2026, 4, 20, 14, 0, tzinfo=UTC) + timedelta(minutes=i),
                    price=100.0 + i,
                    volume=5,
                    bid=None,
                    ask=None,
                    tick_count=1,
                    source="tradestation_el",
                )
            )

    store = HistoryStore(root)
    out = store.load_bars(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 22, tzinfo=UTC),
        "1d",
    )
    assert out.height == 0
    assert not (root / "bars" / "timeframe=1d").exists(), (
        "a miss must not leave a computed daily bar behind"
    )

    # The same rule from the other side: a rebuild is a delete plus a
    # recompute, and neither half is legal on data that is the only copy.
    with pytest.raises(ValueError, match="published, not derived"):
        store.rebuild_bar_cache(
            "SPY",
            datetime(2026, 4, 19, tzinfo=UTC),
            datetime(2026, 4, 22, tzinfo=UTC),
            "1d",
        )


def test_daily_reads_the_single_file_layout(tmp_path: Path) -> None:
    """BarWriter drops the date= level for 1d; the reader must follow it,
    or a glob that matches nothing reads as 'no data' rather than an error."""
    from tradestation_data.domain.bar import Bar
    from tradestation_data.storage.bar_writer import BarWriter

    root = tmp_path / "store"
    with BarWriter(root / "bars") as w:
        for i in range(3):
            w.write(
                Bar(
                    symbol="SPY",
                    bucket_start=datetime(2026, 4, 20, 8, 0, tzinfo=UTC) + timedelta(days=i),
                    open=1.0,
                    high=2.0,
                    low=0.5,
                    close=450.0 + i,
                    volume=10,
                    tick_count=3,
                    source="tradestation_el",
                    timeframe="1d",
                )
            )

    out = HistoryStore(root).load_bars(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 24, tzinfo=UTC),
        "1d",
    )
    assert out["close"].to_list() == [450.0, 451.0, 452.0]


def test_et_columns_keep_their_zone_through_every_read_path(tmp_path: Path) -> None:
    """`*_et` exists so downstream never converts at query time.

    A column labelled UTC defeats that silently: the instant is right, so
    nothing raises, but every wall-clock question asked of it — which
    session, is this RTH — is answered in the wrong zone.
    """
    from tradestation_data.domain.bar import Bar
    from tradestation_data.storage.bar_writer import BarWriter

    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
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

    ticks = store.load_ticks("SPY", T0, T0 + timedelta(minutes=5))
    assert ticks.schema["timestamp_et"].time_zone == "America/New_York"
    # T0 is 13:30 UTC = 09:30 EDT.
    assert ticks["timestamp_et"].dt.hour().to_list() == [9]

    bars = store.load_bars("SPY", T0, T0 + timedelta(minutes=5), "5m")
    assert bars.schema["bucket_start_et"].time_zone == "America/New_York"
    assert bars["bucket_start_et"].dt.hour().to_list() == [9]

    # The single-file layout reads through the same path.
    daily = store.load_bars(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 22, tzinfo=UTC),
        "1d",
    )
    assert daily.schema["bucket_start_et"].time_zone == "America/New_York"
    assert daily["bucket_start_et"].dt.hour().to_list() == [4]  # the 04:00 ET anchor
