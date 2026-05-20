from __future__ import annotations

from pathlib import Path

from tradestation_data.runtime import load_symbols


def test_load_symbols_parses_yaml() -> None:
    cfg = load_symbols(Path(__file__).parent.parent / "config" / "symbols.yaml")
    ids = cfg.ids()
    assert "SPY" in ids
    assert "VXX" in ids
    assert cfg.trade_symbols() == ["SPY"]
    assert "QQQ" in cfg.context_symbols()


def test_load_symbols_handles_custom_file(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        "symbols:\n"
        "  - { id: XYZ, category: etf, role: trade }\n"
        "  - { id: ABC, category: etf, role: context }\n",
        encoding="utf-8",
    )
    cfg = load_symbols(p)
    assert cfg.ids() == ["XYZ", "ABC"]
    assert cfg.trade_symbols() == ["XYZ"]


def test_session_policies_derived_from_category() -> None:
    cfg = load_symbols(Path(__file__).parent.parent / "config" / "symbols.yaml")
    policies = cfg.session_policies()

    # Breadth symbols reset at 09:30 ET
    for sym in ("$TICK", "$ADD", "$VOLD", "$TRIN", "$PCVA"):
        assert policies[sym].session_reset is True
        assert policies[sym].pre_market_window_minutes is None

    # ETF / volatility / mega_cap keep 60 min of pre-market
    for sym in ("SPY", "QQQ", "VXX", "NVDA", "AAPL"):
        assert policies[sym].session_reset is False
        assert policies[sym].pre_market_window_minutes == 60


def test_per_symbol_session_override(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        "symbols:\n"
        "  - { id: XYZ, category: etf, role: trade, "
        "session_reset: true, pre_market_window_minutes: 0 }\n"
        "  - { id: ABC, category: breadth, role: context, "
        "pre_market_window_minutes: 30 }\n",
        encoding="utf-8",
    )
    cfg = load_symbols(p)
    policies = cfg.session_policies()
    assert policies["XYZ"].session_reset is True
    assert policies["XYZ"].pre_market_window_minutes == 0
    # ABC keeps the default breadth session_reset=True but overrides the window
    assert policies["ABC"].session_reset is True
    assert policies["ABC"].pre_market_window_minutes == 30
