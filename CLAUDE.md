# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

Pure-Python data pipeline for a TradeStation EasyLanguage feed. The C++ DLL (built in a separate parent repo) publishes ticks and 1-min bars over ZeroMQ; this project is the Python side that subscribes, aggregates, and **fans the events out through a pluggable sink pipeline** (Parquet by default, but anything implementable). Offline scripts under `scripts/` then verify, dedupe, aggregate, and back-fill the resulting Parquet store. **No strategy / broker / risk wiring lives here** — the runtime is data-collection-only.

- Python ≥ 3.11, managed with **uv** (`pyproject.toml` + `uv.lock`).
- Package: `tradestation_data` under `src/` (src layout). Console script: `tradestation-data-ingest` → `tradestation_data.runtime.main:main`.
- Distributed via pip / uv / poetry — installable from PyPI (after first publish) or directly from the GitHub URL. Build backend: hatchling.
- README is split: `README.md` (English, primary; what PyPI shows) and `README.zh-TW.md` (繁體中文). Architecture diagram in the README is Mermaid; the ASCII diagram in this file is kept because Claude reads the file as plain text.
- All wrapper scripts in `scripts/` shell out to `uv run …` via `scripts/_common.py`; run them with plain `python scripts/<name>.py` and they re-enter the project venv.

## Commands

