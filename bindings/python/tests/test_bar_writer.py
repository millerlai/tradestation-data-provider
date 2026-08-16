from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.storage import BarWriter, bar_writer


def _bar(symbol: str, bucket: datetime, close: float, *, el_volume: int = 100) -> Bar:
    return Bar(
        symbol=symbol,
        bar_time=bucket,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        el_volume=el_volume,
        # Five values no two of which are equal, and none of which is the sum
        # or double of any other. Derivable numbers make a column swap
        # invisible: with el_volume == el_upticks and el_ticks == the sum, a
        # writer that transposed two of these would round-trip byte-identical
        # and every assertion here would still pass.
        el_ticks=el_volume * 2 + 7,
        el_upticks=el_volume + 3,
        el_downticks=el_volume + 5,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )


T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)


def test_writer_creates_timeframe_partitioned_file(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("SPY", T0 + timedelta(minutes=1), 450.5))

    expected = root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert expected.exists()

    table = pq.read_table(expected)
    assert table.num_rows == 2
    assert {
        "bar_time",
        "open",
        "high",
        "low",
        "close",
        "el_volume",
        "el_ticks",
        "el_upticks",
        "el_downticks",
        "el_open_interest",
    } <= set(table.column_names)
    assert table.column("close").to_pylist() == pytest.approx([450.0, 450.5])


def test_writer_partitions_by_symbol_and_date(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("QQQ", T0, 400.0))
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))

    for p in [
        root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet",
        root / "bartype=1" / "interval=1" / "symbol=QQQ" / "date=2026-04-18" / "bars.parquet",
        root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-19" / "bars.parquet",
    ]:
        assert p.exists(), p


def test_empty_bar_writes_zero_quantities(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(
            Bar(
                symbol="SPY",
                bar_time=T0,
                open=450.0,
                high=450.0,
                low=450.0,
                close=450.0,
                el_volume=0,
                el_ticks=0,
                el_upticks=0,
                el_downticks=0,
                el_open_interest=0,
                bar_type=1,
                bar_interval=1,
                category=2,
            )
        )
    path = root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    table = pq.read_table(path)
    for column in ("el_volume", "el_ticks", "el_upticks", "el_downticks", "el_open_interest"):
        assert table.column(column).to_pylist() == [0], column


def test_writer_close_is_idempotent(tmp_path: Path) -> None:
    writer = BarWriter(tmp_path / "bars")
    writer.write(_bar("SPY", T0, 450.0))
    writer.close()
    writer.close()  # must not raise


def test_write_after_close_raises(tmp_path: Path) -> None:
    writer = BarWriter(tmp_path / "bars")
    writer.close()
    with pytest.raises(RuntimeError):
        writer.write(_bar("SPY", T0, 450.0))


def test_writer_schema_includes_bar_time_et(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(_bar("SPY", T0, 450.0))

    path = root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    table = pq.read_table(path)
    assert "bar_time_et" in table.column_names
    et_series = table.column("bar_time_et").to_pylist()
    assert len(et_series) == 1
    et0 = et_series[0]
    assert et0.tzinfo is not None
    # 13:30 UTC on 2026-04-18 → 09:30 EDT (UTC-4).
    assert et0.hour == 9 and et0.minute == 30


def test_writer_partitions_by_et_date_not_utc(tmp_path: Path) -> None:
    """A bar at 2026-04-18 02:30 UTC (= 2026-04-17 22:30 ET) must land
    under the ET calendar date 2026-04-17, not the UTC-rolled 2026-04-18.
    After-hours sessions routinely straddle the UTC midnight; we want a
    single date= directory per trading session."""
    root = tmp_path / "bars"
    late_utc = datetime(2026, 4, 18, 2, 30, 0, tzinfo=UTC)  # 22:30 ET prev day
    with BarWriter(root) as writer:
        writer.write(_bar("SPY", late_utc, 450.0))

    et_partition = (
        root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-17" / "bars.parquet"
    )
    utc_partition = (
        root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    )
    assert et_partition.exists()
    assert not utc_partition.exists()


def test_burst_of_bars_becomes_one_row_group(tmp_path: Path) -> None:
    """One row group per bar was costing ~25x the bytes.

    Measured on a real session before this: 78 five-minute bars occupied
    145,977 bytes as 78 row groups, 5,936 as one. A chart reload delivers
    a whole session at once, which is exactly when it hurt most.

    today_et is pinned to the bars' own day so this stays on the streaming
    path: a past `date=` partition now rewrites, and pq.write_table always
    produces one row group regardless of buffering, which would let this
    pass even with the buffer removed entirely.
    """
    root = tmp_path / "bars"
    with BarWriter(root, today_et=lambda: date(2026, 4, 18)) as writer:
        for i in range(60):
            writer.write(_bar("SPY", T0 + timedelta(minutes=i), 450.0 + i))

    path = root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    meta = pq.ParquetFile(path).metadata
    assert meta.num_rows == 60
    assert meta.num_row_groups == 1


def test_finished_day_is_readable_before_close(tmp_path: Path) -> None:
    """The bug that hid 499 daily bars: a ParquetWriter held open leaves
    its file without a footer, so every reader rejects it. Rolling into a
    new day must seal the old one."""
    root = tmp_path / "bars"
    writer = BarWriter(root)
    try:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("SPY", T0 + timedelta(minutes=1), 450.5))
        # A bar for the next day: day one can never receive another.
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))

        day_one = (
            root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
        )
        table = pq.read_table(day_one)  # would raise without the footer
        assert table.num_rows == 2
    finally:
        writer.close()


