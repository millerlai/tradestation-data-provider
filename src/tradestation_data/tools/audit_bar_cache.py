"""
T2.5.E.1 — weekly bar-cache consistency audit.

Rebuilds the 1-min bar cache for each (symbol, day) in the given date
range from the Tier 1 tick store, and diffs it against what is currently
stored under `{root}/bars/timeframe=1m/...`. Any mismatch (missing rows,
extra rows, or OHLC/volume divergence) is reported to stderr.

Use case: cron weekly. If the audit ever flags drift, investigate before
Tier 3 resamples start producing bad numbers downstream.

Example:
  uv run python -m tradestation_data.tools.audit_bar_cache \\
      --data-root ./data --symbols SPY QQQ --days 7

Exit codes:
  0 — audit clean
  1 — drift detected (details on stderr)
  2 — bad arguments / no data to audit
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from tradestation_data.storage.history_store import HistoryStore
from tradestation_data.storage.resampler import Resampler

log = logging.getLogger("tradestation_data.tools.audit_bar_cache")

# OHLC tolerance: prices are stored as float64, so byte-for-byte equality
# is fine if resampler is deterministic — but leave a tiny epsilon in case
# Polars / DuckDB kernels introduce float noise after an upgrade.
_PRICE_TOL = 1e-9


@dataclass(slots=True)
class AuditResult:
    symbol: str
    day: str
    live_rows: int
    rebuilt_rows: int
    diffs: list[str]

    @property
    def clean(self) -> bool:
        return not self.diffs


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="audit_bar_cache", description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="Symbols to audit (e.g. SPY QQQ VXX).",
    )
    p.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of trailing days (inclusive of today) to audit.",
    )
    p.add_argument(
        "--end-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC),
        default=None,
        help="Last day to audit (YYYY-MM-DD UTC, default: today).",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def _compare_dataframes(live: pl.DataFrame, rebuilt: pl.DataFrame) -> list[str]:
    """Return a list of human-readable diff strings; empty = clean."""
    diffs: list[str] = []
    if live.height != rebuilt.height:
        diffs.append(f"row_count live={live.height} rebuilt={rebuilt.height}")
        # Still compare overlapping rows below to give a richer report.

    # Align on bucket_start for row-wise comparison.
    joined = live.join(
        rebuilt,
        on="bucket_start",
        how="full",
        suffix="_r",
    )

    # Missing / extra buckets.
    only_live = joined.filter(pl.col("open_r").is_null()).select("bucket_start")
    only_rebuilt = joined.filter(pl.col("open").is_null()).select("bucket_start")
    if only_live.height:
        diffs.append(
            f"missing_in_rebuilt n={only_live.height} "
            f"first={only_live.item(0, 0)} last={only_live.item(-1, 0)}"
        )
    if only_rebuilt.height:
        diffs.append(
            f"missing_in_live n={only_rebuilt.height} "
            f"first={only_rebuilt.item(0, 0)} last={only_rebuilt.item(-1, 0)}"
        )

    # Value drift on overlapping rows.
    overlap = joined.filter(pl.col("open").is_not_null() & pl.col("open_r").is_not_null())
    for col in ("open", "high", "low", "close"):
        mismatched = overlap.filter((pl.col(col) - pl.col(f"{col}_r")).abs() > _PRICE_TOL)
        if mismatched.height:
            diffs.append(f"price_drift column={col} n={mismatched.height}")
    for col in ("volume", "tick_count"):
        mismatched = overlap.filter(pl.col(col) != pl.col(f"{col}_r"))
        if mismatched.height:
            diffs.append(f"count_drift column={col} n={mismatched.height}")
    return diffs


def _audit_one(
    store: HistoryStore,
    resampler: Resampler,
    symbol: str,
    start: datetime,
    end: datetime,
) -> AuditResult:
    day_str = start.date().isoformat()
    live = store.load_bars(symbol, start, end, "1m")
    # Rebuild straight from ticks (bypassing cache).
    rebuilt = resampler.resample(symbol, start, end, "1m")
    diffs = _compare_dataframes(live, rebuilt)
    return AuditResult(
        symbol=symbol,
        day=day_str,
        live_rows=live.height,
        rebuilt_rows=rebuilt.height,
        diffs=diffs,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.data_root.exists():
        log.error("data_root_not_found path=%s", args.data_root)
        return 2

    end_date = args.end_date or datetime.now(tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    days = [end_date - timedelta(days=i) for i in range(args.days)]
    days.reverse()  # oldest → newest

    store = HistoryStore(args.data_root)
    resampler = Resampler(args.data_root / "ticks")

    all_clean = True
    any_data = False
    for symbol in args.symbols:
        for day_start in days:
            day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
            result = _audit_one(store, resampler, symbol, day_start, day_end)
            if result.live_rows == 0 and result.rebuilt_rows == 0:
                log.debug("skip_empty_day symbol=%s day=%s", symbol, result.day)
                continue
            any_data = True
            if result.clean:
                log.info(
                    "clean symbol=%s day=%s rows=%d",
                    symbol,
                    result.day,
                    result.live_rows,
                )
            else:
                all_clean = False
                for d in result.diffs:
                    log.error("drift symbol=%s day=%s %s", symbol, result.day, d)

    if not any_data:
        log.warning("no_bars_in_window — did you run ingestion yet?")
        return 2
    return 0 if all_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
