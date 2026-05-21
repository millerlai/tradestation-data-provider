from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick

log = logging.getLogger(__name__)

_ONE_MINUTE = timedelta(minutes=1)


def _floor_to_minute(ts: datetime) -> datetime:
    # UTC minute flooring is equivalent to ET minute flooring because the
    # America/New_York offset is always a whole hour (no fractional offset
    # before or after DST). So a UTC floor lands on the same instant as an
    # ET floor, which is what the strategy layer expects: session edges
    # 09:30 / 16:00 ET align exactly with the resulting bucket boundaries.
    # DST transitions are handled correctly because UTC never repeats or
    # skips minutes — the 01:30 ET fold-back maps to two distinct UTC
    # minutes and therefore two distinct buckets, not one collapsed one.
    return ts.replace(second=0, microsecond=0)


@dataclass(slots=True)
class _BarBuilder:
    symbol: str
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int
    source: str

    def apply(self, tick: Tick) -> None:
        if tick.price > self.high:
            self.high = tick.price
        if tick.price < self.low:
            self.low = tick.price
        self.close = tick.price
        self.volume += tick.volume
        self.tick_count += tick.tick_count

    def build(self) -> Bar:
        return Bar(
            symbol=self.symbol,
            bucket_start=self.bucket_start,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            tick_count=self.tick_count,
            source=self.source,
        )


class BarAggregator:
    """
    Aggregates ticks into 1-minute OHLCV bars per symbol.

    See docs/design.md §3.6.4 for aggregation rules.

    Semantics:
      - Bucket = [floor_minute(ts), floor_minute(ts) + 1 min).
      - A bar closes when:
          (a) a later-bucket tick arrives (ingest emits), or
          (b) advance_time(now) is called with now >= bucket_end.
      - When ingest crosses multiple minute boundaries with no ticks in
        between, the gap is filled forward with EMPTY bars
        (volume=0, OHLC = prev close, source="empty").
      - Out-of-order ticks (ts strictly earlier than the latest seen
        tick for that symbol) are dropped and logged.
    """

    def __init__(self) -> None:
        self._builders: dict[str, _BarBuilder] = {}
        self._last_close: dict[str, float] = {}
        self._last_tick_ts: dict[str, datetime] = {}

    def ingest(self, tick: Tick) -> list[Bar]:
        prev_ts = self._last_tick_ts.get(tick.symbol)
        if prev_ts is not None and tick.timestamp < prev_ts:
            log.warning(
                "Dropping out-of-order tick for %s: ts=%s < last=%s",
                tick.symbol,
                tick.timestamp,
                prev_ts,
            )
            return []
        self._last_tick_ts[tick.symbol] = tick.timestamp

        bucket = _floor_to_minute(tick.timestamp)
        builder = self._builders.get(tick.symbol)

        if builder is None:
            self._builders[tick.symbol] = self._new_builder(tick, bucket)
            return []

        if bucket == builder.bucket_start:
            builder.apply(tick)
            return []

        emitted: list[Bar] = []
        closed = builder.build()
        emitted.append(closed)
        self._last_close[tick.symbol] = closed.close

        next_start = builder.bucket_start + _ONE_MINUTE
        while next_start < bucket:
            emitted.append(self._empty_bar(tick.symbol, next_start))
            next_start += _ONE_MINUTE

        self._builders[tick.symbol] = self._new_builder(tick, bucket)
        return emitted

    def advance_time(self, now: datetime) -> list[Bar]:
        """Close any in-progress builder whose bucket has fully ended."""
        emitted: list[Bar] = []
        for symbol in list(self._builders):
            builder = self._builders[symbol]
            if builder.bucket_start + _ONE_MINUTE > now:
                continue
            closed = builder.build()
            emitted.append(closed)
            self._last_close[symbol] = closed.close
            del self._builders[symbol]
        return emitted

    def _new_builder(self, tick: Tick, bucket: datetime) -> _BarBuilder:
        return _BarBuilder(
            symbol=tick.symbol,
            bucket_start=bucket,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=tick.volume,
            tick_count=tick.tick_count,
            source=tick.source,
        )

    def _empty_bar(self, symbol: str, bucket_start: datetime) -> Bar:
        last = self._last_close.get(symbol, 0.0)
        return Bar(
            symbol=symbol,
            bucket_start=bucket_start,
            open=last,
            high=last,
            low=last,
            close=last,
            volume=0,
            tick_count=0,
            source="empty",
        )
