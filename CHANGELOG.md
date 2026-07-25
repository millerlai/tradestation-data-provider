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
- **`contract/` — a language-neutral wire specification, now the source of truth for this
  repo.** Holds `v1/` and `v2/` JSON Schemas, `semantics.md` (the rules a schema cannot
  express: timestamp authority, left-labelled and minute-floored `bucket_start`, quote
  availability, session policy, sequence handling), `error_codes.md`, `compat.md`, and
  recorded conformance fixtures. Python is now one binding against this spec rather than
  the definition of it.
- **Wire v2 / DLL ABI 7 — gap detection.** The payload carries `seq` (per-symbol,
  monotonic) and `sid` (publisher session id). PUB/SUB drops silently at both high-water
  marks, so before this a subscriber could not distinguish a quiet market from a lost one.
  `TradeStationELProvider.messages_lost` reports the count.
- `EL/` — the EasyLanguage exporter indicator, previously kept in the consuming project.
  It is the upstream origin of the feed and belongs with the provider.
- `contract/fixtures/` with `smoke`, `noquote`, and `v1_legacy` cases, all recorded from
  real DLL output (the v1 case from a DLL built out of git history, not hand-written).
  `bindings/python/tests/conformance/` replays them against this binding.
- `contract/tools/record.py` — wire inspector and fixture recorder, promoted from
  `scripts/simple_sub.py`. It depends on no binding, which is what qualifies it to record
  the files every binding is checked against.
- Test harness gains `--mode noquote`, reproducing the shape TradeStation emits outside
  live mode.
- CMake presets for Visual Studio 2026 alongside 2022.
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
- **BREAKING (wire): `bid` / `ask` may now be `null`.** EL's `InsideBid` / `InsideAsk`
  return 0 whenever there is no quote — historical replay, any non-live bar, and symbols
  that never carry one. The DLL previously forwarded that 0 verbatim, putting what reads
  as a $0.00 quote on the wire and leaving every binding to remember that 0 means absent.
  It now emits `null`. Wire v1 cannot do this, so bindings reading v1 must treat a
  non-positive quote as absent.
- **BREAKING (layout): the Python package moved to `bindings/python/`.** A second binding
  is now a sibling directory rather than a restructure. `config/` and a `LICENSE` copy
  moved with it, because packaging back-ends resolve paths relative to `pyproject.toml`
  and cannot reach above it.
- **BREAKING (API): `providers/` renamed to `wire/`**, and `tradestation_el.py` to
  `el_subscriber.py`. "Providers" invited reading the module as a generic multi-vendor
  abstraction; its job is decoding wire frames. `MarketDataProvider` keeps its name but no
  longer claims to cover other vendors — consumers wanting to swap this package out should
  declare their own Protocol and let this one satisfy it structurally.
- **BREAKING (API): `domain/` is now exactly `Tick` and `Bar`.** `Order`, `Fill`,
  `OrderIntent`, `OrderStatus`, `OrderType`, `Side`, and `Position` are gone, along with
  `MarketSnapshot.{position_of,positions,set_position,clear_position}`. Nothing in
  production code imported the order module, and position tracking is a consumer concern —
  a data provider does not know what you hold.
- `bindings/python/.python-version` pins 3.12 to match the CI matrix. `requires-python`
  has no upper bound, so a fresh `uv sync` had picked 3.14, for which pyarrow has no wheel.
- Dependency declarations now carry upper bounds (`pyzmq<28`, `polars<2`, `pyarrow<22`,
  `pyyaml<7`, `structlog<26`, `duckdb<2`, `pydantic<3`) to guard installs against
  accidental major-version breakage. Caps are intentionally generous; revisit them at
  each major release.

### Fixed
- `verify_parquet.py::_expected_bars()` produced right-labelled bar ends (09:31..16:00)
  where `BAR_SCHEMA.bucket_start` is left-labelled (09:30..15:59), so a complete session
  was reported as missing its first bar and carrying an extra last one.
- The CMake build could not produce a DLL at all: `CMakeLists.txt` never defined
  `TS2PYTHON_EXPORTS`, which `ts2python.h` checks to choose `dllexport` over `dllimport`.
  CMake's automatic define is `TS2Python_EXPORTS` — target name, different case — so every
  definition failed with C2491. Only the MSBuild project set it, which is why the breakage
  went unnoticed. `CMAKE_TOOLCHAIN_FILE` also no longer reads `$env{VCPKG_ROOT}`, which is
  easy to leave pointing at an unrelated checkout; it uses the vendored submodule.
- Repo-root `.ruff.toml` excluding `cpp/`. After the layout change there is no Python
  config at the top of the tree, so `ruff check .` from there fell back to defaults and
  rewrote the vendored vcpkg checkout — which had already happened once.
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
