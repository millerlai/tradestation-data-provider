# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

**The product is the wire protocol, not the Python package.** TradeStation → an
EasyLanguage indicator → a C++ bridge DLL → ZeroMQ PUB. Any language can subscribe;
Python is the reference binding and the template for the next one.

```
contract/            wire spec, semantics, conformance fixtures   <- source of truth
EL/                  EasyLanguage exporter indicator
cpp/                 bridge DLL (Win32 x86) + standalone test harness
bindings/python/     reference binding: ingestion runtime, sinks, Parquet store
  └ examples/        four runnable scripts; two need no DLL (see its README)
docs/                architecture.md, migration/
```

**Rules that live only inside a binding are bugs.** They get missed by the next
implementation — this repo has already had a spec drift into describing fields the DLL
no longer emitted, unnoticed, because nothing checked. Anything a second binding would
have to guess belongs in `contract/semantics.md`, with a fixture.

- Wire v4 / DLL ABI 9 is current; v1, v2 and v3 are superseded but **still supported** — the DLL
  sits in the user's TradeStation install and does not update when a binding does. v4 added `pv`,
  which says *which convention produced the numbers* as opposed to `v`, which says where the
  fields are: the indicator is a third independently-versioned piece, and an exporter older than
  the §3.4 volume fix puts up-tick volume in `vol` with nothing in the number to say so.
  Upgrading is therefore directional — **binding first, then DLL + `.ELD`** — because a binding
  that does not know v4 refuses the frames outright.
- Python 3.12–3.14, managed with **uv**; all three are in the CI matrix. 3.11 was
  dropped so the Windows event loop can be selected with
  `asyncio.run(loop_factory=...)` (3.12+) instead of the policy API, which 3.14
  deprecates and 3.16 removes. There is
  deliberately no `.python-version` — one would override `uv sync --python <v>`, leaving
  `uv run` on a different interpreter than the one just synced.
- Package `tradestation_data` under `bindings/python/src/`. Console script
  `tradestation-data-ingest` → `tradestation_data.runtime.main:main`.
- READMEs are split EN / zh-TW at the repo root and again under `bindings/python/`.
- Wrapper scripts in `bindings/python/scripts/` shell out to `uv run` via `_common.py`.
- `contract/tools/record.py` deliberately imports **no binding** — that independence is
  what qualifies it to record the fixtures every binding is checked against.
- **No strategy / broker / risk wiring lives here.** `domain/` is `Tick`, `Bar`, and
  `Timeframe` — the value range of the wire, `tf` included. A new domain type with no
  counterpart on the wire means scope is leaking back in.
- `domain/timeframe.py` is the single source for the timeframe vocabulary: the enum,
  `NATIVE_ONLY_TIMEFRAMES` (never computed), `SINGLE_FILE_TIMEFRAMES` (no `date=` level),
  the minutes table, the wire allow-list, the Tier-3 default, and `align_bucket_start`
  (the Python twin of `resampler._bucket_expr`). Adding an interval should be one edit
  there, not six scattered ones — and any new interval must divide one hour, or the
  intraday grid stops surviving DST (`contract/semantics.md` §2.2).

## Commands

All Python commands run from `bindings/python/`.

```powershell
uv sync --extra dev

# Ingestion (DLL must already be publishing)
python scripts/run_ingestion.py
python scripts/run_ingestion.py --sinks-config config/sinks.yaml
python scripts/run_ingestion.py --no-storage --log-level DEBUG
python scripts/run_ingestion.py --print-bars 5

# Offline Parquet tools (uv-run wrappers)
python scripts/aggregate_parquet.py --symbol all --timeframe 5m --input data/bars/timeframe=1m --output data/bars
python scripts/verify_parquet.py    --start-date YYYY-MM-DD --end-date YYYY-MM-DD
python scripts/imputation_parquet.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dry-run
python scripts/audit_bar_cache.py ; python scripts/clear_bar_cache.py
python scripts/dedupe_bars.py ; python scripts/dump_parquet.py

# Tests / lint / types
uv run pytest                                  # full suite, asyncio_mode=auto
uv run pytest tests/conformance                # contract fixtures only
uv run ruff check . ; uv run ruff format .
uv run mypy                                    # strict on src/

uv build
```

C++ and wire inspection, from the repo root:

```powershell
cd cpp
.\setup-build-env.bat               # once per clone; idempotent
.\verify-build-env.bat              # exit 0 = ready; names the fix for anything missing
.\build.bat                         # Release x86 + x64; also `Debug` / `all` / `--x86` / `--rebuild`

cmake --preset x86-release          # or x86-release-vs2022
cmake --build --preset x86-release

# The MSBuild path — note the SOLUTION platform is x86, not Win32
msbuild TS2Python.sln /p:Configuration=Release /p:Platform=x86

# Drive the DLL without TradeStation, then watch or record the wire.
# Leave enough warmup for the subscriber to attach — PUB drops with no subscriber.
# build.bat / VS write to cpp\Release\; cmake to cpp\build\x86-release\Release\.
cpp/Release/TS2Python_TestHarness.exe --mode smoke --warmup-ms 8000
python contract/tools/record.py
python contract/tools/record.py --count 6 --quiet --record contract/fixtures/smoke.jsonl
```

Harness modes: `smoke` (3 topics + one bar), `noquote` (bid/ask absent, the
history-replay shape, on both an index and a non-index symbol), `bars` (every
non-1m `tf` plus the `-5` refusal path), `session` (RTH first/last bar),
`stress`, `multithread`. Each fixture's mode and frame count is tabulated in
`contract/fixtures/README.md`.

Pytest is configured with `pythonpath = ["src"]`, `asyncio_mode = "auto"`, and
`filterwarnings = ["error", ...]` — **a new warning fails the build**; fix the cause
rather than widening the filter.

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

- `ParquetBarSink`, `ParquetTickSink` — thin adapters over the legacy `BarWriter` / `TickWriter` under `storage/`; on-disk layout and schema are unchanged. **Both buffer and both advertise `flush`** — bars used to be written one at a time, which cost one Parquet row group per bar. Both writers also *seal* a partition (flush + close its file) as soon as an event for a later day of the same series arrives: a `ParquetWriter` left open has no footer, so its file is unreadable to every reader until the process stops.
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
| —    | `bars/timeframe=1d/symbol={SYM}/bars.parquet`                            | `ParquetBarSink` only — **published, never derived** |

**`1d` is not a tier, it is data.** `NATIVE_ONLY_TIMEFRAMES` in `domain/timeframe.py` names it: TradeStation's daily bar carries the exchange's official close and the split/dividend adjustment, which no rollup reproduces, so nothing computes one. `load_bars` returns empty rather than building it, `rebuild_bar_cache` and `aggregate_parquet.py` refuse it, and `clear_bar_cache.py` leaves it alone. Its layout is flat — no `date=` level, one file per symbol rewritten whole on each flush (`SINGLE_FILE_TIMEFRAMES`), because a day partition of daily bars is one row inside a ~2.9 KB file. Any reader building a bars path must ask `domain/timeframe.py` which shape applies rather than assuming `date=`.

**Do not "verify" a `1d` bar by summing intraday bars.** The daily `vol` is the exchange's official consolidated total (late prints, block trades, dark pool, closing cross); intraday is what the live SIP stream happened to carry. They are two different measurements and will not match — `contract/semantics.md` §3.4 has the four reasons.

**EasyLanguage's `Volume` and `Ticks` mean opposite things intraday and daily**, which is what made that gap look impossible (3× on SPY). Intraday, `Volume` is *up-tick share volume only* and `Ticks` is *total share volume*; daily, they swap. Confirmed against TradeStation's own reserved-word docs and measured on a live 100-tick chart — §3.4 has the table and the evidence. The indicator now picks the field the chart type requires, so wire `vol` is total share volume everywhere; `tc` is a trade count **only on `1d`**, and even there it is under suspicion (byte-for-byte equal to `vol` across 499 SPY rows). Never read `tc` as a trade count on intraday — EL exposes no reserved word for it, and the field goes out as 0. Measured on the collected SPY data, correcting the field takes the gap from **3.00× to 1.33×** — the remainder is the four reasons above, and the two numbers are still not meant to be equal.

**Bars written before this fix have an understated intraday `vol`** — it holds up-tick volume — while `tick_count` holds the total. On the data in `bindings/python/data/`, `tick_count / volume` sits at 1.85–2.25 across 1m and 5m. Old partitions are recoverable without re-collecting: the total is already there under the wrong name. There is deliberately no migration script — swapping two columns in place is the kind of edit that is unrecoverable if the store is half-old and half-new, and nothing on disk records which convention a partition was written under.