```powershell
# Install
uv sync                       # base deps
uv sync --extra dev           # + pytest / pytest-asyncio / pytest-cov / ruff / mypy

# Run ingestion (DLL must already be publishing on the configured endpoint)
python scripts/run_ingestion.py
python scripts/run_ingestion.py --sinks-config config/sinks.yaml   # use a different sink pipeline
python scripts/run_ingestion.py --no-storage --log-level DEBUG     # smoke test (empty pipeline)
python scripts/run_ingestion.py --print-bars 5                     # dump first N bars as the on-disk schema

# Offline pipeline tools (all are uv-run wrappers)
python scripts/aggregate_parquet.py --symbol all --timeframe 5m --input data/bars/timeframe=1m --output data/bars
python scripts/verify_parquet.py    --start-date YYYY-MM-DD --end-date YYYY-MM-DD
python scripts/imputation_parquet.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dry-run
python scripts/audit_bar_cache.py
python scripts/clear_bar_cache.py
python scripts/dedupe_bars.py
python scripts/dump_parquet.py
python scripts/simple_sub.py        # raw ZMQ wire-format inspector

# Build / smoke-test the wheel
uv build                                                            # → dist/*.whl + *.tar.gz
uv run --isolated --no-project --with dist/*.whl tradestation-data-ingest --help

# Tests / lint / types
uv run pytest                                       # full suite (asyncio_mode=auto), 272 tests
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
                                            │            └──▶ SinkPipeline.on_tick
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

`IngestionRuntime` writes to a `SinkPipeline` rather than directly to `BarWriter` / `TickWriter`. The pipeline broadcasts every Tick and every closed Bar to every registered sink and **isolates per-sink exceptions** (one bad sink doesn't take down the others). The pipeline is built from `config/sinks.yaml` via `tradestation_data.sinks.registry.build_pipeline_from_config()`; users register custom sinks by pointing the YAML `class:` field at any importable `module:attr` returning a Sink protocol implementation. Built-ins:

- `ParquetBarSink`, `ParquetTickSink` — thin adapters over the legacy `BarWriter` / `TickWriter` under `storage/`; on-disk layout, schema, and flush semantics are unchanged.
- `InMemorySink` — bounded per-symbol deques; for tests / notebook use only.
- `CallbackSink` — dynamic Python callback dispatch with per-symbol or catch-all registration. Instances are tracked in a module-level `WeakValueDictionary`; user code does `get_sink(name)` to look up the instance declared in `sinks.yaml` and register handlers on it. `close()` eagerly removes the registry entry so a subsequent `get_sink()` raises `KeyError` immediately, not after GC.

The `tick_writer` / `bar_writer` constructor parameters on `IngestionRuntime` are gone — pass a `SinkPipeline` instead. The legacy `BarWriter` / `TickWriter` classes still live under `storage/` because `ParquetBarSink` / `ParquetTickSink` wrap them; they are *not* the public interface anymore.

Two bar paths coexist because the DLL supports both `EL_PublishTick` (tick-only — bars built by `BarAggregator`) and `EL_PublishTickEx` (whole-OHLC bars). Whole bars **bypass the aggregator** — running a single close-price tick through it would collapse OHLC to O=H=L=C. EL's "Update every tick" mode re-sends the same `(symbol, bucket_start)` many times per minute; `_handle_provider_bar` buffers the latest per symbol and only emits when the next bucket arrives, on wall-clock advance past `bucket_end + grace`, or on shutdown. `_last_emitted_direct_bucket` blocks history-replay duplicates after a TS chart reload.

### Storage tiers (`storage/`)

Hive-partitioned Parquet under `data/`. `ParquetBarSink` / `ParquetTickSink` produce Tier 1 + Tier 2; Tier 3 is derived on demand:

| Tier | Path                                                                     | Producer       |
|------|--------------------------------------------------------------------------|----------------|
| 1    | `ticks/symbol={SYM}/date={YYYY-MM-DD}/ticks.parquet`                     | `ParquetTickSink` (→ `TickWriter`) |
| 2    | `bars/timeframe=1m/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet`          | `ParquetBarSink`  (→ `BarWriter`)  |
| 3    | `bars/timeframe={5m,15m,30m,1h}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet` | `Resampler` (lazy, on cache miss from `HistoryStore.load_bars`) or `aggregate_parquet.py` (batch) |

`HistoryStore` is the read-side facade — DuckDB + Polars over the Parquet glob; `load_bars` falls through to `Resampler` and persists the result. `BAR_SCHEMA` / `TICK_SCHEMA` carry both `*_utc` (`UTC`) and `*_et` (`America/New_York`) timestamps — both are persisted so downstream tooling never has to convert at query time. Tier 3 caches are derived; `clear_bar_cache.py` deletes them safely and `audit_bar_cache.py` cross-checks them against a Tier-1 rebuild.

`--data-root` is now only a fallback path used when `--sinks-config` is missing — the YAML's per-sink `root` parameter wins otherwise. When you need to redirect output, edit `sinks.yaml`, not the CLI flag.

### Timestamps and sessions

- The EL string `ts_str` (format `yyyy-MM/dd-HH:mm:ss`, 24-hour) is **authoritative** for `Bar.bucket_start` and is parsed as `America/New_York`, then converted to UTC. `ts_utc` from the DLL is only a sanity cross-check (a >5s drift is logged, not raised). 24-hour is deliberate — the old `hh:mm:ss tt` format broke on zh-TW Windows hosts where `FormatTime("tt")` emits localised AM/PM.
- Ticks use the DLL's receive-side `ts` (UTC epoch) as authoritative.
- `aggregation/session.py` owns session-edge logic. US equity session = 09:30–16:00 ET; bars before 04:00 ET belong to the *previous* session. Per-symbol retention via `SessionPolicy`: `breadth` indices reset daily, everything else retains 60 min of pre-market by default. Defaults come from `symbols.yaml::category`; per-symbol overrides are read in `runtime/config.py`.
- Index/breadth symbols (`$TICK`, `$ADD`, `$VOLD`, `$TRIN`, `$PCVA`, `VXX` by default in `DEFAULT_INDEX_SYMBOLS`) have no bid/ask/volume — the provider forces `bid=ask=None` and `volume==0`.

### Windows-specific event loop

pyzmq's asyncio integration uses `loop.add_reader()`, which the default Windows ProactorEventLoop does **not** support — SUB sockets connect but `recv_multipart()` never wakes. Both `runtime/main.py` and `tests/conftest.py` force `WindowsSelectorEventLoopPolicy` on `win32`. Preserve this when adding new entrypoints.

### Shutdown ordering

`IngestionRuntime.run()` deliberately cancels tasks → closes provider → awaits tasks → calls `_shutdown()` in an *outer* finally; `runtime/main.py` then calls `pipeline.close()` one more time as belt-and-suspenders. `_shutdown()` drains the aggregator and the direct-bar buffer **before** closing the sink pipeline, so any final closed bar still reaches every sink. A second Ctrl+C while `zmq ctx.term()` was blocking previously skipped sink close and left `bars.parquet` without a footer — don't simplify the nested-finally structure without understanding that failure mode.

## Packaging and distribution

- Build backend: `hatchling`. Wheel target is `packages = ["src/tradestation_data"]`; sdist target is explicitly listed (src / tests / config/{sinks,symbols}.yaml / README / LICENSE / pyproject.toml). The C++ tree and `scripts/` are deliberately excluded from sdist.
- `src/tradestation_data/py.typed` (empty marker, PEP 561) ships in the wheel so downstream mypy picks up our type hints.
- `.github/workflows/release.yml` fires on `v*` tag pushes: builds, smoke-tests by installing the wheel into an isolated venv and running `tradestation-data-ingest --help`, then uploads to PyPI via Trusted Publishing (OIDC, no API tokens in repo secrets). First publish requires registering the workflow as a Trusted Publisher on the PyPI project page (workflow `release.yml`, environment `pypi`).
- The workflow includes a tag-vs-`pyproject.toml` version sanity check; bumping `version` and tagging `vX.Y.Z` must stay in sync.

## Testing notes

- `tests/conftest.py` injects `tests/_sink_fixtures.py` into `sys.modules` under the bare name `_sink_fixtures` at collection time, because `tests/` is *not* on `pythonpath` (only `src/` is). The sink-registry tests use that name in their `module:attr` target strings to verify dynamic instantiation without the fixture module needing to be installable. New registry tests should follow the same convention rather than re-add `tests/` to the path.
- `tests/conftest.py::zmq_inproc_bus` uses `inproc://` for asyncio ZMQ socket tests so there's no network handshake; teardown is `ctx.destroy(linger=0)` because plain `ctx.term()` occasionally segfaults on Windows + Python 3.13 + pyzmq 27.

