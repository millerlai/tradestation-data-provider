from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tradestation_data.aggregation.session import SessionPolicy


@dataclass(frozen=True, slots=True)
class SymbolConfig:
    id: str
    category: str
    role: str
    session_reset: bool
    pre_market_window_minutes: int | None

    def session_policy(self) -> SessionPolicy:
        return SessionPolicy(
            session_reset=self.session_reset,
            pre_market_window_minutes=self.pre_market_window_minutes,
        )


@dataclass(frozen=True, slots=True)
class SymbolsConfig:
    symbols: tuple[SymbolConfig, ...]

    def ids(self) -> list[str]:
        return [s.id for s in self.symbols]

    def trade_symbols(self) -> list[str]:
        return [s.id for s in self.symbols if s.role == "trade"]

    def context_symbols(self) -> list[str]:
        return [s.id for s in self.symbols if s.role == "context"]

    def session_policies(self) -> dict[str, SessionPolicy]:
        return {s.id: s.session_policy() for s in self.symbols}


def _build_symbol_config(raw: dict[str, Any]) -> SymbolConfig:
    category = raw["category"]
    default = SessionPolicy.for_category(category)
    session_reset = bool(raw.get("session_reset", default.session_reset))
    pre_market = raw.get("pre_market_window_minutes", default.pre_market_window_minutes)
    pre_market_value = None if pre_market is None else int(pre_market)
    return SymbolConfig(
        id=raw["id"],
        category=category,
        role=raw["role"],
        session_reset=session_reset,
        pre_market_window_minutes=pre_market_value,
    )


def load_symbols(path: Path | str) -> SymbolsConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw = data["symbols"]
    return SymbolsConfig(symbols=tuple(_build_symbol_config(s) for s in raw))
