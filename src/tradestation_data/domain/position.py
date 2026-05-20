from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Position:
    """
    Current holdings in a symbol.
    `quantity > 0` = long, `< 0` = short, `0` = flat (not usually stored).
    """

    symbol: str
    quantity: int
    avg_cost: float
    realized_pnl: float
    unrealized_pnl: float
