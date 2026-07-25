from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.storage import Resampler, TickWriter, Timeframe, timeframe_to_minutes
from tradestation_data.storage.bar_writer import BarWriter


def _tick(
    symbol: str,
    ts: datetime,
    price: float,
    *,
    volume: int = 100,
    bid: float | None = None,
    ask: float | None = None,
) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        volume=volume,
        bid=bid,
        ask=ask,
        tick_count=1,
        source="tradestation_el",
    )


T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)


def _write_ticks(root: Path, ticks: list[Tick]) -> None:
    with TickWriter(root) as w:
        for t in ticks:
            w.write(t)


def test_resample_1m_recovers_bar_from_ticks(tmp_path: Path) -> None:
    ticks_root = tmp_path / "ticks"
    _write_ticks(
        ticks_root,
        [
            _tick("SPY", T0 + timedelta(seconds=1), 450.0, volume=100),
            _tick("SPY", T0 + timedelta(seconds=30), 451.0, volume=200),
            _tick("SPY", T0 + timedelta(seconds=59), 449.5, volume=100),
        ],
    )
    resampler = Resampler(ticks_root)
    df = resampler.resample("SPY", T0, T0 + timedelta(minutes=1), Timeframe.M1)
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["open"] == pytest.approx(450.0)
    assert row["high"] == pytest.approx(451.0)
    assert row["low"] == pytest.approx(449.5)
    assert row["close"] == pytest.approx(449.5)
    assert row["volume"] == 400
    assert row["tick_count"] == 3
    assert row["symbol"] == "SPY"


def test_resample_5m_aggregates_five_minutes(tmp_path: Path) -> None:
    ticks_root = tmp_path / "ticks"
    ticks: list[Tick] = []
    for i in range(5):
        base = T0 + timedelta(minutes=i)
        ticks.append(_tick("SPY", base + timedelta(seconds=5), 450.0 + i, volume=100))
        ticks.append(_tick("SPY", base + timedelta(seconds=55), 450.0 + i + 0.5, volume=100))
    _write_ticks(ticks_root, ticks)

    df = Resampler(ticks_root).resample("SPY", T0, T0 + timedelta(minutes=5), "5m")
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["open"] == pytest.approx(450.0)
    assert row["close"] == pytest.approx(454.5)
    assert row["high"] == pytest.approx(454.5)
    assert row["low"] == pytest.approx(450.0)
    assert row["volume"] == 1000
    assert row["tick_count"] == 10


def test_resample_zero_volume_index_symbol(tmp_path: Path) -> None:
    ticks_root = tmp_path / "ticks"
    _write_ticks(
        ticks_root,
        [
            _tick("VXX", T0 + timedelta(seconds=10), 18.5, volume=0),
            _tick("VXX", T0 + timedelta(seconds=50), 18.6, volume=0),
        ],
    )
    df = Resampler(ticks_root).resample("VXX", T0, T0 + timedelta(minutes=1), "1m")
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["volume"] == 0
    assert row["open"] == pytest.approx(18.5)
    assert row["close"] == pytest.approx(18.6)


def test_resample_no_data_returns_empty(tmp_path: Path) -> None:
    df = Resampler(tmp_path / "ticks").resample("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert df.height == 0
    assert "bucket_start" in df.columns


def test_resample_time_window_filters(tmp_path: Path) -> None:
    ticks_root = tmp_path / "ticks"
    _write_ticks(
        ticks_root,
        [
            _tick("SPY", T0 + timedelta(seconds=10), 450.0),
            _tick("SPY", T0 + timedelta(minutes=5, seconds=10), 451.0),
            _tick("SPY", T0 + timedelta(minutes=10, seconds=10), 452.0),
        ],
    )
    df = Resampler(ticks_root).resample(
        "SPY",
        T0 + timedelta(minutes=4),
        T0 + timedelta(minutes=9),
        "1m",
    )
    prices = df.select("close").to_series().to_list()
    assert prices == pytest.approx([451.0])


def test_unsupported_timeframe_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        Resampler(tmp_path).resample("SPY", T0, T0 + timedelta(hours=1), "7m")


def test_resample_returns_polars_dataframe(tmp_path: Path) -> None:
    ticks_root = tmp_path / "ticks"
    _write_ticks(ticks_root, [_tick("SPY", T0 + timedelta(seconds=1), 450.0)])
    df = Resampler(ticks_root).resample("SPY", T0, T0 + timedelta(minutes=1), "1m")
    assert isinstance(df, pl.DataFrame)


def test_timeframe_to_minutes_maps_standard_timeframes() -> None:
    assert timeframe_to_minutes("1m") == 1
    assert timeframe_to_minutes("5m") == 5
    assert timeframe_to_minutes("15m") == 15
    assert timeframe_to_minutes(Timeframe.M5) == 5
    with pytest.raises(ValueError, match="Unsupported"):
        timeframe_to_minutes("7m")


def _bar_1m(
    sym: str, t: datetime, open_: float, high: float, low: float, close: float, vol: int = 100
) -> Bar:
    return Bar(
        symbol=sym,
        bucket_start=t,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=vol,
        tick_count=5,
        source="tradestation_el",
    )


def _write_bars(bars_root: Path, bars: list[Bar]) -> None:
    with BarWriter(bars_root, timeframe="1m") as w:
        for b in bars:
            w.write(b)


def test_resample_from_bars_rolls_up_five_1m_bars_to_one_5m(tmp_path: Path) -> None:
    bars_root = tmp_path / "bars"
    bars = [
        _bar_1m(
            "SPY",
            T0 + timedelta(minutes=i),
            450.0 + i,
            450.0 + i + 0.3,
            450.0 + i - 0.1,
            450.0 + i + 0.2,
            vol=100 * (i + 1),
        )
        for i in range(5)
    ]
    _write_bars(bars_root, bars)

    resampler = Resampler(tmp_path / "ticks", bars_root=bars_root)
    df = resampler.resample_from_bars(
        "SPY", T0, T0 + timedelta(minutes=5), "5m", source_timeframe="1m"
    )
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["open"] == pytest.approx(450.0)  # first bar open
    assert row["close"] == pytest.approx(454.2)  # last bar close
    assert row["high"] == pytest.approx(454.3)  # max of highs
    assert row["low"] == pytest.approx(449.9)  # min of lows
    assert row["volume"] == sum(100 * (i + 1) for i in range(5))
    assert row["tick_count"] == 5 * 5


def test_resample_from_bars_returns_empty_when_no_source(tmp_path: Path) -> None:
    resampler = Resampler(tmp_path / "ticks", bars_root=tmp_path / "bars")
    df = resampler.resample_from_bars("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert df.height == 0


def test_resample_from_bars_rejects_equal_source_and_target(tmp_path: Path) -> None:
    resampler = Resampler(tmp_path / "ticks", bars_root=tmp_path / "bars")
    with pytest.raises(ValueError, match="source_timeframe must differ"):
        resampler.resample_from_bars("SPY", T0, T0 + timedelta(hours=1), "1m")


def test_resample_from_bars_without_bars_root_returns_empty(tmp_path: Path) -> None:
    """Covers line 154."""
    resampler = Resampler(tmp_path / "ticks", bars_root=None)
    df = resampler.resample_from_bars("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert df.height == 0


def test_resample_from_bars_rejects_unsupported_timeframe(tmp_path: Path) -> None:
    """Covers line 158."""
    resampler = Resampler(tmp_path / "ticks", bars_root=tmp_path / "bars")
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        resampler.resample_from_bars("SPY", T0, T0 + timedelta(hours=1), "7m")
