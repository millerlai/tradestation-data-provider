from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from tradestation_data.domain.bar import Bar

# The wire carries one shape: a data point on a chart, whatever kind of
# chart it is. There is no Tick/Bar split any more — TradeStation supplies
# the same reserved words for every point, and dropping some of them by
# chart type was the publisher deciding what a number meant.
#
# See contract/wire.md.
MarketEvent = Bar


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
        """Yield points as they arrive. Runs until `close()`."""
        ...

    async def close(self) -> None:
        """Release resources. Safe to call multiple times."""
        ...
