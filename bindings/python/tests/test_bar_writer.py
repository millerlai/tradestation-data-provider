from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.storage import BarWriter


def _bar(symbol: str, bucket: datetime, close: float, *, el_volume: int = 100) -> Bar:
    return Bar(
        symbol=symbol,
        bucket_start=bucket,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        el_volume=el_volume,
        el_ticks=el_volume * 2,
        el_upticks=el_volume,
        el_downticks=el_volume,
        el_open_interest=0,
    )


T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)


def test_writer_creates_timeframe_partitioned_file(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("SPY", T0 + timedelta(minutes=1), 450.5))

    expected = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert expected.exists()

    table = pq.read_table(expected)
    assert table.num_rows == 2
    assert {
        "bucket_start",
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
        root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet",
        root / "timeframe=1m" / "symbol=QQQ" / "date=2026-04-18" / "bars.parquet",
        root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-19" / "bars.parquet",
    ]:
        assert p.exists(), p


def test_empty_bar_writes_zero_quantities(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(
            Bar(
                symbol="SPY",
                bucket_start=T0,
                open=450.0,
                high=450.0,
                low=450.0,
                close=450.0,
                el_volume=0,
                el_ticks=0,
                el_upticks=0,
                el_downticks=0,
                el_open_interest=0,
            )
        )
    path = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
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


def test_writer_schema_includes_bucket_start_et(tmp_path: Path) -> None:
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        writer.write(_bar("SPY", T0, 450.0))

    path = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    table = pq.read_table(path)
    assert "bucket_start_et" in table.column_names
    et_series = table.column("bucket_start_et").to_pylist()
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

    et_partition = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-17" / "bars.parquet"
    utc_partition = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert et_partition.exists()
    assert not utc_partition.exists()


def test_burst_of_bars_becomes_one_row_group(tmp_path: Path) -> None:
    """One row group per bar was costing ~25x the bytes.

    Measured on a real session before this: 78 five-minute bars occupied
    145,977 bytes as 78 row groups, 5,936 as one. A chart reload delivers
    a whole session at once, which is exactly when it hurt most.
    """
    root = tmp_path / "bars"
    with BarWriter(root) as writer:
        for i in range(60):
            writer.write(_bar("SPY", T0 + timedelta(minutes=i), 450.0 + i))

    path = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
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

        day_one = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
        table = pq.read_table(day_one)  # would raise without the footer
        assert table.num_rows == 2
    finally:
        writer.close()


def test_late_bar_for_a_sealed_day_does_not_truncate_it(tmp_path: Path, caplog) -> None:
    root = tmp_path / "bars"
    writer = BarWriter(root)
    try:
        writer.write(_bar("SPY", T0, 450.0))
        writer.write(_bar("SPY", T0 + timedelta(days=1), 451.0))
        with caplog.at_level("WARNING"):
            writer.write(_bar("SPY", T0 + timedelta(minutes=1), 450.5))
        assert any("bar_partition_sealed" in r.message for r in caplog.records)
    finally:
        writer.close()

    day_one = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert pq.read_table(day_one).num_rows == 1


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

    qqq = root / "timeframe=1m" / "symbol=QQQ" / "date=2026-04-18" / "bars.parquet"
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


def test_writer_partitions_on_the_bar_timeframe_not_its_own(tmp_path) -> None:
    """The bar decides the partition; the constructor arg is only a default.

    Routing on the writer's setting instead would file every interval under
    whatever it was configured with — the exact corruption `tf` exists to
    stop.
    """
    from tradestation_data.domain.bar import Bar

    root = tmp_path / "bars"
    t = datetime(2026, 4, 20, 13, 30, tzinfo=UTC)

    def _bar(tf: str) -> Bar:
        return Bar(
            symbol="SPY",
            bucket_start=t,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            el_volume=10,
            el_ticks=20,
            el_upticks=10,
            el_downticks=10,
            el_open_interest=0,
            timeframe=tf,
        )

    with BarWriter(root) as w:
        w.write(_bar("1m"))
        w.write(_bar("5m"))
        w.write(_bar("1d"))

    # 1d has no date= level, so its file sits one directory shallower.
    written = sorted(p.relative_to(root).parts[0] for p in root.rglob("bars.parquet"))
    assert written == ["timeframe=1d", "timeframe=1m", "timeframe=5m"]
    assert (root / "timeframe=1d" / "symbol=SPY" / "bars.parquet").exists()


# ---- single-file timeframes (1d) ------------------------------------


def _daily(bucket: datetime, close: float) -> Bar:
    return Bar(
        symbol="SPY",
        bucket_start=bucket,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        el_volume=1_000,
        el_ticks=50,
        el_upticks=1_000,
        el_downticks=0,
        el_open_interest=0,
        timeframe="1d",
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

    flat = root / "timeframe=1d" / "symbol=SPY" / "bars.parquet"
    assert flat.exists()
    assert not list((root / "timeframe=1d").glob("symbol=SPY/date=*"))
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
        flat = root / "timeframe=1d" / "symbol=SPY" / "bars.parquet"
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

    table = pq.read_table(root / "timeframe=1d" / "symbol=SPY" / "bars.parquet")
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

    table = pq.read_table(root / "timeframe=1d" / "symbol=SPY" / "bars.parquet")
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
    legacy = root / "timeframe=1d" / "symbol=SPY" / "bars.parquet"
    legacy.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "bucket_start": pa.array([D1], type=pa.timestamp("us", tz="UTC")),
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
    intraday = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert intraday.exists(), "an unrelated partition was starved by the bad one"
    assert pq.read_table(intraday).num_rows == 1


def test_legacy_schema_partition_is_reported_with_the_path_and_the_fix(
    tmp_path: Path, caplog
) -> None:
    """The operator's only signal, so it has to name the file and the way out."""
    import pyarrow as pa

    root = tmp_path / "bars"
    legacy = root / "timeframe=1d" / "symbol=SPY" / "bars.parquet"
    legacy.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "bucket_start": pa.array([D1], type=pa.timestamp("us", tz="UTC")),
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
