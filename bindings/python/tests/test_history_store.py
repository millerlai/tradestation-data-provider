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