## Conventions

- Lint/format: ruff (line length 100, target py311; rules `E,F,W,I,N,UP,B,SIM,RUF`; tests get `N802/N803` relaxed). `cpp/build-tools` and `cpp/vcpkg_installed` are explicitly excluded.
- Types: mypy strict on `src/`; `tests/` is excluded.
- Coverage: branch coverage on `src/tradestation_data`, excludes match `pragma: no cover`, `raise NotImplementedError`, `if TYPE_CHECKING`, ellipsis-only stubs.
- Dataclasses: use `slots=True` (and `frozen=True` for value types) — the codebase is consistent on this.
- Logging: stdlib `logging` everywhere, with structured kwargs via `extra={...}`. `runtime/main.py` ships both a plain formatter (`_ExtraDumpFilter` flattens `extra` into the message tail) and `--log-json` (`_JsonFormatter`). Keep new log sites in the same shape. Sink-related events use the conventions `sink_on_tick_failed`, `sink_on_bar_failed`, `sink_flush_failed`, `sink_close_failed`, `sinks_loaded`, `sinks_config_invalid` — match these when adding new sink paths.
- Sinks: subclass `BaseSink` (or implement the `Sink` Protocol directly), accept `name=` as a keyword argument in `__init__`, and assign it to `self.name`. `should_flush` defaults to `False`; only override it on sinks that actually buffer.
- File encoding when writing on Windows: PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM; pass raw bytes through `Write` / `Edit` (our tools) or use Python to write UTF-8 without BOM. A BOM in `pyproject.toml` makes the TOML parser reject the file.
