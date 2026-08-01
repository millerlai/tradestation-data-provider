"""`publisher_version` on disk, and the pre-existing partitions that lack it.

The column records which publisher convention produced `volume` — wire v4's
`pv`, see contract/v4/envelope.md. Every partition written before it existed
simply has no such column, and none of it can be back-filled: nothing on disk
says which convention those rows were written under. So the whole point of
these tests is the *mixed* store, which is the only state this repo will ever
actually be in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick
from tradestation_data.storage import BarWriter, HistoryStore, TickWriter
from tradestation_data.storage.resampler import Resampler

# 09:30 ET on a weekday.
T0 = datetime(2026, 4, 20, 13, 30, 0, tzinfo=UTC)

# Every field BAR_SCHEMA had before `publisher_version` was appended. Written
# out longhand rather than derived from BAR_SCHEMA: a test that builds the old
# shape by removing the new field from the new schema would keep passing if the
# field were removed again, which is exactly what it is here to notice.
_LEGACY_BAR_SCHEMA = pa.schema(
    [
        pa.field("bucket_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("bucket_start_et", pa.timestamp("us", tz="America/New_York"), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.int64(), nullable=False),
        pa.field("tick_count", pa.int32(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
    ]
)


def _tick(ts: datetime, price: float, *, pv: int | None) -> Tick:
    return Tick(
        symbol="SPY",
        timestamp=ts,
        price=price,
        volume=100,
        bid=None,
        ask=None,
        tick_count=0,
        source="tradestation_el",
        publisher_version=pv,
    )


def _write_legacy_bar_partition(root: Path, tf: str, day: str, bucket: datetime) -> Path:
    """A Tier-3 partition in the pre-`publisher_version` shape."""
    out = root / "bars" / f"timeframe={tf}" / "symbol=SPY" / f"date={day}" / "bars.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pydict(
        {
            "bucket_start": [bucket],
            "bucket_start_et": [bucket.astimezone(tz=None)],
            "open": [1.0],
            "high": [2.0],
            "low": [0.5],
            "close": [1.5],
            "volume": [10],
            "tick_count": [3],
            "source": ["derived:ticks"],
        },
        schema=_LEGACY_BAR_SCHEMA,
    )
    pq.write_table(table, out, compression="zstd")
    return out


def test_a_partition_without_the_column_still_reads(tmp_path: Path) -> None:
    """union_by_name, or the whole column vanishes from a mixed glob.

    read_parquet defaults to the FIRST file's schema and silently drops what
    the others add — no error, no warning. One legacy partition in range would
    therefore erase `publisher_version` for every row of the query, including
    the rows that do carry it.
    """
    legacy_day = (T0 - timedelta(days=1)).date().isoformat()
    _write_legacy_bar_partition(tmp_path, "5m", legacy_day, T0 - timedelta(days=1))

    with BarWriter(tmp_path / "bars") as w:
        w.write(
            Bar(
                symbol="SPY",
                bucket_start=T0,
                open=1.0,
                high=2.0,
                low=0.5,
                close=1.5,
                volume=10,
                tick_count=3,
                source="derived:ticks",
                timeframe="5m",
                publisher_version=1,
            )
        )

    store = HistoryStore(tmp_path)
    df = store.load_cached_bars("SPY", T0 - timedelta(days=2), T0 + timedelta(days=1), "5m")
    assert df is not None
    assert df.height == 2
    assert sorted(df["publisher_version"].to_list(), key=lambda v: (v is not None, v)) == [None, 1]


def test_a_legacy_partition_still_accepts_new_buckets(tmp_path: Path) -> None:
    """`.cast(BAR_SCHEMA)` is a strict field-list comparison.

    `_merge_with_existing_partition` casts what it reads off disk and treats a
    failure as "not our file, leave it alone" — the right answer for a file
    written by another producer, and the wrong one for our own file that
    merely predates a column. Without padding it first, every partition
    written before the field existed silently stops accepting new rows: the
    count on disk never changes and nothing raises.

    Goes through `load_bars`, not `rebuild_bar_cache`: rebuild deletes the
    partition before rebuilding it, so it never reaches the merge at all.
    """
    day = T0.date().isoformat()
    path = _write_legacy_bar_partition(tmp_path, "5m", day, T0)
    assert pq.ParquetFile(path).read().num_rows == 1

    # A bucket the legacy partition does not hold, on the same ET day.
    later = T0 + timedelta(minutes=30)
    _populate(
        tmp_path, [_tick(later + timedelta(seconds=i * 10), 450.0 + i, pv=1) for i in range(3)]
    )

    HistoryStore(tmp_path).load_bars("SPY", later, later + timedelta(minutes=5), "5m")

    after = pq.ParquetFile(path).read()
    assert "publisher_version" in after.column_names
    assert after.num_rows > 1, "the legacy partition stopped accepting rows"
    got = pl.from_arrow(after)
    assert isinstance(got, pl.DataFrame)
    assert later in got["bucket_start"].to_list()


def test_resampling_a_store_with_no_column_anywhere_does_not_raise(tmp_path: Path) -> None:
    """`union_by_name` unions what exists; it cannot invent a column.

    Naming `publisher_version` outright over a tick tree written entirely
    before the field raises "Referenced column not found" and takes the whole
    resample down — which is every store that existed before this change.
    """
    out = tmp_path / "ticks" / "symbol=SPY" / f"date={T0.date().isoformat()}" / "ticks.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pydict(
            {
                "timestamp": [T0, T0 + timedelta(seconds=30)],
                "timestamp_et": [T0.astimezone(tz=None)] * 2,
                "price": [450.0, 451.0],
                "volume": [100, 100],
                "bid": [None, None],
                "ask": [None, None],
                "tick_count": [0, 0],
                "source": ["tradestation_el"] * 2,
            }
        ),
        out,
        compression="zstd",
    )

    df = Resampler(tmp_path / "ticks").resample("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert df.height == 1
    assert df["publisher_version"].to_list() == [None]


def _populate(root: Path, ticks: list[Tick]) -> None:
    with TickWriter(root / "ticks") as w:
        for t in ticks:
            w.write(t)


def test_the_wire_value_survives_all_the_way_to_parquet(tmp_path: Path) -> None:
    """Tier 1 and Tier 3 both keep it; a derived bar takes the last tick's.

    This is the only thing the column is for: without it a partition holding
    up-tick volume and one holding the total are byte-for-byte the same shape,
    and no reader can tell which it has.
    """
    _populate(tmp_path, [_tick(T0 + timedelta(seconds=i * 10), 450.0 + i, pv=1) for i in range(4)])

    tick_file = (
        tmp_path / "ticks" / "symbol=SPY" / f"date={T0.date().isoformat()}" / "ticks.parquet"
    )
    assert pl.read_parquet(tick_file)["publisher_version"].to_list() == [1, 1, 1, 1]

    store = HistoryStore(tmp_path)
    bars = store.load_bars("SPY", T0, T0 + timedelta(hours=1), "5m")
    assert bars.height >= 1
    assert set(bars["publisher_version"].to_list()) == {1}


@pytest.mark.parametrize("pv", [None, 0, 1])
def test_undeclared_and_absent_stay_distinct_on_disk(tmp_path: Path, pv: int | None) -> None:
    """0 and null are different facts and must not collapse into each other.

    0 is a publisher that declared itself undeclared (wire v4, old .ELD);
    null is a row written where nothing could declare at all. A reader that
    saw them as one could not tell "this feed told me it is old" from
    "this row predates the question".
    """
    _populate(tmp_path, [_tick(T0, 450.0, pv=pv)])
    tick_file = (
        tmp_path / "ticks" / "symbol=SPY" / f"date={T0.date().isoformat()}" / "ticks.parquet"
    )
    assert pl.read_parquet(tick_file)["publisher_version"].to_list() == [pv]