def test_late_bar_for_a_sealed_past_day_is_merged(tmp_path: Path, caplog) -> None:
    """A sealed past day no longer means "reopening would truncate it" — it
    means "its writer, if it ever had one, is closed". A late bar for it now
    lands through `_rewrite` instead of being refused. The tick-chart shape
    this test used to pin lives on as
    test_late_bar_for_a_sealed_tick_day_is_still_refused.
    """
    root = tmp_path / "bars"
    writer = BarWriter(root, today_et=lambda: date(2026, 4, 19))
    try:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))  # seals 04-18
        with caplog.at_level("WARNING"):
            writer.write(_bar("SPY", T0 + timedelta(minutes=1), 450.5))
        assert not any("bar_partition_sealed" in r.message for r in caplog.records)
    finally:
        writer.close()

    day_one = root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert pq.read_table(day_one).num_rows == 2


def test_sealing_is_per_symbol_and_timeframe(tmp_path: Path) -> None:
    """A new day on one series must not close another series' open file."""
    root = tmp_path / "bars"
    writer = BarWriter(root)
    try:
        writer.write(_bar("QQQ", T0, 400.0))
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))
        writer.write(_bar("QQQ", T0 + timedelta(minutes=1), 400.5))  # still open
    finally:
        writer.close()

    qqq = root / "bartype=1" / "interval=1" / "symbol=QQQ" / "date=2026-04-18" / "bars.parquet"
    assert pq.read_table(qqq).num_rows == 2


def test_should_flush_triggers_on_buffered_count(tmp_path: Path) -> None:
    writer = BarWriter(tmp_path / "bars", max_buffered_bars=3, max_flush_seconds=3600)
    assert writer.should_flush() is False
    for i in range(2):
        writer.write(_bar("SPY", T0 + timedelta(minutes=i), 450.0))
    assert writer.should_flush() is False
    writer.write(_bar("SPY", T0 + timedelta(minutes=2), 450.0))
    assert writer.should_flush() is True

    assert writer.flush() == 3
    assert writer.should_flush() is False
    writer.close()


