from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.sinks.parquet import ParquetBarSink, ParquetTickSink


def _bar() -> Bar:
    return Bar(
        symbol="SPY",
        bucket_start=datetime(2026, 4, 18, 13, 30, tzinfo=UTC),
        open=450.1,
        high=450.5,
        low=449.9,
        close=450.3,
        volume=1000,
        tick_count=42,
        source="t",
    )


def _tick() -> Tick:
    return Tick(
        symbol="SPY",
        timestamp=datetime(2026, 4, 18, 13, 30, 15, tzinfo=UTC),
        price=450.3,
        volume=10,
        bid=450.29,
        ask=450.31,
        tick_count=1,
        source="t",
    )


def test_parquet_bar_sink_writes_on_close(tmp_path: Path) -> None:
    sink = ParquetBarSink(name="bars", root=tmp_path)
    sink.on_bar(_bar())
    sink.close()

    out = tmp_path / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
    assert out.exists()
    table = pq.read_table(out)
    assert table.num_rows == 1
    assert table.column("close").to_pylist() == [450.3]


def test_parquet_bar_sink_buffers_until_flush(tmp_path: Path) -> None:
    sink = ParquetBarSink(
        name="bars",
        root=tmp_path,
        max_buffered_bars=10,
        max_flush_seconds=3600,  # never expire by time during the test
    )
    out = tmp_path / "timeframe=1m" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"

    assert sink.should_flush() is False  # nothing buffered yet
    sink.on_bar(_bar())
    assert sink.should_flush() is False  # below both triggers
    assert not out.exists()

    sink.flush()
    assert out.exists()  # written, but the footer only lands on close
    assert sink.should_flush() is False

    sink.close()
    assert pq.read_table(out).num_rows == 1


def test_parquet_tick_sink_buffers_until_flush(tmp_path: Path) -> None:
    sink = ParquetTickSink(
        name="ticks",
        root=tmp_path,
        max_buffered_ticks=10,
        max_flush_seconds=3600,  # never expire by time during the test
    )
    for _ in range(3):
        sink.on_tick(_tick())
    # Buffered: not above threshold and not past time → should_flush False.
    assert sink.should_flush() is False
    out = tmp_path / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert not out.exists()

    sink.close()  # close flushes the buffer
    assert out.exists()
    table = pq.read_table(out)
    assert table.num_rows == 3


def test_parquet_tick_sink_should_flush_when_threshold_reached(tmp_path: Path) -> None:
    sink = ParquetTickSink(
        name="ticks",
        root=tmp_path,
        max_buffered_ticks=2,
        max_flush_seconds=3600,
    )
    sink.on_tick(_tick())
    assert sink.should_flush() is False
    sink.on_tick(_tick())
    assert sink.should_flush() is True
    sink.flush()
    out = tmp_path / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert out.exists()
    sink.close()
