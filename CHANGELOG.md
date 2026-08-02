# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) starting at
v1.0.0. While the project is in `0.x`, minor releases (`0.X.0`) may introduce breaking
changes; patch releases (`0.x.Y`) will not.

## [Unreleased]

### Fixed — post-merge review of the proto-1 refactor

- **An unparseable `ts_str` is refused instead of silently falling back to the
  receive clock.** `_parse_bar` treated "the publisher sent no time string" and
  "the publisher sent a time string we cannot read" as the same state and
  substituted `ts` for both. `ts` is the DLL's receive clock, and during a
  TradeStation chart replay a whole session arrives inside one minute — so
  every bar collapsed onto a single `bucket_start`, the runtime's dedupe kept
  one, and a trading day became one plausible-looking bar in today's
  partition. Nothing logged and nothing raised.

  This is the zh-TW `FormatTime("tt")` incident that already cost this repo a
  day of data, and the same diff that shipped proto 1 removed both of the
  detectors that used to catch it: the DLL's own `ts_str` parse (deleted with
  `ts_utc`) and the drift warning it fed. The binding is the only layer left
  that can notice, so it now refuses the frame and names the field; `events()`
  logs the payload and the stream continues. An absent `ts_str` still falls
  back, but says so. Specified in `contract/semantics.md` §1.1.

- **`imputation_parquet.py --output` writes every day in range, not only the
  ones it filled.** A complete day returned `new_table is None` and was skipped
  without being copied, so the "imputed copy" was a delta against `--root`. A
  caller pointing `HistoryStore` at it got zero rows for every complete day and
  no error, because an empty range is an ordinary answer (§2.4) — a six-month
  run with three gap days produced a store that was 97% empty and a summary
  line that read as success. Unchanged days are now copied with `imputed` set
  to False, so the whole tree carries one schema.

- `examples/01_print_events.py` read `event.volume`, renamed to `el_volume` in
  this same release. Examples 02-04 were migrated and 01 was missed; it raised
  `AttributeError` on the first bar.

- `cpp/install-to-tradestation.bat` told the operator to import
  `EL\TS2Python_Exporter.eld`, which the repo does not ship — `.eld` is
  TradeStation's exported binary, while what lives here is the EasyLanguage
  source, compiled in the Development Environment. The operator would fail that
  step, leave the previously compiled indicator on the chart, and it would hit
  the `EL_Init` tombstone and stop publishing with only a Print Log line to
  show for it.

### Changed — BREAKING: one protocol, `proto` 1, and no derived data

- **BREAKING (wire): the version key is now `proto`, and its only value is `1`.**
  Everything older is refused, not read. The key changed name on purpose: the
  superseded wire used `v` and counted to 4, so restarting the numbering at 1 under
  the same key would have made `{"v":1}` a legal opening for two different protocols
  — and the mismatch would have surfaced as plausible-looking wrong numbers rather
  than an error. A frame without `proto` simply is not this protocol.

  **Upgrade the DLL and the `.ELD` together.** They are separate install steps and
  every incompatible combination is caught before anything is published:

  | Deployment | Caught by | What you see |
  | --- | --- | --- |
  | new `.ELD` + old DLL | old DLL has no `EL_Init3` export | `DefineDLLFunc` fails at Verify (F3), naming the missing export |
  | old `.ELD` + new DLL | tombstone `EL_Init` / `EL_Init2` return `-6` | `EL_Init FAILED rc=-6` in the Print Log; the chart never publishes |
  | new `.ELD` + a future DLL | the indicator's `EL_DllVersion()` latch | one Print Log line, then publishing latches off |
  | new binding + old DLL | the `proto` gate in `_parse_payload` | every frame dropped with a message naming `TS2Python.dll` and the `.ELD` |

- **BREAKING (wire): the five quantity fields are EasyLanguage's reserved words,
  verbatim, under `el_*` names.** `vol` and `tc` are replaced by `el_volume`,
  `el_ticks`, `el_upticks`, `el_downticks` and `el_open_interest`, all int64. The
  publisher no longer selects between them.

  It used to. `Volume` and `Ticks` mean opposite things on an intraday chart and a
  daily one, and the indicator swapped fields so `vol` would mean "total shares"
  everywhere. That swap happened off-wire, where no number could reveal it, which is
  the entire reason `pv` / `publisher_version` had to exist. Both are gone. A consumer
  wanting "how much traded" now reads the field `contract/semantics.md` §3.4 names for
  its timeframe — and the `el_` prefix is there so nobody assumes instead of looking.