`HistoryStore` is the read-side facade — DuckDB + Polars over the Parquet glob; `load_bars` falls through to `Resampler` and persists the result. `BAR_SCHEMA` / `TICK_SCHEMA` carry both `*_utc` (`UTC`) and `*_et` (`America/New_York`) timestamps — both are persisted so downstream tooling never has to convert at query time.

**Both schemas end with a nullable `publisher_version`** — wire v4's `pv`, recording which
convention produced `volume`. `NULL` means the row predates the column and is *unknown*, which is
not the same as `0` ("the publisher declared itself undeclared"); do not collapse them. Two rules
follow from the store being permanently mixed:

- **Every `read_parquet` over a glob needs `union_by_name = true`.** Without it DuckDB takes the
  *first* file's schema and silently drops any column the others add — one legacy partition in
  range erases `publisher_version` for the whole query, no error. Measured on duckdb 1.5.3;
  `tests/test_publisher_version.py` pins it.
- **Anything that reads a partition back and `.cast(BAR_SCHEMA)`s it must pad first** — the cast
  compares field lists and raises, and both rewrite paths read that as "not our file, leave it
  alone". `bar_writer.with_publisher_version` is the pad; without it a legacy partition silently
  stops accepting new rows. Tier 3 caches are derived; `clear_bar_cache.py` deletes them safely and `audit_bar_cache.py` cross-checks them against a Tier-1 rebuild. **Native bars are not only 1m** — a 5-minute chart publishes native 5m bars into the Tier-3 directory, which is why the provenance guard (`source` = `derived:*`) is what decides deletion, never the path.

`--data-root` is now only a fallback path used when `--sinks-config` is missing — the YAML's per-sink `root` parameter wins otherwise. When you need to redirect output, edit `sinks.yaml`, not the CLI flag.

### Timestamps and sessions

**These are contract rules, not local choices — `contract/semantics.md` is authoritative
and every binding must agree. Change them there first.**

- `ts_str` (`yyyy-MM/dd-HH:mm:ss`, 24-hour) is **authoritative** for `Bar.bucket_start`:
  parsed as `America/New_York`, converted to UTC, then **floored to the minute**. The
  flooring is invisible in normal operation because EL sends aligned times; the smoke
  fixture catches it because the harness reuses a tick's `:45` timestamp. `ts_utc` from
  the DLL is only a cross-check (>5s drift logged, never raised). 24-hour is deliberate —
  `hh:mm:ss tt` broke on zh-TW Windows hosts where `FormatTime("tt")` emits localised
  AM/PM.
- Bars are **left-labelled**: `bucket_start` covers `[t, t+step)`, so an RTH 1m session
  runs 09:30…15:59, not 09:31…16:00. **The wire is right-labelled** — EasyLanguage's
  `Time` is the bar's *close* and the indicator forwards it verbatim — so `_parse_bar`
  subtracts one `tf` before the grid snap, exempting `SESSION_ANCHORED_TIMEFRAMES`
  (`1d`), where alignment discards the time-of-day for the 04:00 ET anchor anyway and
  subtracting would move the bar into the previous session. That step went missing once
  and every stored 1m file came out as 09:31…16:00: same 390 rows, all values plausible,
  nothing raised. `contract/fixtures/session.jsonl` is what pins it — it publishes the
  real 09:31 / 16:00 shape, so a fixture that emits the contract's own labels is not a
  test, it is a tautology.
- Ticks use the DLL's receive-side `ts` (UTC epoch) as authoritative.
- `aggregation/session.py` owns session-edge logic. US equity session = 09:30–16:00 ET;
  bars before 04:00 ET belong to the *previous* session. Per-symbol retention via
  `SessionPolicy`: `breadth` indices reset daily, everything else retains 60 min of
  pre-market. Defaults from `symbols.yaml::category`, overrides in `runtime/config.py`.
- **Quotes are absent in two different ways.** EL's `InsideBid`/`InsideAsk` return 0 when
  there is no quote (historical replay, non-live mode, breadth indices); the DLL
  normalises non-positive values to JSON `null` on wire v2. Separately, index/breadth
  symbols (`DEFAULT_INDEX_SYMBOLS`) may carry live numbers that still mean nothing, and
  the binding invalidates those. `_quote_or_none()` handles both, plus v1's bare `0.0`.

### Windows-specific event loop

pyzmq's asyncio integration uses `loop.add_reader()`, which the default Windows ProactorEventLoop does **not** support — SUB sockets connect but `recv_multipart()` never wakes, with no error to explain it. Every entry point must force a selector loop on `win32`. Preserve this when adding new ones.

