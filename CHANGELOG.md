# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) starting at
v1.0.0. While the project is in `0.x`, minor releases (`0.X.0`) may introduce breaking
changes; patch releases (`0.x.Y`) will not.

## [Unreleased]

## [0.2.0] — 2026-05-21

### Removed
- **BREAKING:** The `vwap` field is gone from every layer of the pipeline. `Bar` no longer
  carries it, `BarAggregator` no longer accumulates `pv_sum`, the EL provider no longer
  emits a synthesised value, and `BAR_SCHEMA` / `_polars_bars_to_arrow` /
  `Resampler` SQL no longer write or compute it. Downstream consumers should derive VWAP
  from raw ticks (`price * volume / sum(volume)`) if needed — the synthesised
  `close`-as-proxy that lived on bars added no information over what `close` already
  carries. Existing `bars.parquet` files written by `0.1.x` remain readable column-wise,
  but `BarWriter` / `HistoryStore` will reject them via schema mismatch on the next write
  pass; rebuild the bar cache from ticks (or delete and re-collect) when upgrading.

## [0.1.1] — 2026-05-20

### Added
- CI workflow (`.github/workflows/ci.yml`) running ruff (lint + format), mypy strict, and
  pytest across Python 3.11 / 3.12 / 3.13 on Ubuntu + Windows; separate `build` job
  smoke-tests the wheel.
- `__version__` exposed at the package root via `importlib.metadata`, so the package
  version is a single source of truth (`pyproject.toml`).
- `types-PyYAML` added to the `dev` extra so mypy can type-check yaml usage.
- Mypy override scoped to the three storage modules that wrap pyarrow's untyped Parquet
  API (`no-untyped-call`); the rest of `src/` stays under full strict rules.
- This `CHANGELOG.md`.

### Changed
- Dependency declarations now carry upper bounds (`pyzmq<28`, `polars<2`, `pyarrow<22`,
  `pyyaml<7`, `structlog<26`, `duckdb<2`, `pydantic<3`) to guard installs against
  accidental major-version breakage. Caps are intentionally generous; revisit them at
  each major release.

### Fixed
- Cleared all 12 mypy strict errors that the project had been silently carrying:
  - `runtime/config.py:7` and `sinks/registry.py:9` — resolved by `types-PyYAML`.
  - `storage/{bar_writer,tick_writer,history_store}.py` — pyarrow override above.
  - `providers/tradestation_el.py` — annotated `dict[str, Any]` on two parse helpers and
    moved the `type: ignore[unreachable]` onto the genuinely unreachable `return` rather
    than the `if` line above it.
- `ruff format --check` cleanup (four files) that had drifted from formatter output.

## [0.1.0] — 2026-05-20

### Added
- Initial Python data-collection runtime for the TradeStation EasyLanguage feed:
  - `TradeStationELProvider` (asyncio ZMQ SUB) subscribes to the C++ DLL's PUB endpoint.
  - `MarketSnapshot` + `BarAggregator` produce closed 1-min bars from tick streams.
  - `IngestionRuntime` wires the provider, snapshot, aggregator, and sinks together;
    handles intra-bar dedup for `EL_PublishTickEx` whole-OHLC bars.
- **Pluggable sink architecture** (`src/tradestation_data/sinks/`):
  - `Sink` protocol + `BaseSink` convenience class.
  - `SinkPipeline` fan-out with per-sink exception isolation.
  - YAML registry (`config/sinks.yaml` → `module:attr` dynamic import).
  - Built-in sinks: `ParquetBarSink`, `ParquetTickSink`, `InMemorySink`, `CallbackSink`.
- Hive-partitioned Parquet storage with Tier 1 (ticks) / Tier 2 (1-min bars) /
  Tier 3 (resampled bars) layout under `data/`.
- Offline tools in `scripts/`: aggregate, verify, impute, audit, dedupe, dump.
- Console script: `tradestation-data-ingest`.
- 272 unit + integration tests; pytest configured with `filterwarnings = ["error", ...]`
  so any new warning fails the build.
- PyPI release workflow (`.github/workflows/release.yml`) via Trusted Publishing.
- MIT LICENSE; `py.typed` marker for downstream mypy.
- Bilingual README (English primary, 繁體中文 mirror) with Mermaid architecture diagram.

[Unreleased]: https://github.com/millerlai/tradestation-data-provider/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/millerlai/tradestation-data-provider/releases/tag/v0.2.0
[0.1.1]: https://github.com/millerlai/tradestation-data-provider/releases/tag/v0.1.1
[0.1.0]: https://github.com/millerlai/tradestation-data-provider/releases/tag/v0.1.0