def test_writer_partitions_on_the_charts_own_words(tmp_path) -> None:
    """The point decides the partition, from BarType and BarInterval verbatim.

    There is no allow-list and no name-mapping in between, so a chart this
    binding has no word for — a 2-minute series, say — still files under its
    own pair instead of being refused or defaulted into someone else's.
    """
    from tradestation_data.domain.bar import Bar

    root = tmp_path / "bars"
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)

    def _point(bar_type: int, bar_interval: int) -> Bar:
        return Bar(
            symbol="SPY",
            bar_time=t,
            bar_type=bar_type,
            bar_interval=bar_interval,
            category=2,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            el_volume=10,
            el_ticks=20,
            el_upticks=10,
            el_downticks=10,
            el_open_interest=0,
        )

    with BarWriter(root) as w:
        w.write(_point(1, 1))
        w.write(_point(1, 5))
        w.write(_point(1, 2))  # no wire name ever existed for this one
        w.write(_point(2, 1))

    written = sorted(
        str(p.relative_to(root).parent).replace("\\", "/") for p in root.rglob("bars.parquet")
    )
    assert written == [
        "bartype=1/interval=1/symbol=SPY/date=2026-04-20",
        "bartype=1/interval=2/symbol=SPY/date=2026-04-20",
        "bartype=1/interval=5/symbol=SPY/date=2026-04-20",
        "bartype=2/interval=1/symbol=SPY",  # daily: no date= level
    ]


# ---- single-file timeframes (1d) ------------------------------------


def _daily(bucket: datetime, close: float) -> Bar:
    return Bar(
        symbol="SPY",
        bar_time=bucket,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        el_volume=1_000,
        el_ticks=50,
        el_upticks=1_000,
        el_downticks=0,
        el_open_interest=0,
        bar_type=2,
        bar_interval=1,
        category=2,
    )


# 04:00 ET on three consecutive sessions — the 1d grid anchor (semantics §2.2).
D1 = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)
D2 = D1 + timedelta(days=1)
D3 = D1 + timedelta(days=2)


def test_daily_bars_share_one_file_per_symbol(tmp_path: Path) -> None:
    """A day partition of daily bars holds one row and costs ~2.9 KB of
    schema and footer to carry about 60 bytes of it."""
    root = tmp_path / "bars"
    with BarWriter(root) as w:
        for i, bucket in enumerate((D1, D2, D3)):
            w.write(_daily(bucket, 450.0 + i))

    flat = root / "bartype=2" / "interval=1" / "symbol=SPY" / "bars.parquet"
    assert flat.exists()
    assert not list((root / "bartype=2" / "interval=1").glob("symbol=SPY/date=*"))
    meta = pq.ParquetFile(flat).metadata
    assert meta.num_rows == 3
    assert meta.num_row_groups == 1


def test_daily_file_is_readable_after_every_flush(tmp_path: Path) -> None:
    """Rewritten whole means complete: no waiting for close() to get a
    footer, which is the trap the date= layout fell into."""
    root = tmp_path / "bars"
    writer = BarWriter(root)
    try:
        writer.write(_daily(D1, 450.0))
        writer.flush()
        flat = root / "bartype=2" / "interval=1" / "symbol=SPY" / "bars.parquet"
        assert pq.read_table(flat).num_rows == 1

        writer.write(_daily(D2, 451.0))
        writer.flush()
        assert pq.read_table(flat).num_rows == 2
    finally:
        writer.close()


def test_daily_rewrite_keeps_rows_written_by_an_earlier_process(tmp_path: Path) -> None:
    """The restart case. This file is the only copy of a native daily bar
    and pq.write_table truncates, so the rewrite has to read first."""
    root = tmp_path / "bars"
    with BarWriter(root) as w:
        w.write(_daily(D1, 450.0))
        w.write(_daily(D2, 451.0))

    with BarWriter(root) as w:  # a fresh run, as after a restart
        w.write(_daily(D3, 452.0))

    table = pq.read_table(root / "bartype=2" / "interval=1" / "symbol=SPY" / "bars.parquet")
    assert table.num_rows == 3
    assert table.column("close").to_pylist() == pytest.approx([450.0, 451.0, 452.0])