The supported spelling is `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)` — `runtime/main.py` and `examples/_compat.py` both use it; copy `_compat.run()`. Do **not** reach for `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`: 3.14 deprecates the whole policy API and 3.16 removes it, which would stop the CLI from starting on the only platform TradeStation runs on.

`tests/conftest.py` is the one remaining policy caller and cannot move yet — pytest-asyncio owns loop creation there and 1.3.0 exposes no `loop_factory` hook. `pyproject.toml`'s `filterwarnings` carries three **narrow** ignores for exactly those messages; the blanket `ignore::DeprecationWarning` that used to sit there is what let this rot unnoticed, so do not widen them back.

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
- **`inproc://` requires both ends to share one `zmq.Context`.** Anything creating its own context (`contract/tools/record.py` calls `zmq.Context.instance()`) cannot be driven over that fixture and will block forever. PUB also drops whatever is sent before a subscriber exists, so a test cannot publish first and connect after. Test such tools through an extracted pure function instead — that is why `record.fixture_entry()` exists.
- `tests/conformance/` replays `contract/fixtures/` against this binding. Expectations there are derived from `contract/semantics.md` **by hand** and must never be regenerated from this code: expectations produced by the code under test only prove it agrees with itself. When conformance fails, decide which side is wrong before touching either — twice so far the spec was.

## Conventions

- Lint/format: ruff (line length 100, target py311; rules `E,F,W,I,N,UP,B,SIM,RUF`; tests get `N802/N803` relaxed). Run it from `bindings/python/`. The repo-root `.ruff.toml` exists only to stop a top-level `ruff check .` from falling back to defaults and rewriting the vendored vcpkg checkout — which has happened.
- Types: mypy strict on `src/`; `tests/` is excluded.
- C++ builds Win32 (x86) only — TradeStation is a 32-bit process. `DEFINE_SYMBOL` on the `TS2Python` target supplies `TS2PYTHON_EXPORTS`; CMake's automatic `<target>_EXPORTS` differs in case and leaves the header on the `dllimport` branch.
- The MSBuild build imports vcpkg **from the submodule** via `cpp/vcpkg-local.props` + `.targets`, and sets `VCPkgLocalAppDataDisabled` so `%LOCALAPPDATA%\vcpkg\vcpkg.user.props` is ignored. Do not "fix" a build by running `vcpkg integrate install` — that global file holds an absolute path to one checkout, and a stale one silently skips its own `Exists()` guard, which is precisely the `C1083: 'zmq.hpp'` this arrangement exists to prevent. `cpp/verify-build-env.bat` reports it.
- `PlatformToolset` comes from `$(TS2PythonToolset)`, declared in each vcxproj's Globals group (it has to precede the configuration groups that read it). Default `v145` (VS 2026); override with `/p:TS2PythonToolset=v143`.
- **Never point `VcpkgInstalledDir` at a root shared by both triplets.** vcpkg manifest mode reconciles an install root against the current plan, so an x64 build would delete the x86 packages and the next x86 build fails with C1083 on a machine where it just worked. The default per-triplet root — which is why the triplet appears twice in `vcpkg_installed\x86-windows\x86-windows\include` — is what keeps them isolated.
- Coverage: branch coverage on `src/tradestation_data`, excludes match `pragma: no cover`, `raise NotImplementedError`, `if TYPE_CHECKING`, ellipsis-only stubs.
- Dataclasses: use `slots=True` (and `frozen=True` for value types) — the codebase is consistent on this.
- Logging: stdlib `logging` everywhere, with structured kwargs via `extra={...}`. `runtime/main.py` ships both a plain formatter (`_ExtraDumpFilter` flattens `extra` into the message tail) and `--log-json` (`_JsonFormatter`). Keep new log sites in the same shape. Sink-related events use the conventions `sink_on_tick_failed`, `sink_on_bar_failed`, `sink_flush_failed`, `sink_close_failed`, `sinks_loaded`, `sinks_config_invalid` — match these when adding new sink paths.
- Sinks: subclass `BaseSink` (or implement the `Sink` Protocol directly), accept `name=` as a keyword argument in `__init__`, and assign it to `self.name`. `should_flush` defaults to `False`; only override it on sinks that actually buffer.
- File encoding when writing on Windows: PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM; pass raw bytes through `Write` / `Edit` (our tools) or use Python to write UTF-8 without BOM. A BOM in `pyproject.toml` makes the TOML parser reject the file.
