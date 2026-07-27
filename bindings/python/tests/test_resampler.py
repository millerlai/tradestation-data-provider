from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def test_resample_partition_present_but_window_empty(tmp_path: Path) -> None:
    """A day the symbol did not trade is a question, not an error.

    The file-existence guard above only rules out "no partition at all"; the
    query itself can still match nothing, and both callers of this method
    branch on ``height == 0``.
    """
    ticks_root = tmp_path / "ticks"
    _write_ticks(ticks_root, [_tick("SPY", T0 + timedelta(seconds=10), 450.0)])
    df = Resampler(ticks_root).resample("SPY", T0 + timedelta(days=1), T0 + timedelta(days=2), "5m")
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
    with BarWriter(bars_root) as w:
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


def test_resample_from_bars_partition_present_but_window_empty(tmp_path: Path) -> None:
    bars_root = tmp_path / "bars"
    _write_bars(bars_root, [_bar_1m("SPY", T0, 450.0, 450.5, 449.5, 450.2)])
    resampler = Resampler(tmp_path / "ticks", bars_root=bars_root)
    df = resampler.resample_from_bars(
        "SPY", T0 + timedelta(days=1), T0 + timedelta(days=2), "5m", source_timeframe="1m"
    )
    assert df.height == 0
    assert "bucket_start" in df.columns


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


# ---- session-anchored bucket alignment (contract/semantics.md §2.2) -------
#
# 09:30 ET on 2026-04-20 is 13:30 UTC (EDT, UTC-4).

_OPEN_UTC = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)
_ET = ZoneInfo("America/New_York")


def _et(df: pl.DataFrame) -> list[str]:
    """bucket_start rendered in ET, which is where the alignment is visible."""
    return [b.astimezone(_ET).strftime("%Y-%m-%d %H:%M") for b in df["bucket_start"].to_list()]


def test_hourly_buckets_open_at_the_session_open_not_the_clock_hour(tmp_path: Path) -> None:
    """Epoch-aligned hours would label the first RTH bar 09:00.

    That bar would hold only 09:30-10:00 — half a bar wearing a whole bar's
    timestamp, which nothing downstream could tell apart from a real one.
    """
    root = tmp_path / "ticks"
    # One tick every 10 minutes for two hours from the open.
    _write_ticks(
        root, [_tick("SPY", _OPEN_UTC + timedelta(minutes=10 * i), 450.0 + i) for i in range(12)]
    )

    df = (
        Resampler(root)
        .resample("SPY", _OPEN_UTC - timedelta(hours=1), _OPEN_UTC + timedelta(hours=3), "1h")
        .sort("bucket_start")
    )

    assert _et(df) == ["2026-04-20 09:30", "2026-04-20 10:30"]


def test_intraday_frames_align_to_the_open(tmp_path: Path) -> None:
    root = tmp_path / "ticks"
    _write_ticks(root, [_tick("SPY", _OPEN_UTC + timedelta(minutes=i), 450.0) for i in range(60)])
    rs = Resampler(root)
    window = (_OPEN_UTC - timedelta(hours=1), _OPEN_UTC + timedelta(hours=2))

    for tf, first in [("5m", "09:30"), ("15m", "09:30"), ("30m", "09:30")]:
        df = rs.resample("SPY", *window, tf).sort("bucket_start")
        assert _et(df)[0] == f"2026-04-20 {first}", tf


def test_daily_bucket_keeps_the_post_market_on_the_same_session(tmp_path: Path) -> None:
    """UTC midnight falls at 20:00 ET — the exact end of the extended session.

    Bucketing on it would push every post-market print onto the next day.
    """
    root = tmp_path / "ticks"
    _write_ticks(
        root,
        [
            _tick("SPY", datetime(2026, 4, 20, 13, 30, tzinfo=UTC), 450.0),  # 09:30 ET
            _tick("SPY", datetime(2026, 4, 20, 23, 59, tzinfo=UTC), 451.0),  # 19:59 ET
            _tick("SPY", datetime(2026, 4, 21, 0, 30, tzinfo=UTC), 452.0),  # 20:30 ET
        ],
    )

    df = (
        Resampler(root)
        .resample(
            "SPY",
            datetime(2026, 4, 19, tzinfo=UTC),
            datetime(2026, 4, 22, tzinfo=UTC),
            "1d",
        )
        .sort("bucket_start")
    )

    assert _et(df) == ["2026-04-20 04:00"]
    assert df["close"].to_list() == [452.0]


def test_daily_bucket_puts_pre_0400_on_the_previous_session(tmp_path: Path) -> None:
    """Matches aggregation.session.PRE_SESSION_CUTOFF_LOCAL.

    If the rollup used a different boundary from the session logic, the two
    would disagree about which day a bar belongs to.
    """
    root = tmp_path / "ticks"
    _write_ticks(
        root,
        [
            _tick("SPY", datetime(2026, 4, 21, 7, 59, tzinfo=UTC), 450.0),  # 03:59 ET
            _tick("SPY", datetime(2026, 4, 21, 8, 0, tzinfo=UTC), 451.0),  # 04:00 ET
        ],
    )

    df = (
        Resampler(root)
        .resample(
            "SPY",
            datetime(2026, 4, 19, tzinfo=UTC),
            datetime(2026, 4, 23, tzinfo=UTC),
            "1d",
        )
        .sort("bucket_start")
    )

    assert _et(df) == ["2026-04-20 04:00", "2026-04-21 04:00"]


@pytest.mark.parametrize(
    ("day", "note"),
    [("2026-03-06", "EST, before DST"), ("2026-03-10", "EDT, after DST")],
)
def test_hourly_grid_survives_dst(tmp_path: Path, day: str, note: str) -> None:
    """The grid is laid out in ET wall-clock, so it does not drift twice a year.

    A fixed UTC origin would slide by an hour against the session each time
    the offset changes.
    """
    root = tmp_path / "ticks"
    open_et = datetime.fromisoformat(f"{day}T10:30:00").replace(tzinfo=_ET)
    _write_ticks(root, [_tick("SPY", open_et.astimezone(UTC), 450.0)])

    df = Resampler(root).resample(
        "SPY",
        open_et.astimezone(UTC) - timedelta(days=1),
        open_et.astimezone(UTC) + timedelta(days=1),
        "1h",
    )

    assert _et(df) == [f"{day} 10:30"], note
