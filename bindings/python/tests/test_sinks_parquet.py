from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from tradestation_data.domain.bar import Bar
from tradestation_data.sinks.parquet import ParquetBarSink


def _bar() -> Bar:
    return Bar(
        symbol="SPY",
        bar_time=datetime(2026, 4, 18, 13, 30, tzinfo=UTC),
        open=450.1,
        high=450.5,
        low=449.9,
        close=450.3,
        el_volume=1000,
        el_ticks=2000,
        el_upticks=1000,
        el_downticks=1000,
        el_open_interest=0,
        bar_type=1,
        bar_interval=1,
        category=2,
    )


def test_parquet_bar_sink_writes_on_close(tmp_path: Path) -> None:
    sink = ParquetBarSink(name="bars", root=tmp_path)
    sink.on_bar(_bar())
    sink.close()

    out = tmp_path / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"
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
    out = tmp_path / "bartype=1" / "interval=1" / "symbol=SPY" / "date=2026-04-18" / "bars.parquet"

    assert sink.should_flush() is False  # nothing buffered yet
    sink.on_bar(_bar())
    assert sink.should_flush() is False  # below both triggers
    assert not out.exists()

    sink.flush()
    assert out.exists()  # written, but the footer only lands on close
    assert sink.should_flush() is False

    sink.close()
    assert pq.read_table(out).num_rows == 1
