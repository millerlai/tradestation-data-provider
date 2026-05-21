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
    with BarWriter(tmp_path / "bars", timeframe="1m") as w:
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
