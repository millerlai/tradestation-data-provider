from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tradestation_data.domain.tick import Tick
from tradestation_data.storage import TickWriter


def _tick(
    symbol: str,
    ts: datetime,
    price: float,
    *,
    el_volume: int = 100,
    bid: float | None = None,
    ask: float | None = None,
) -> Tick:
    return Tick(
        symbol=symbol,
        timestamp=ts,
        price=price,
        el_volume=el_volume,
        el_ticks=el_volume * 2,
        el_upticks=el_volume,
        el_downticks=el_volume,
        el_open_interest=0,
        bid=bid,
        ask=ask,
    )


T0 = datetime(2026, 4, 18, 13, 30, 0, tzinfo=UTC)


def test_writer_creates_hive_partitioned_file(tmp_path: Path) -> None:
    writer = TickWriter(tmp_path / "ticks")
    writer.write(_tick("SPY", T0, 450.0, bid=449.99, ask=450.01))
    writer.write(_tick("SPY", T0 + timedelta(seconds=1), 450.1, bid=450.05, ask=450.15))
    writer.close()

    expected = tmp_path / "ticks" / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert expected.exists()

    table = pq.read_table(expected)
    assert table.num_rows == 2
    assert {
        "timestamp",
        "price",
        "el_volume",
        "el_ticks",
        "el_upticks",
        "el_downticks",
        "el_open_interest",
        "bid",
        "ask",
    } <= set(table.column_names)
    prices = table.column("price").to_pylist()
    assert prices == pytest.approx([450.0, 450.1])


def test_writer_separates_partitions_by_symbol_and_date(tmp_path: Path) -> None:
    root = tmp_path / "ticks"
    writer = TickWriter(root)
    writer.write(_tick("SPY", T0, 450.0))
    writer.write(_tick("QQQ", T0, 400.0))
    next_day = T0 + timedelta(days=1)
    writer.write(_tick("SPY", next_day, 451.0))
    writer.close()

    expected_paths = {
        root / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet",
        root / "symbol=QQQ" / "date=2026-04-18" / "ticks.parquet",
        root / "symbol=SPY" / "date=2026-04-19" / "ticks.parquet",
    }
    for p in expected_paths:
        assert p.exists(), p

    spy_day1 = pq.read_table(root / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet")
    spy_day2 = pq.read_table(root / "symbol=SPY" / "date=2026-04-19" / "ticks.parquet")
    assert spy_day1.num_rows == 1
    assert spy_day2.num_rows == 1
    assert spy_day1.column("price").to_pylist() == pytest.approx([450.0])
    assert spy_day2.column("price").to_pylist() == pytest.approx([451.0])


def test_multiple_flushes_append_to_same_file(tmp_path: Path) -> None:
    writer = TickWriter(tmp_path / "ticks")
    writer.write(_tick("SPY", T0, 450.0))
    assert writer.flush() == 1
    writer.write(_tick("SPY", T0 + timedelta(seconds=1), 450.2))
    writer.write(_tick("SPY", T0 + timedelta(seconds=2), 450.3))
    assert writer.flush() == 2
    writer.close()

    path = tmp_path / "ticks" / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 3
    assert table.column("price").to_pylist() == pytest.approx([450.0, 450.2, 450.3])


def test_should_flush_triggers_on_size(tmp_path: Path) -> None:
    writer = TickWriter(tmp_path / "ticks", max_buffered_ticks=3, max_flush_seconds=3600)
    for i in range(2):
        writer.write(_tick("SPY", T0 + timedelta(seconds=i), 450.0 + i))
    assert writer.should_flush() is False
    writer.write(_tick("SPY", T0 + timedelta(seconds=2), 452.0))
    assert writer.should_flush() is True
    writer.close()


def test_index_symbol_writes_null_bid_ask(tmp_path: Path) -> None:
    writer = TickWriter(tmp_path / "ticks")
    writer.write(_tick("VXX", T0, 18.5, el_volume=0, bid=None, ask=None))
    writer.close()

    path = tmp_path / "ticks" / "symbol=VXX" / "date=2026-04-18" / "ticks.parquet"
    table = pq.read_table(path)
    assert table.column("bid").to_pylist() == [None]
    assert table.column("ask").to_pylist() == [None]
    assert table.column("el_volume").to_pylist() == [0]


def test_close_is_idempotent(tmp_path: Path) -> None:
    writer = TickWriter(tmp_path / "ticks")
    writer.write(_tick("SPY", T0, 450.0))
    writer.close()
    writer.close()  # must not raise


def test_write_after_close_raises(tmp_path: Path) -> None:
    writer = TickWriter(tmp_path / "ticks")
    writer.close()
    with pytest.raises(RuntimeError):
        writer.write(_tick("SPY", T0, 450.0))


def test_context_manager_flushes_on_exit(tmp_path: Path) -> None:
    root = tmp_path / "ticks"
    with TickWriter(root) as writer:
        writer.write(_tick("SPY", T0, 450.0))
    path = root / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert path.exists()
    assert pq.read_table(path).num_rows == 1


def test_should_flush_false_when_empty(tmp_path: Path) -> None:
    """Covers line 105."""
    writer = TickWriter(tmp_path / "ticks")
    assert writer.should_flush() is False
    writer.close()


def test_should_flush_false_when_oldest_monotonic_reset(tmp_path: Path) -> None:
    """Covers line 109: buffered_ticks>0 but _oldest_buffer_monotonic=None."""
    writer = TickWriter(tmp_path / "ticks", max_buffered_ticks=1000, max_flush_seconds=0.01)
    writer.write(_tick("SPY", T0, 450.0))
    writer._oldest_buffer_monotonic = None  # simulate reset without flush
    assert writer.should_flush() is False
    writer.close()


def test_finished_day_is_readable_before_close(tmp_path: Path) -> None:
    """A ParquetWriter held open leaves its file without a footer, so no
    reader will touch it. A tick for the next day means the old one can
    never grow again — seal it."""
    root = tmp_path / "ticks"
    writer = TickWriter(root, max_buffered_ticks=1000, max_flush_seconds=3600)
    try:
        writer.write(_tick("SPY", T0, 450.0))
        writer.write(_tick("SPY", T0 + timedelta(days=1), 451.0))

        day_one = root / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
        assert pq.read_table(day_one).num_rows == 1  # would raise without the footer
    finally:
        writer.close()


def test_late_tick_for_a_sealed_day_does_not_truncate_it(tmp_path: Path, caplog) -> None:
    root = tmp_path / "ticks"
    writer = TickWriter(root, max_buffered_ticks=1000, max_flush_seconds=3600)
    try:
        writer.write(_tick("SPY", T0, 450.0))
        writer.write(_tick("SPY", T0 + timedelta(days=1), 451.0))
        with caplog.at_level("WARNING"):
            writer.write(_tick("SPY", T0 + timedelta(seconds=1), 450.5))
        assert any("tick_partition_sealed" in r.message for r in caplog.records)
    finally:
        writer.close()

    day_one = root / "symbol=SPY" / "date=2026-04-18" / "ticks.parquet"
    assert pq.read_table(day_one).num_rows == 1