def test_daily_repeated_bucket_keeps_the_later_bar(tmp_path: Path) -> None:
    """A chart reload re-sends days we already have. They must land as one
    row each, and the fresher copy wins — TradeStation may have adjusted it."""
    root = tmp_path / "bars"
    with BarWriter(root) as w:
        w.write(_daily(D1, 450.0))
        w.write(_daily(D2, 451.0))
    with BarWriter(root) as w:
        w.write(_daily(D1, 999.0))  # same bucket, re-exported

    table = pq.read_table(root / "bartype=2" / "interval=1" / "symbol=SPY" / "bars.parquet")
    assert table.num_rows == 2
    assert table.column("close").to_pylist() == pytest.approx([999.0, 451.0])


def test_daily_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as w:
        w.write(_daily(D1, 450.0))
    assert not list(root.rglob("*.tmp"))


def test_legacy_schema_partition_does_not_starve_the_other_partitions(tmp_path: Path) -> None:
    """One unwritable file must cost one series, not the whole run.

    A store written by a release before the el_* columns is the realistic
    case: `_rewrite` reads it back, the shapes do not match, and before this
    was isolated the raise aborted `flush()` for every partition ordered
    after it. The buffer is only cleared on success, so the same exception
    repeated every cycle while memory grew, and nothing raised anywhere an
    operator was watching — the heartbeat just stopped counting bars.
    """
    import pyarrow as pa

    root = tmp_path / "bars"
    legacy = root / "bartype=2" / "interval=1" / "symbol=SPY" / "bars.parquet"
    legacy.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "bar_time": pa.array([D1], type=pa.timestamp("us", tz="UTC")),
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [100],
                "tick_count": [5],
                "source": ["tradestation_el"],
            }
        ),
        legacy,
    )

    with BarWriter(root) as w:
        w.write(_daily(D1, 450.0))  # lands on the legacy 1d file
        w.write(_bar("SPY", T0, 450.0))  # a different partition entirely

    # The 1d partition is given up on, but the 1m one is written.
    intraday = root / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert intraday.exists(), "an unrelated partition was starved by the bad one"
    assert pq.read_table(intraday).num_rows == 1


def test_legacy_schema_partition_is_reported_with_the_path_and_the_fix(
    tmp_path: Path, caplog
) -> None:
    """The operator's only signal, so it has to name the file and the way out."""
    import pyarrow as pa

    root = tmp_path / "bars"
    legacy = root / "bartype=2" / "interval=1" / "symbol=SPY" / "bars.parquet"
    legacy.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "bar_time": pa.array([D1], type=pa.timestamp("us", tz="UTC")),
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [100],
                "tick_count": [5],
                "source": ["x"],
            }
        ),
        legacy,
    )

    with (
        caplog.at_level("ERROR", logger="tradestation_data.storage.bar_writer"),
        BarWriter(root) as w,
    ):
        w.write(_daily(D1, 450.0))
        w.write(_daily(D2, 451.0))  # poisoned now: must not re-report or re-raise

    errors = [r for r in caplog.records if r.message == "bar_partition_unwritable"]
    assert len(errors) == 1, "poisoned partition must report once, not once per flush"
    assert "el_volume" in errors[0].error
    assert str(legacy) == errors[0].path


def _five_min(day: int, i: int) -> Bar:
    t = datetime(2026, 7, day, 13, 30, tzinfo=UTC) + timedelta(minutes=5 * i)
    return Bar(
        symbol="SPY",
        bar_time=t,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        el_volume=10,
        el_ticks=27,
        el_upticks=13,
        el_downticks=15,
        el_open_interest=0,
        bar_type=1,
        bar_interval=5,
        category=2,
    )


def test_flush_seals_a_day_that_is_over(tmp_path: Path) -> None:
    """The newest day of a finished replay must not wait for close().

    A chart loaded with history publishes several days at once. Only the
    arrival of a LATER day sealed a partition, so the last day held an open
    pq.ParquetWriter — and a Parquet file has no footer until it is closed,
    so that day read as corrupt (or as nothing) for the whole run. Ctrl+C
    was the only thing that finished it.
    """
    writer = BarWriter(
        tmp_path,
        max_flush_seconds=0.0,
        today_et=lambda: date(2026, 8, 1),  # both days below are over
    )
    for day in (30, 31):
        for i in range(3):
            writer.write(_five_min(day, i))

    writer.flush()  # exactly what the runtime's flush loop calls — no close()

    for day in ("2026-07-30", "2026-07-31"):
        path = tmp_path / "bartype=1" / "interval=5" / "symbol=SPY" / f"date={day}" / "bars.parquet"
        assert pq.ParquetFile(path).metadata.num_rows == 3, day


