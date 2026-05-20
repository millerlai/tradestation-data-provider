from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"

    @property
    def is_buy_side(self) -> bool:
        """True for sides that increase signed quantity (lift the offer)."""
        return self in (Side.BUY, Side.BUY_TO_COVER)

    @property
    def is_sell_side(self) -> bool:
        """True for sides that decrease signed quantity (hit the bid)."""
        return self in (Side.SELL, Side.SELL_SHORT)


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """
    A request from Strategy/TradingAgent to open/close a position.
    Broker translates this into a concrete Order.
    """

    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    client_ref: str = ""


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    intent: OrderIntent
    status: OrderStatus
    submitted_at: datetime
    broker_ref: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    timestamp: datetime
