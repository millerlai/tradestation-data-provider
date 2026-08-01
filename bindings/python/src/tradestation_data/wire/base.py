from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from tradestation_data.domain.bar import Bar
from tradestation_data.domain.tick import Tick

# An event on the wire is either a single trade print (Tick) or a complete
# OHLC bar (Bar). Both come straight from the EL indicator; this binding
# builds neither from the other.
#
# This union is the value range of the wire — see contract/wire.md.
MarketEvent = Tick | Bar


@runtime_checkable
class MarketDataProvider(Protocol):
    """
    A TradeStation ingress for this binding.

    NOT a generic multi-vendor abstraction. This package speaks to
    TradeStation and nothing else; the shape exists because TradeStation
    can be reached more than one way:

      - TradeStationELProvider — EL indicator + DLL + ZeroMQ (implemented)
      - a future WebAPI ingress — REST/streaming, no DLL

    Consumers that want to swap this package out for a different vendor
    should declare their own Protocol on their side and let this one
    satisfy it structurally. Do not import this type as a vendor-neutral
    contract — it is not one, and it is free to change with TradeStation.
    See docs/architecture.md §7.1.
    """

    # Which ingress produced these events. Nothing inside this binding reads
    # it any more — Tick and Bar no longer carry a provenance field, because
    # with nothing computing bars there are no two kinds of bar to tell
    # apart. It stays on the Protocol for consumers running more than one
    # ingress, which is the only place the distinction can still matter.
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