- **BREAKING (wire): `ts_utc` is removed, and bars no longer carry `bid` / `ask`.**
  Dropping `ts_utc` is a trade, not a redundancy cleanup: it was the DLL's own reading
  of `ts_str`, and the >5s drift warning was the only signal that the two ends
  disagreed about a timezone database. **The DLL also no longer parses `ts_str` at
  all**, so an unparseable time string now travels intact and fails one layer later.
  Both are recorded in `contract/wire.md`. Bar quotes are removed because a live-quote
  function describes the moment of the call, which on a bar is its last print.

- **BREAKING (ABI): `EL_DllVersion()` returns 1. `EL_Init3` is the init export.**
  `EL_Init` and `EL_Init2` remain as **tombstones** returning `-6` — three lines each,
  and they must stay in `TS2Python.def`. `EL_PublishTick` and `EL_PublishBar` kept
  their names while changing signature, and under `__stdcall` a mismatched call
  corrupts the stack rather than returning an error; init is the only interception
  point, because the indicator guards every publish on `InitDone`. `EL_PublishTickEx`
  is deleted.

- **BREAKING (behaviour): nothing in this binding computes a bar.** `BarAggregator`,
  `Resampler`, `bar_coverage`, the Tier-3 cache, the `source = derived:*` provenance
  mechanism and `publisher_version` are all removed, along with
  `aggregate_parquet.py`, `audit_bar_cache.py`, `clear_bar_cache.py` and
  `src/tradestation_data/tools/`. `HistoryStore` reads and only reads: a query with
  nothing behind it returns zero rows and never writes.

  A computed bar is indistinguishable from a published one the moment it is
  persisted, which is what made the provenance machinery necessary in the first
  place. Removing the computation removes the need for it. Consumers wanting derived
  intervals either chart them in TradeStation — a 5-minute chart publishes native 5m
  bars — or build them from what is stored, on their own terms.

- **BREAKING (storage): `BAR_SCHEMA` and `TICK_SCHEMA` changed.** `volume`,
  `tick_count`, `source` and `publisher_version` are gone; the five `el_*` int64
  columns take their place. `union_by_name` and `with_publisher_version` disappear with
  them — the schema no longer evolves, so there is nothing to pad.

  **Partitions written by an earlier version cannot be read by this one, and there is
  deliberately no migration script.** The old intraday `volume` holds up-tick volume
  while `tick_count` holds the total; swapping two columns in place is unrecoverable if
  a run is interrupted, and nothing on disk records which convention a partition was
  written under. Keep old data under a separate root and read it with an older release.

- **`imputation_parquet.py` is non-destructive.** `--output` is now required and the
  result is written to a separate root under its own schema, with an extra
  `imputed: bool` column. `HistoryStore` refuses that directory rather than reading
  invented bars as raw data.

### Added
- **Multiple timeframes: tick · 1m · 5m · 15m · 30m · 1h · 1d.** Bars previously could
  only be 1-minute, because the wire had no field to say otherwise — `kind: "bar_1m"`
  bound the shape and the interval into one string. `kind` (tick/bar) and `tf` are now
  separate, so adding an interval is a new value rather than a change to every binding's
  dispatch. `Bar` gains a `timeframe` field and `BarWriter` partitions on it, not on its
  own constructor argument. A `tf` the binding cannot place is refused, never defaulted.
- **Daily bars are taken natively.** TradeStation's daily bar carries the exchange's
  official OHLC and is split/dividend adjusted; nothing reproduces that, and nothing here
  tries to.
- New DLL export `EL_PublishBar(symbol, ts, bar_type, bar_interval, ...)`, mapping
  EasyLanguage's `BarType`/`BarInterval` to a wire timeframe in `wire_timeframe()`. It
  returns the new code `-5` rather than guessing at an interval it cannot name.
- **`contract/` — a language-neutral wire specification, now the source of truth for this
  repo.** Holds `wire.md`, `bar.schema.json` / `tick.schema.json`, `semantics.md` (the
  rules a schema cannot express: timestamp authority, left-labelled and minute-floored
  `bucket_start`, quote availability, session policy, sequence handling),
  `error_codes.md`, and recorded conformance fixtures. Python is now one binding against
  this spec rather than the definition of it.
- **Gap detection.** Every frame carries `seq` (per-symbol, monotonic) and `sid`
  (publisher session id). PUB/SUB drops silently at both high-water marks, so before this
  a subscriber could not distinguish a quiet market from a lost one.
  `TradeStationELProvider.messages_lost` reports the count. A frame the protocol gate
  refuses is still counted against the sequence, so a refusal never fabricates a gap.
- `EL/` — the EasyLanguage exporter indicator, previously kept in the consuming project.
  It is the upstream origin of the feed and belongs with the provider.
- `contract/fixtures/` with `smoke`, `noquote`, `bars` and `session` cases, all recorded
  from real DLL output. `bindings/python/tests/conformance/` replays them against this
  binding; the expectations are derived from `semantics.md` by hand, never generated from
  the code under test.
