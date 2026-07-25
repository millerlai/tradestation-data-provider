from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.storage import BarWriter


def _bar(symbol: str, bucket: datetime, close: float, *, volume: int = 100) -> Bar:
    return Bar(
        symbol=symbol,
        bucket_start=bucket,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        volume=volume,
        tick_count=5,
        source="tradestation_el",
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
        "volume",
        "tick_count",
        "source",
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


def test_empty_bar_writes_zero_volume(tmp_path: Path) -> None:
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
                volume=0,
                tick_count=0,
                source="empty",
            )
        )
    path = root / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    table = pq.read_table(path)
    assert table.column("volume").to_pylist() == [0]
    assert table.column("tick_count").to_pylist() == [0]
    assert table.column("source").to_pylist() == ["empty"]


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
            volume=10,
            vwap=1.2,
            tick_count=3,
            source="tradestation_el",
            timeframe=tf,
        )

    with BarWriter(root, timeframe="1m") as w:
        w.write(_bar("1m"))
        w.write(_bar("5m"))
        w.write(_bar("1d"))

    written = sorted(p.parent.parent.parent.name for p in root.rglob("bars.parquet"))
    assert written == ["timeframe=1d", "timeframe=1m", "timeframe=5m"]
