# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

Pure-Python data pipeline for a TradeStation EasyLanguage feed. The C++ DLL (built in a separate parent repo) publishes ticks and 1-min bars over ZeroMQ; this project is the Python side that subscribes, aggregates, persists, verifies, and back-fills the resulting Parquet store. **No strategy / broker / risk wiring lives here** — the runtime is data-collection-only.

- Python ≥ 3.11, managed with **uv** (`pyproject.toml` + `uv.lock`).
- Package: `tradestation_data` under `src/` (src layout). Console script: `tradestation-data-ingest` → `tradestation_data.runtime.main:main`.
- All wrapper scripts in `scripts/` shell out to `uv run …` via `scripts/_common.py`; run them with plain `python scripts/<name>.py` and they re-enter the project venv.

## Commands

```powershell
# Install
uv sync                       # base deps
uv sync --extra dev           # + pytest / pytest-asyncio / pytest-cov / ruff / mypy

# Run ingestion (DLL must already be publishing on the configured endpoint)
python scripts/run_ingestion.py
python scripts/run_ingestion.py --no-storage --log-level DEBUG     # smoke test
python scripts/run_ingestion.py --print-bars 5                     # dump first N bars as written

# Offline pipeline tools (all are uv-run wrappers)
python scripts/aggregate_parquet.py --symbol all --timeframe 5m --input data/bars/timeframe=1m --output data/bars
python scripts/verify_parquet.py    --start-date YYYY-MM-DD --end-date YYYY-MM-DD
python scripts/imputation_parquet.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dry-run
python scripts/audit_bar_cache.py
python scripts/clear_bar_cache.py
python scripts/dedupe_bars.py
python scripts/dump_parquet.py
python scripts/simple_sub.py        # raw ZMQ wire-format inspector

# Tests / lint / types
uv run pytest                                       # full suite (asyncio_mode=auto)
uv run pytest tests/test_bar_aggregator.py          # one file
uv run pytest tests/test_bar_aggregator.py::test_x  # one test
uv run pytest --cov                                 # coverage (config in pyproject.toml)
uv run ruff check . ; uv run ruff format .
uv run mypy                                         # strict mode on src/
```

Pytest is configured (in `pyproject.toml`) with `pythonpath = ["src"]`, `asyncio_mode = "auto"`, and `filterwarnings = ["error", "ignore::DeprecationWarning"]` — **a new warning will fail the build**; fix the cause, don't broaden the filter.

## Architecture

### Live ingest data-flow

```
TradeStation EL DLL  ──ZMQ PUB──▶  TradeStationELProvider (SUB, asyncio)
                                            │
                                            ├── Tick  ──▶ MarketSnapshot.on_tick
                                            │            └──▶ BarAggregator.ingest ──▶ closed Bar
                                            │
                                            └── Bar   ──▶ IngestionRuntime._handle_provider_bar
                                                         (intra-bar buffer, dedupe, replace-last)
                                                                 │
                              closed Bar ◀───────────────────────┘
                                  │
                                  ├──▶ MarketSnapshot.on_bar
                                  ├──▶ SinkPipeline.on_bar  ──┬──▶ ParquetBarSink (default)
                                  │                           ├──▶ ParquetTickSink (default; ticks only)
                                  │                           ├──▶ InMemorySink / CallbackSink
                                  │                           └──▶ user-defined sinks from sinks.yaml
                                  └──▶ optional on_bar callback

Background loops in IngestionRuntime: ingest / advance (wall-clock) / flush (sinks that advertise it) / heartbeat.
```

### Pluggable sinks (`sinks/`)

