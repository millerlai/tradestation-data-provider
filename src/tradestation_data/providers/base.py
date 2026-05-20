from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick

# An event on the wire is either a single trade/quote print (Tick) or a
# complete OHLC bar (Bar) emitted directly by the vendor-side indicator.
# Historically the bridge only shipped ticks and Python aggregated bars
# downstream; now EL_PublishTickEx can ship an already-formed minute bar
# so the OHLC is preserved through historical-chart replay.
MarketEvent = Tick | Bar


@runtime_checkable
class MarketDataProvider(Protocol):
    """
    Abstract real-time market data source. Implementations:
      - TradeStationELProvider    (current: EL + DLL + ZeroMQ)
      - TradeStationWebAPIProvider (future: direct REST/streaming)
      - ...other vendors

    See docs/design.md §3.3.
    """

    source_id: str

    async def connect(self) -> None:
        """Open the underlying connection (socket, websocket, ...)."""
        ...

    async def subscribe(self, symbols: list[str]) -> None:
        """Start receiving events for the given symbols. Idempotent."""
        ...

    def events(self) -> AsyncIterator[MarketEvent]:
        """Yield ticks and/or bars as they arrive. Runs until `close()`."""
        ...

    def ticks(self) -> AsyncIterator[Tick]:
        """Tick-only convenience view. Bars are silently skipped."""
        ...

    async def close(self) -> None:
        """Release resources. Safe to call multiple times."""
        ...