- `contract/tools/record.py` — wire inspector and fixture recorder, promoted from
  `scripts/simple_sub.py`. It depends on no binding, which is what qualifies it to record
  the files every binding is checked against.
- Test harness gains `--mode noquote`, reproducing the shape TradeStation emits outside
  live mode.
- CMake presets for Visual Studio 2026 alongside 2022.

### Changed
- **The bucket grid is laid out in `America/New_York` wall-clock time**, anchored at
  09:30 for intraday and 04:00 for daily, so it does not drift against the session when
  the offset changes twice a year. Neither anchor sits in the DST fold. An epoch-aligned
  grid is correct for 5m/15m/30m by coincidence — the ET offset is a whole number of
  hours and 09:30 is a multiple of 30 minutes — but not for the longer frames: `1h`
  produced 09:00 ET buckets, and `1d` split on UTC midnight, which is 20:00 ET, so
  post-market prints landed on the following day. Specified in
  `contract/semantics.md` §2.2 and implemented once, in `domain/timeframe.py`.
- **BREAKING (wire): `bid` / `ask` are `null` when there is no quote.** EL's `InsideBid`
  / `InsideAsk` return 0 whenever there is none — historical replay, any non-live bar,
  and symbols that never carry one. The DLL previously forwarded that 0 verbatim, putting
  what reads as a $0.00 quote on the wire and leaving every binding to remember that 0
  means absent. It now emits `null`, and the binding additionally blanks index/breadth
  symbols whose live numbers mean nothing.
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
- **Python 3.14 is supported and tested.** A fresh `uv sync` had been resolving to 3.14
  and failing, because the lockfile pinned pyarrow 21, which ships no cp314 wheel and so
  fell back to building from source. Unpinning it (24.0.0) fixes that at the source; 3.14
  joins the CI matrix and the classifiers.

- **`1d` moves to one file per symbol**: `bars/timeframe=1d/symbol={SYM}/bars.parquet`,
  no `date=` level, rewritten whole on every flush (merging what is already on disk, later
  wins on a repeated `bucket_start`, replaced atomically). A day partition of daily bars is
  one row inside a file that costs ~2,903 bytes of schema and footer to carry about 60 of
  it; 499 sessions took 1.4 MB where one file takes 7.7 KB. Every reader now asks
  `domain/timeframe.py` which shape a timeframe uses instead of assuming `date=`.

### Fixed
- **A finished day's Parquet file was unreadable until the process stopped.** Both writers
  held one `ParquetWriter` open per partition until `close()`, and a Parquet file has no
  footer before that — so every reader rejects it. A daily chart replaying two years left
  499 such files, 943 bytes of unreadable prefix each. Both writers now *seal* a partition
  — flush, close, mark — as soon as an event for a later day of the same series arrives; a
  late event for a sealed day is dropped with a warning rather than reopening, because
  `pq.ParquetWriter` truncates on open and losing one bar beats losing the session.
- **Bars cost one Parquet row group each.** `BarWriter` wrote every bar the moment it
  closed, and each `write_table` is a row group carrying a full set of per-column headers
  and statistics. A real session's 78 five-minute bars took 145,977 bytes as 78 row groups
  and 5,936 as one. `BarWriter` now buffers like `TickWriter`, with `max_buffered_bars` /
  `max_flush_seconds` (default 1,000 / 60s — the same one-minute crash-loss bound the
  unbuffered version claimed), and `ParquetBarSink` advertises `flush` so the runtime's
  flush loop drives it.
- **A daily chart published nothing.** TradeStation 10 reports `BarInterval = 0` on a
  daily chart, not `1`, and `wire_timeframe()` accepted only `(BarType 2, BarInterval 1)`
  — so `EL_PublishBar` returned `-5`, the indicator printed one line and went idle for the
  life of the chart while minute charts worked normally. The number was never measured
  against a live install; the harness asserted the value the ABI had assumed. Both `0` and
  `1` now map to `1d`, `2` and above are still refused (on `BarType 2` the interval is a
  day multiplier, and a 2-day bar in the `1d` partition is indistinguishable from real
  daily data), and `bars.jsonl` carries both readings.
- **A 5-minute chart silently corrupted the 1-minute partition.** `BarType = 1` covers
  every intraday minute chart — 1/5/15/60-minute are all `BarType 1`, told apart only by
  `BarInterval`, which the indicator never read — and the DLL stated `bar_1m`
  unconditionally. Bars from a 5-minute chart were written to `timeframe=1m`, and the
  resampler then derived "5m" from data that was already 5m. Nothing reported an error.
  The indicator now forwards `BarInterval` and the DLL refuses intervals it cannot name.
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