def test_flush_leaves_today_open(tmp_path: Path) -> None:
    """Today keeps taking bars, so it must not be sealed.

    pq.ParquetWriter truncates on open, so a partition closed early cannot
    be resumed — sealing the live day would discard the rest of the session.
    """
    writer = BarWriter(
        tmp_path,
        max_flush_seconds=0.0,
        today_et=lambda: date(2026, 7, 31),
    )
    for i in range(3):
        writer.write(_five_min(31, i))
    writer.flush()

    path = tmp_path / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    assert path.exists()
    with pytest.raises(Exception):  # noqa: B017 — footerless, the arrow error is incidental
        pq.ParquetFile(path)

    # Still writable: the session continues and the later bars land.
    writer.write(_five_min(31, 3))
    writer.flush()
    writer.close()
    assert pq.ParquetFile(path).metadata.num_rows == 4


def test_should_flush_reports_a_day_that_is_over(tmp_path: Path) -> None:
    """With nothing buffered there is still work to do: the footer.

    should_flush() gates the runtime's flush loop, so returning False here
    would mean flush() is never called and the seal never happens.
    """
    writer = BarWriter(tmp_path, max_flush_seconds=0.0, today_et=lambda: date(2026, 7, 31))
    writer.write(_five_min(31, 0))
    writer.flush()
    assert writer.should_flush() is False  # today: still taking bars

    writer._today_et = lambda: date(2026, 8, 1)  # the day rolls over
    assert writer.should_flush() is True
    writer.flush()
    assert writer.should_flush() is False  # sealed, so once only
    writer.close()


def test_a_replay_burst_is_not_sealed_out_from_under_itself(tmp_path: Path) -> None:
    """The wall clock alone must not seal a day the burst is still filling.

    A chart loaded with five days of history publishes all of them within
    seconds, and every one of those days is already over. Sealing on the date
    alone closes a partition mid-burst, and `write` refuses a sealed
    partition — so a readability problem would have become lost bars.

    The stream's own signal still seals: day 30 finishes the moment day 31
    appears, which is a fact about the data rather than about the clock.
    """
    writer = BarWriter(
        tmp_path,
        max_flush_seconds=3600.0,  # nothing has been quiet that long
        today_et=lambda: date(2026, 8, 1),  # both days below are in the past
    )
    for day in (30, 31):
        for i in range(3):
            writer.write(_five_min(day, i))

    assert writer.should_flush() is False  # a True here would seal mid-burst
    writer.flush()
    writer.write(_five_min(31, 3))  # the rest of the burst still lands
    writer.close()

    def rows(name: str) -> int:
        path = (
            tmp_path / "bartype=1" / "interval=5" / "symbol=SPY" / f"date={name}" / "bars.parquet"
        )
        return int(pq.ParquetFile(path).metadata.num_rows)

    assert rows("2026-07-31") == 4
    assert rows("2026-07-30") == 3  # stream-sealed when day 31 arrived


# ---- past date= partitions rewrite instead of streaming ---------------


def _tick(bucket: datetime) -> Bar:
    return Bar(
        symbol="SPY",
        bar_time=bucket,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        el_volume=1,
        el_ticks=1,
        el_upticks=1,
        el_downticks=0,
        el_open_interest=0,
        bar_type=0,
        bar_interval=1,
        category=2,
    )


_QUOTE_BUCKET = datetime(2026, 7, 31, 13, 30, tzinfo=UTC)