`IngestionRuntime` writes to a `SinkPipeline` rather than directly to `BarWriter`/`TickWriter`. The pipeline broadcasts every Tick and every closed Bar to every registered sink and **isolates per-sink exceptions** (one bad sink doesn't take down the others). The pipeline is built from `config/sinks.yaml` via `tradestation_data.sinks.registry.build_pipeline_from_config()`; users register custom sinks by pointing the YAML `class:` field at any importable `module:attr` returning a Sink protocol implementation. Built-ins: `ParquetBarSink`, `ParquetTickSink`, `InMemorySink`, `CallbackSink` (with module-level `get_sink(name)` lookup so user code can register handlers dynamically after the runtime starts).

The legacy `BarWriter` / `TickWriter` classes still live under `storage/` — `ParquetBarSink` / `ParquetTickSink` are thin adapters around them so on-disk layout, schema, and flush semantics are unchanged. The `tick_writer` / `bar_writer` constructor parameters on `IngestionRuntime` are gone; pass a `SinkPipeline` instead.

Two bar paths coexist because the DLL supports both `EL_PublishTick` (tick-only — bars built by `BarAggregator`) and `EL_PublishTickEx` (whole-OHLC bars). Whole bars **bypass the aggregator** — running a single close-price tick through it would collapse OHLC to O=H=L=C. EL's "Update every tick" mode re-sends the same `(symbol, bucket_start)` many times per minute; `_handle_provider_bar` buffers the latest per symbol and only emits when the next bucket arrives, on wall-clock advance past `bucket_end + grace`, or on shutdown. `_last_emitted_direct_bucket` blocks history-replay duplicates after a TS chart reload.

### Storage tiers (`storage/`)

Hive-partitioned Parquet under `data/` (path-overridable via `--data-root`):

| Tier | Path                                                                     | Producer       |
|------|--------------------------------------------------------------------------|----------------|
| 1    | `ticks/symbol={SYM}/date={YYYY-MM-DD}/ticks.parquet`                     | `TickWriter`   |
| 2    | `bars/timeframe=1m/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet`          | `BarWriter`    |
| 3    | `bars/timeframe={5m,15m,30m,1h}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet` | `Resampler` (lazy, on cache miss from `HistoryStore.load_bars`) or `aggregate_parquet.py` (batch) |

`HistoryStore` is the read-side facade — DuckDB + Polars over the Parquet glob; `load_bars` falls through to `Resampler` and persists the result. `BAR_SCHEMA` / `TICK_SCHEMA` carry both `*_utc` (`UTC`) and `*_et` (`America/New_York`) timestamps — both are persisted so downstream tooling never has to convert at query time. Tier 3 caches are derived; `clear_bar_cache.py` deletes them safely and `audit_bar_cache.py` cross-checks them against a Tier-1 rebuild.

### Timestamps and sessions

- The EL string `ts_str` (format `yyyy-MM/dd-HH:mm:ss`, 24-hour) is **authoritative** for `Bar.bucket_start` and is parsed as `America/New_York`, then converted to UTC. `ts_utc` from the DLL is only a sanity cross-check (a >5s drift is logged, not raised). 24-hour is deliberate — the old `hh:mm:ss tt` format broke on zh-TW Windows hosts where `FormatTime("tt")` emits localised AM/PM.
- Ticks use the DLL's receive-side `ts` (UTC epoch) as authoritative.
- `aggregation/session.py` owns session-edge logic. US equity session = 09:30–16:00 ET; bars before 04:00 ET belong to the *previous* session. Per-symbol retention via `SessionPolicy`: `breadth` indices reset daily, everything else retains 60 min of pre-market by default. Defaults come from `symbols.yaml::category`; per-symbol overrides are read in `runtime/config.py`.
- Index/breadth symbols (`$TICK`, `$ADD`, `$VOLD`, `$TRIN`, `$PCVA`, `VXX` by default in `DEFAULT_INDEX_SYMBOLS`) have no bid/ask/volume — the provider forces `bid=ask=None` and `vwap` is null when `volume==0`.

### Windows-specific event loop

pyzmq's asyncio integration uses `loop.add_reader()`, which the default Windows ProactorEventLoop does **not** support — SUB sockets connect but `recv_multipart()` never wakes. Both `runtime/main.py` and `tests/conftest.py` force `WindowsSelectorEventLoopPolicy` on `win32`. Preserve this when adding new entrypoints.

### Shutdown ordering

`IngestionRuntime.run()` deliberately cancels tasks → closes provider → awaits tasks → calls `_shutdown()` in an *outer* finally, with belt-and-suspenders `close()` calls also in `runtime/main.py`. A second Ctrl+C while `zmq ctx.term()` was blocking previously skipped writer close and left `bars.parquet` without a footer. Don't simplify the nested-finally structure without understanding the failure mode.

## Conventions

- Lint/format: ruff (line length 100, target py311; rules `E,F,W,I,N,UP,B,SIM,RUF`; tests get `N802/N803` relaxed).
- Types: mypy strict on `src/`; `tests/` is excluded.
- Coverage: branch coverage on `src/tradestation_data`, excludes match `pragma: no cover`, `raise NotImplementedError`, `if TYPE_CHECKING`, ellipsis-only stubs.
- Dataclasses: use `slots=True` (and `frozen=True` for value types) — the codebase is consistent on this.
- Logging: stdlib `logging` everywhere, with structured kwargs via `extra={...}`. `runtime/main.py` ships both a plain formatter (`_ExtraDumpFilter` flattens `extra` into the message tail) and `--log-json` (`_JsonFormatter`). Keep new log sites in the same shape.
