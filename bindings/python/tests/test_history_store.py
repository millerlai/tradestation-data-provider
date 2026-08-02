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
from tradestation_data.storage import BarWriter, HistoryStore

T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)
# 04:00 ET on 2026-04-20 — the 1d grid anchor (contract/semantics.md §2.2).
D0 = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)


def _bar(
    symbol: str,
    bucket: datetime,
    close: float,
    *,
    bar_type: int = 1,
    bar_interval: int = 5,
) -> Bar:
    return Bar(
        symbol=symbol,
        bar_time=bucket,
        bar_type=bar_type,
        bar_interval=bar_interval,
        category=2,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        el_volume=10,
        el_ticks=20,
        el_upticks=10,
        el_downticks=10,
        el_open_interest=0,
    )


def _populate_bars(root: Path, bars: list[Bar]) -> None:
    with BarWriter(root / "bars") as w:
        for b in bars:
            w.write(b)


# ---- the glob finds the partition, the window filters it -------------------


def test_load_bars_returns_rows_in_window(tmp_path: Path) -> None:
    _populate_bars(
        tmp_path,
        [
            _bar("SPY", T0, 450.0),
            _bar("SPY", T0 + timedelta(minutes=5), 450.5),
            _bar("SPY", T0 + timedelta(minutes=30), 451.0),
        ],
    )
    df = HistoryStore(tmp_path).load_bars(
        "SPY", T0, T0 + timedelta(minutes=10), bar_type=1, bar_interval=5
    )
    assert df.height == 2
    assert df.select("close").to_series().to_list() == pytest.approx([450.0, 450.5])


def test_load_bars_only_answers_the_timeframe_it_was_asked_for(tmp_path: Path) -> None:
    """`timeframe=` is a directory, so reading the wrong one is silent."""
    _populate_bars(tmp_path, [_bar("SPY", T0, 450.0, bar_interval=5)])
    store = HistoryStore(tmp_path)
    assert (
        store.load_bars("SPY", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=5).height == 1
    )
    assert (
        store.load_bars("SPY", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=15).height == 0
    )


def test_an_interval_with_no_wire_name_is_stored_and_readable(tmp_path: Path) -> None:
    """A 2-minute chart used to publish nothing at all.

    The DLL mapped BarType/BarInterval to a timeframe string and returned -5
    for any pair it could not name, so the data never reached the wire; the
    store then refused the same names on the read side. Both allow-lists are
    gone — the pair is stored as sent and read back by the same pair.
    """
    _populate_bars(tmp_path, [_bar("SPY", T0, 450.0, bar_interval=2)])

    out = HistoryStore(tmp_path).load_bars(
        "SPY", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=2
    )
    assert out.height == 1
    assert out["close"][0] == 450.0


def test_daily_empty_answer_matches_the_daily_hit(tmp_path: Path) -> None:
    """`1d` returns through its own branch, and its layout has no `date=` level.

    A read that hits the single file carries the `timeframe` hive key but not
    `date`, so the empty answer has to match that exactly — otherwise the same
    stacking that works for intraday raises on the one timeframe whose layout
    differs.
    """
    _populate_bars(tmp_path, [_bar("SPY", D0, 450.0, bar_type=2, bar_interval=1)])
    store = HistoryStore(tmp_path)
    populated = store.load_bars(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 24, tzinfo=UTC),
        bar_type=2,
        bar_interval=1,
    )
    quiet = store.load_bars(
        "SPY",
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 5, tzinfo=UTC),
        bar_type=2,
        bar_interval=1,
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
        [
            _bar("SPY", D0 + timedelta(days=i), 450.0 + i, bar_type=2, bar_interval=1)
            for i in range(3)
        ],
    )
    out = HistoryStore(tmp_path).load_bars(
        "SPY",
        datetime(2026, 4, 19, tzinfo=UTC),
        datetime(2026, 4, 24, tzinfo=UTC),
        bar_type=2,
        bar_interval=1,
    )
    assert out["close"].to_list() == [450.0, 451.0, 452.0]


def test_sealed_day_is_readable_while_the_writer_holds_today_open(tmp_path: Path) -> None:
    """The promise BarWriter's docstring makes, which the read side broke.

    The writer keeps a ParquetWriter open on the current day, so that file
    has no footer. `_read` used to glob every `date=` partition and filter on
    `bar_time`, a column *inside* each file — so polars had to open all
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
        got = HistoryStore(root).load_bars(
            "SPY", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=5
        )
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
            got = HistoryStore(root).load_bars(
                "SPY", T0, T0 + timedelta(days=2), bar_type=1, bar_interval=5
            )

        assert got.height == 1, "the sealed day should still come back"
        assert any(r.message == "history_partition_unreadable_skipped" for r in caplog.records), (
            "skipping a partition must be reported, never silent"
        )
    finally:
        writer.close()


def test_imputation_output_root_is_refused_not_read_as_collected_data(
    tmp_path: Path,
) -> None:
    """The guard CHANGELOG.md and imputation_parquet.py both already claimed.

    Imputed bars are flat O=H=L=C with all five quantities zero. Read back as
    ordinary rows they are indistinguishable from published ones to any
    consumer selecting OHLC, which is exactly why imputed output was given a
    schema of its own -- and that schema is what makes the refusal possible.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from tradestation_data.storage.bar_writer import BAR_SCHEMA

    out = tmp_path / "bars" / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18"
    out.mkdir(parents=True)
    row = {
        "bar_time": [T0],
        "bar_time_et": [T0],
        "open": [450.0],
        "high": [450.0],
        "low": [450.0],
        "close": [450.0],
        "el_volume": [0],
        "el_ticks": [0],
        "el_upticks": [0],
        "el_downticks": [0],
        "el_open_interest": [0],
        "category": [2],
        "bid": [None],
        "ask": [None],
        "ts": [None],
        "imputed": [True],
    }
    pq.write_table(
        pa.Table.from_pydict(row, schema=BAR_SCHEMA.append(pa.field("imputed", pa.bool_()))),
        out / "bars.parquet",
    )

    with pytest.raises(ValueError, match="imputation output root"):
        HistoryStore(tmp_path).load_bars(
            "SPY", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=1
        )


def test_populated_and_empty_answers_have_identical_columns(tmp_path: Path) -> None:
    """Width parity is what lets a caller stack a symbol loop with pl.concat.

    Before the column projection, a populated day returned whatever the file
    held and a quiet day returned BAR_SCHEMA, so the two disagreed the moment
    a file carried anything extra.
    """
    _populate_bars(tmp_path, [_bar("SPY", T0, 450.0)])
    store = HistoryStore(tmp_path)
    populated = store.load_bars("SPY", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=5)
    never = store.load_bars("NOSUCH", T0, T0 + timedelta(hours=1), bar_type=1, bar_interval=5)
    assert populated.columns == never.columns
    assert populated.schema == never.schema