def _quote_bar(
    bid: float | None, ask: float | None, close: float, *, ts: float | None = None
) -> Bar:
    return Bar(
        symbol="SPY",
        bar_time=_QUOTE_BUCKET,
        open=close - 0.1,
        high=close + 0.1,
        low=close - 0.1,
        close=close,
        el_volume=10,
        el_ticks=20,
        el_upticks=10,
        el_downticks=10,
        el_open_interest=0,
        bar_type=1,
        bar_interval=5,
        category=2,
        bid=bid,
        ask=ask,
        ts=ts,
    )


def test_republished_past_day_is_merged_not_truncated(tmp_path: Path) -> None:
    """The bug report's own reproduction. Truncation is cross-run — the
    streaming ParquetWriter only truncates on open — so this needs two
    BarWriters, the same shape as
    test_daily_rewrite_keeps_rows_written_by_an_earlier_process.
    """
    root = tmp_path / "bars"
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        for i in range(4):
            w.write(_five_min(31, i))

    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:  # a fresh run
        for i in (1, 2, 3):  # the reload's history omits the oldest bar
            w.write(_five_min(31, i))

    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 4
    assert table.column("bar_time").to_pylist()[0] == _five_min(31, 0).bar_time


def test_sealed_past_day_can_be_imported_again(tmp_path: Path, caplog) -> None:
    """The second symptom: before this, a past day could be imported only
    once per process — every republish after the elapsed-day seal was
    silently dropped by `bar_partition_sealed`, while the DLL still
    reported rc=0.
    """
    writer = BarWriter(tmp_path, max_flush_seconds=0.0, today_et=lambda: date(2026, 8, 1))
    try:
        for i in range(3):
            writer.write(_five_min(31, i))
        writer.flush()  # seals 2026-07-31: it is already over

        with caplog.at_level("WARNING"):
            writer.write(_five_min(31, 3))  # re-import after the seal
            writer.flush()
        assert not any("bar_partition_sealed" in r.message for r in caplog.records)
    finally:
        writer.close()

    path = tmp_path / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    assert pq.ParquetFile(path).metadata.num_rows == 4


def test_day_that_rolls_over_mid_run_keeps_its_streamed_rows(tmp_path: Path, caplog) -> None:
    """Pins the writer-close-before-rewrite step. Without it, a partition
    that started the run as "today" and then rolls into the past hits
    `_rewrite` with a footerless file still open underneath it and gets
    poisoned instead of merged.
    """
    writer = BarWriter(tmp_path, today_et=lambda: date(2026, 7, 31))
    try:
        for i in range(3):
            writer.write(_five_min(31, i))
        writer.flush()  # streaming path: the file exists but has no footer yet

        for i in (3, 4):
            writer.write(_five_min(31, i))  # left buffered, not yet flushed

        writer._today_et = lambda: date(2026, 8, 1)  # ET midnight passes
        with caplog.at_level("ERROR"):
            writer.flush()
        assert not any("bar_partition_unwritable" in r.message for r in caplog.records)
    finally:
        writer.close()

    path = tmp_path / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    assert pq.ParquetFile(path).metadata.num_rows == 5


def test_tick_partition_keeps_the_streaming_writer(tmp_path: Path) -> None:
    """A tick chart's row count per day has no upper bound, so a past day
    must never take the rewrite path. `flush()` alone must leave it
    footerless, same as any other streaming partition — proving it never
    reached `_rewrite`, which always leaves a complete file.
    """
    root = tmp_path / "bars"
    writer = BarWriter(root, today_et=lambda: date(2026, 8, 1))
    try:
        writer.write(_tick(datetime(2026, 7, 31, 13, 30, tzinfo=UTC)))
        writer.flush()

        path = root / "bartype=0" / "interval=1" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
        assert path.exists()
        with pytest.raises(Exception):  # noqa: B017 — footerless, the arrow error is incidental
            pq.ParquetFile(path)
    finally:
        writer.close()


