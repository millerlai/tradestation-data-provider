"""`HistoryStore` is a reader and only a reader.

Every test here asks a question of what is on disk. None of them expect an
answer the store had to compute: there is no resampler, no bar cache and no
coverage bookkeeping left to exercise, because a bar that TradeStation did not
publish is a bar that does not exist. The one behaviour worth pinning hardest
is the negative — see `test_load_bars_never_derives_from_ticks`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.storage import BarWriter, HistoryStore, TickWriter

T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)
# 04:00 ET on 2026-04-20 — the 1d grid anchor (contract/semantics.md §2.2).
D0 = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)


def _tick(symbol: str, ts: datetime, price: float, *, el_volume: int = 100) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        el_volume=el_volume,
        el_ticks=el_volume * 2,
        el_upticks=el_volume,
        el_downticks=el_volume,
        el_open_interest=0,
        bid=None,
        ask=None,
    )


def _bar(symbol: str, bucket: datetime, close: float, *, timeframe: str = "5m") -> Bar:
    return Bar(
        symbol=symbol,
        bucket_start=bucket,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        el_volume=10,
        el_ticks=20,
        el_upticks=10,
        el_downticks=10,
        el_open_interest=0,
        timeframe=timeframe,
    )


def _populate_ticks(root: Path, ticks: list[Tick]) -> None:
    with TickWriter(root / "ticks") as w:
        for t in ticks:
            w.write(t)


def _populate_bars(root: Path, bars: list[Bar]) -> None:
    with BarWriter(root / "bars") as w:
        for b in bars:
            w.write(b)


# ---- the glob finds the partition, the window filters it -------------------


def test_load_ticks_returns_rows_in_window(tmp_path: Path) -> None:
    _populate_ticks(
        tmp_path,
        [
            _tick("SPY", T0 + timedelta(seconds=5), 450.0),
            _tick("SPY", T0 + timedelta(seconds=30), 450.5),
            _tick("SPY", T0 + timedelta(minutes=10), 451.0),
        ],
    )
    df = HistoryStore(tmp_path).load_ticks("SPY", T0, T0 + timedelta(minutes=5))
    assert df.height == 2
    assert df.select("price").to_series().to_list() == pytest.approx([450.0, 450.5])


def test_load_bars_returns_rows_in_window(tmp_path: Path) -> None:
    _populate_bars(
        tmp_path,
        [
            _bar("SPY", T0, 450.0),
            _bar("SPY", T0 + timedelta(minutes=5), 450.5),
            _bar("SPY", T0 + timedelta(minutes=30), 451.0),
        ],
    )
    df = HistoryStore(tmp_path).load_bars("SPY", T0, T0 + timedelta(minutes=10), "5m")
    assert df.height == 2
    assert df.select("close").to_series().to_list() == pytest.approx([450.0, 450.5])


def test_load_bars_only_answers_the_timeframe_it_was_asked_for(tmp_path: Path) -> None:
    """`timeframe=` is a directory, so reading the wrong one is silent."""
    _populate_bars(tmp_path, [_bar("SPY", T0, 450.0, timeframe="5m")])
    store = HistoryStore(tmp_path)
    assert store.load_bars("SPY", T0, T0 + timedelta(hours=1), "5m").height == 1
    assert store.load_bars("SPY", T0, T0 + timedelta(hours=1), "15m").height == 0


def test_unsupported_timeframe_is_refused_rather_than_globbed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        HistoryStore(tmp_path).load_bars("SPY", T0, T0 + timedelta(hours=1), "7m")


# ---- the store never computes what it was not given ------------------------


def test_load_bars_never_derives_from_ticks(tmp_path: Path) -> None:
    """A tick store is not a bar store, and asking must not make it one.

    This is the whole point of the read side being read-only. A derived bar
    is indistinguishable from a published one the moment it is persisted, so
    the answer to "no bars here" is zero rows — never a plausible rollup, and
    never a write.
    """
    _populate_ticks(
        tmp_path,
        [_tick("SPY", T0 + timedelta(seconds=i * 10), 450.0 + i) for i in range(30)],
    )
    store = HistoryStore(tmp_path)

    out = store.load_bars("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert out.height == 0
    # Nothing was written on the way out, either.
    assert not (tmp_path / "bars").exists()


# ---- empty and populated answers are the same shape ------------------------


def test_empty_and_populated_answers_share_one_schema(tmp_path: Path) -> None:
    """The three ways of having no data must be indistinguishable to a caller.

    "Never recorded", "recorded but quiet", and "has rows" differ only in
    height. When they differed in width or dtype, stacking a symbol loop with
    `pl.concat` raised ShapeError the first time one symbol had a quiet day,
    and `df["bucket_start_et"]` raised ColumnNotFoundError on the empty frame.
    """
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
    _populate_bars(tmp_path, [_bar("SPY", T0, 450.0)])
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

    A read that hits the single file carries the `timeframe` hive key but not
    `date`, so the empty answer has to match that exactly — otherwise the same
    stacking that works for intraday raises on the one timeframe whose layout
    differs.
    """
    _populate_bars(tmp_path, [_bar("SPY", D0, 450.0, timeframe="1d")])
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