def test_late_bar_for_a_sealed_tick_day_is_still_refused(tmp_path: Path, caplog) -> None:
    """The tick-chart half of what
    test_late_bar_for_a_sealed_day_does_not_truncate_it used to pin: a tick
    chart never rewrites, so the sealed-day refusal still applies even once
    the day is in the past.
    """
    root = tmp_path / "bars"
    writer = BarWriter(root, today_et=lambda: date(2026, 4, 19))
    try:
        writer.write(_tick(T0))
        writer.write(_tick(T0 + timedelta(days=1)))  # seals 04-18
        with caplog.at_level("WARNING"):
            writer.write(_tick(T0 + timedelta(minutes=1)))
        assert any("bar_partition_sealed" in r.message for r in caplog.records)
    finally:
        writer.close()

    day_one = root / "bartype=0" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert pq.read_table(day_one).num_rows == 1


def test_republishing_a_past_day_in_one_run_does_not_duplicate_rows(tmp_path: Path) -> None:
    """The third symptom from the report: before this, a same-run republish
    of an already-flushed past day was a streaming append, so every bar
    landed twice.
    """
    root = tmp_path / "bars"
    writer = BarWriter(root, today_et=lambda: date(2026, 8, 1))
    try:
        for i in range(3):
            writer.write(_five_min(31, i))
        writer.flush()
        for i in range(3):
            writer.write(_five_min(31, i))  # same bar_times, republished
        writer.flush()
    finally:
        writer.close()

    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    assert pq.ParquetFile(path).metadata.num_rows == 3


def test_the_coalesce_set_is_exactly_the_nullable_columns() -> None:
    """The merge rule's whole argument is "only these three can arrive null,
    because only these three are nullable". Adding a fourth nullable column
    to BAR_SCHEMA without deciding whether it coalesces would silently make
    that sentence false, and every other test here would still pass —
    sitting the two constants next to BAR_SCHEMA is a hint, not a guard.
    """
    nullable = {f.name for f in bar_writer.BAR_SCHEMA if f.nullable}
    assert nullable == set(bar_writer._COALESCE_COLUMNS)
    # _MERGE_COLUMNS must stay BAR_SCHEMA order minus bar_time: group_by
    # emits the key first, and the result is cast straight to BAR_SCHEMA.
    schema_names = [f.name for f in bar_writer.BAR_SCHEMA]
    assert schema_names[0] == "bar_time"
    assert schema_names[1:] == bar_writer._MERGE_COLUMNS


def test_republish_without_quotes_keeps_the_live_quote(tmp_path: Path) -> None:
    """Design §4: a republish carries no quote (historical replay never
    does), so bid/ask/ts must keep whatever the store already had rather
    than being overwritten by null.
    """
    root = tmp_path / "bars"
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        w.write(_quote_bar(bid=222.14, ask=222.15, close=1.5))
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        w.write(_quote_bar(bid=None, ask=None, close=9.0))  # replay shape

    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.column("close").to_pylist() == [9.0]
    assert table.column("bid").to_pylist() == [222.14]
    assert table.column("ask").to_pylist() == [222.15]


def test_republish_with_quotes_replaces_the_stored_quote(tmp_path: Path) -> None:
    """§4's other half, and proof of the "incoming wins" assumption
    `_rewrite` already relied on for every other column: when both sides
    carry a non-null quote, the republished one wins. Without this test,
    "later wins" for bid/ask is only an inference from `.last()`.
    """
    root = tmp_path / "bars"
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        w.write(_quote_bar(bid=222.14, ask=222.15, close=1.5))
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        w.write(_quote_bar(bid=100.0, ask=100.5, close=9.0))

    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.column("bid").to_pylist() == [100.0]
    assert table.column("ask").to_pylist() == [100.5]


def test_republish_without_ts_keeps_the_stored_ts(tmp_path: Path) -> None:
    """`ts` is in the coalesce set for the same reason bid/ask are: the
    DLL's receive clock is not re-derivable from a replay, and on a tick
    chart it is the only sub-minute ordering the stored rows have.

    Asserted on its own because every other Bar factory in this file leaves
    `ts` null on both sides of the merge, where coalescing and not
    coalescing produce the same answer — so dropping "ts" from
    _COALESCE_COLUMNS would otherwise pass the whole suite.
    """
    root = tmp_path / "bars"
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        w.write(_quote_bar(bid=222.14, ask=222.15, close=1.5, ts=1_754_000_000.5))
    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        w.write(_quote_bar(bid=None, ask=None, close=9.0, ts=None))  # replay shape

    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.column("ts").to_pylist() == [1_754_000_000.5]
    assert table.column("close").to_pylist() == [9.0]  # everything else still takes the new row


def test_a_transient_read_failure_does_not_destroy_the_stored_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that cannot be read right now is not a file that is not Parquet.

    Antivirus, a backup agent, or a concurrent HistoryStore read can make
    one open fail with a share violation while the bytes on disk are
    perfectly intact. Treating that like a footerless half-file — dropping
    the stored rows and writing only the current buffer — destroys data
    that was never damaged, and `_rewrite` is also the daily path, where
    that file is the only copy there is.

    So only ArrowInvalid ("not a Parquet file") may be treated as
    discardable. Anything else falls through to _flush_partition's poison
    path, which stops writing but leaves the file untouched.
    """
    root = tmp_path / "bars"
    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"

    with BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w:
        for i in range(4):
            w.write(_five_min(31, i))
    assert pq.read_table(path).num_rows == 4

    real_parquet_file = bar_writer.pq.ParquetFile
    opens = 0

    def flaky(*args: object, **kwargs: object) -> object:
        nonlocal opens
        opens += 1
        if opens == 1:
            raise PermissionError(13, "simulated share violation")
        return real_parquet_file(*args, **kwargs)

    monkeypatch.setattr(bar_writer.pq, "ParquetFile", flaky)

    writer = BarWriter(root, today_et=lambda: date(2026, 8, 1))
    try:
        writer.write(_five_min(31, 9))  # a bar the store does not have yet
        writer.flush()
    finally:
        writer.close()
    monkeypatch.undo()

    assert opens == 1, "the read was attempted exactly once"
    assert pq.read_table(path).num_rows == 4, "an intact file must survive a failed read"


def test_unreadable_past_partition_is_overwritten_not_poisoned(tmp_path: Path, caplog) -> None:
    """Design §5: a footerless half-file left by a hard kill must not
    poison the partition for the rest of the run — the streaming writer it
    replaces self-heals in the same situation, by truncating on open.
    """
    root = tmp_path / "bars"
    path = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a parquet file")

    writer = BarWriter(root, today_et=lambda: date(2026, 8, 1))
    try:
        with caplog.at_level("WARNING"):
            writer.write(_five_min(31, 0))
            writer.flush()
        assert any("bar_partition_unreadable_overwritten" in r.message for r in caplog.records)
        assert not any("bar_partition_unwritable" in r.message for r in caplog.records)

        writer.write(_five_min(31, 1))  # the partition must not be poisoned
        writer.flush()
    finally:
        writer.close()

    assert pq.read_table(path).num_rows == 2


def test_legacy_schema_partition_is_still_refused(tmp_path: Path, caplog) -> None:
    """§5's other half: a genuine schema mismatch must still raise and
    poison the partition, proving the try/except/else in `_rewrite` does
    not let `except Exception` swallow the schema check's `ValueError`.
    :389/:430 already cover this for the daily (bartype=2) layout; this is
    the intraday date= equivalent, which only reaches `_rewrite` once its
    day is in the past.
    """
    import pyarrow as pa

    root = tmp_path / "bars"
    legacy = root / "bartype=1" / "interval=5" / "symbol=SPY" / "date=2026-07-31" / "bars.parquet"
    legacy.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "bar_time": pa.array([_QUOTE_BUCKET], type=pa.timestamp("us", tz="UTC")),
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [100],
                "tick_count": [5],
                "source": ["tradestation_el"],
            }
        ),
        legacy,
    )

    with (
        caplog.at_level("ERROR", logger="tradestation_data.storage.bar_writer"),
        BarWriter(root, today_et=lambda: date(2026, 8, 1)) as w,
    ):
        w.write(_five_min(31, 0))

    errors = [r for r in caplog.records if r.message == "bar_partition_unwritable"]
    assert len(errors) == 1
    assert "el_volume" in errors[0].error