# ---- layout and zone -------------------------------------------------------


def test_daily_reads_the_single_file_layout(tmp_path: Path) -> None:
    """BarWriter drops the date= level for 1d; the reader must follow it,
    or a glob that matches nothing reads as 'no data' rather than an error."""
    _populate_bars(
        tmp_path,
        [_bar("SPY", D0 + timedelta(days=i), 450.0 + i, timeframe="1d") for i in range(3)],
    )
    out = HistoryStore(tmp_path).load_bars(
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
    _populate_ticks(tmp_path, [_tick("SPY", T0 + timedelta(seconds=5), 450.0)])
    _populate_bars(tmp_path, [_bar("SPY", T0, 450.0), _bar("SPY", D0, 450.0, timeframe="1d")])
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


def test_sealed_day_is_readable_while_the_writer_holds_today_open(tmp_path: Path) -> None:
    """The promise BarWriter's docstring makes, which the read side broke.

    The writer keeps a ParquetWriter open on the current day, so that file
    has no footer. `_read` used to glob every `date=` partition and filter on
    `bucket_start`, a column *inside* each file — so polars had to open all
    of them, and one open partition made every read of that symbol raise,
    including reads of days that were sealed and complete.
    """
    root = tmp_path
    writer = BarWriter(root / "bars")
    try:
        writer.write(_bar("SPY", T0, 450.0))  # 2026-04-18
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))  # seals 04-18
        writer.flush()

        # 04-19 is still open and footerless; 04-18 is sealed and complete.
        got = HistoryStore(root).load_bars("SPY", T0, T0 + timedelta(hours=1), "5m")
        assert got.height == 1
        assert got["close"].to_list() == [450.0]
    finally:
        writer.close()


def test_range_reaching_into_the_open_day_answers_with_the_sealed_ones(
    tmp_path: Path, caplog
) -> None:
    """Asking for a range that includes the live session is not an error.

    §2.4 makes an ordinary question an ordinary answer. Returning the sealed
    days and naming what was skipped beats raising about magic bytes.
    """
    root = tmp_path
    writer = BarWriter(root / "bars")
    try:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))
        writer.flush()

        with caplog.at_level("WARNING", logger="tradestation_data.storage.history_store"):
            got = HistoryStore(root).load_bars("SPY", T0, T0 + timedelta(days=2), "5m")

        assert got.height == 1, "the sealed day should still come back"
        assert any(r.message == "history_partition_unreadable_skipped" for r in caplog.records), (
            "skipping a partition must be reported, never silent"
        )
    finally:
        writer.close()
