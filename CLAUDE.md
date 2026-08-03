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

- **Wire `proto` 2 / DLL ABI 2. There is exactly one of each, and nothing else is
  supported.** The version key is `proto`, not `v`: the superseded wire used `v` and
  counted to 4, so restarting under the same key would have made `{"v":1}` a legal
  opening for two different protocols. A frame without `proto` is simply not this
  protocol, and `_parse_payload` refuses it with a message naming the fix. **Upgrade
  DLL and `.ELD` together** — they are separate install steps and the indicator binds
  `EL_Init3` and `EL_Publish`, neither of which an older DLL exports.
- **One publish export, one frame shape.** `EL_Publish` carries everything
  TradeStation hands the indicator for a data point: `Date`+`Time`, `BarType`,
  `BarInterval`, `Category`, OHLC, the five `el_*` words, and `InsideBid`/`InsideAsk`.
  There is no tick/bar split, no `kind`, and no `tf`.

  The split used to drop fields by chart type — tick sent `Close` alone and no chart
  identity; bar sent OHLC and no quote. Both were the publisher deciding which numbers
  were meaningful where, off the wire, where nothing downstream could see the decision.
  TradeStation supplies the same reserved words on every chart; a 1-tick series has
  `Open = High = Low = Close`, and that is a fact worth landing.
- `EL_Init`, `EL_Init2`, `EL_PublishTick` and `EL_PublishBar` survive as **tombstones**
  returning `-6`. They are three lines each and must stay in `TS2Python.def`: the two
  publish names once kept their spelling across a signature change, and they are
  `__stdcall`, so a mismatched call corrupts the stack rather than returning an error.
  Init is the interception point that matters — the indicator guards every publish on
  `InitDone`, which stays False when `InitRC < 0`.
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
- **No strategy / broker / risk wiring lives here.** `domain/` is `Bar` and nothing
  else — one type, because the wire carries one shape. A new domain type with no
  counterpart on the wire means scope is leaking back in.
- **There is no timeframe vocabulary any more.** `domain/timeframe.py`,
  `align_bucket_start`, `SESSION_ANCHORED_TIMEFRAMES` and the 04:00 ET daily anchor
  were all deleted. A chart is named by EasyLanguage's own `BarType` / `BarInterval`,
  verbatim, and the store partitions on that pair — so an interval this binding has no
  word for (a 2-minute chart, a weekly one) is stored rather than refused.

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
python scripts/verify_parquet.py    --start-date YYYY-MM-DD --end-date YYYY-MM-DD
python scripts/imputation_parquet.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD --output data/imputed --dry-run
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
every BarType/BarInterval pair, none refused), `session` (RTH first/last bar),
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
                                            │  one frame shape, whatever the chart
                                            ▼
                              IngestionRuntime._handle_provider_bar
                              (intra-bar buffer, dedupe, replace-last)
                                            │
                          closed point ◀────┘
                                  │
                                  ├──▶ MarketSnapshot.on_bar
                                  ├──▶ SinkPipeline.on_bar  ──┬──▶ ParquetBarSink (default)
                                  │                           ├──▶ InMemorySink / CallbackSink
                                  │                           └──▶ user-defined sinks from sinks.yaml
                                  └──▶ optional on_bar callback

Background loops in IngestionRuntime: ingest / advance (wall-clock) / flush (sinks that advertise it) / heartbeat.
```

**Nothing here builds a point from another.** There is one path and TradeStation is
on the other end of it. Nothing here aggregates, resamples, backfills or caches — see "What this
binding does not do" below.

### Pluggable sinks (`sinks/`)

`IngestionRuntime` writes to a `SinkPipeline` rather than directly to `BarWriter`. The pipeline broadcasts every closed point to every registered sink and **isolates per-sink exceptions** (one bad sink doesn't take down the others). The pipeline is built from `config/sinks.yaml` via `tradestation_data.sinks.registry.build_pipeline_from_config()`; users register custom sinks by pointing the YAML `class:` field at any importable `module:attr` returning a Sink protocol implementation. Built-ins:

- `ParquetBarSink` — a thin adapter over `BarWriter` under `storage/`. It buffers and advertises `flush` — bars used to be written one at a time, which cost one Parquet row group per bar. It also *seals* a partition (flush + close its file) on either of two signals: a point for a later day of the same series arrives, or the day is over in ET and nothing has arrived for it in a whole `max_flush_seconds`. A `ParquetWriter` left open has no footer, so its file is unreadable to every reader until then — and the first signal alone leaves the newest day of a finished replay open until Ctrl+C.
- `InMemorySink` — bounded per-symbol deques; for tests / notebook use only.
- `CallbackSink` — dynamic Python callback dispatch with per-symbol or catch-all registration. Instances are tracked in a module-level `WeakValueDictionary`; user code does `get_sink(name)` to look up the instance declared in `sinks.yaml` and register handlers on it. `close()` eagerly removes the registry entry so a subsequent `get_sink()` raises `KeyError` immediately, not after GC.

The `tick_writer` / `bar_writer` constructor parameters on `IngestionRuntime` are gone — pass a `SinkPipeline` instead. `BarWriter` still lives under `storage/` because `ParquetBarSink` wraps it; it is *not* the public interface anymore.

The DLL exports one publisher, `EL_Publish`; a point always arrives whole. EL's "Update every tick" mode re-sends the same `(symbol, bar_time)` many times per minute, so `_handle_provider_bar` buffers the latest per chart and only emits when the next bucket arrives, on wall-clock advance past `bar_time + grace` (bar_time IS the close — adding an interval on top released every point one interval late, and a daily point a day late), or on shutdown. `_last_emitted_direct_bucket` blocks history-replay duplicates after a TS chart reload.

**Tick charts (`bar_type` 0) bypass the buffer entirely.** Its precondition is that `bar_time` names the bar uniquely, and with minute-resolution `ts_str` every print inside a minute shares one `bar_time` — routed through the buffer, a live 1-tick chart lost nearly its whole stream to replace-then-dedupe. Every tick-chart frame is forwarded on arrival; the wire's `ts` (stored per row) is what orders prints within a minute, and dedupe is the consumer's call.

### What this binding does not do

**It receives, labels and stores. Nothing else.** No aggregation, no resampling, no
backfill, no cache, no imputation into the live store — those are the consumer's
business, and every one of them used to live here. `BarAggregator`, `Resampler`,
`bar_coverage`, the `source = derived:*` provenance mechanism and `publisher_version`
are all gone.

The reason is that a computed bar is indistinguishable from a published one the moment
it is persisted. `HistoryStore.load_bars` therefore answers zero rows for an interval
TradeStation never published, and never writes on a read path.
`tests/test_history_store.py::test_load_bars_never_derives_bars_it_was_not_given` pins it.

### Storage layout (`storage/`)

Hive-partitioned Parquet under `data/`. Everything on disk came off the wire:

| Path | Producer |
|---|---|
| `bars/bartype={N}/interval={M}/symbol={SYM}/date={YYYY-MM-DD}/bars.parquet` | `ParquetBarSink` (→ `BarWriter`) |
| `bars/bartype=2/interval={M}/symbol={SYM}/bars.parquet` | `ParquetBarSink` — flat, no `date=` level |

`BarType 2` (daily) is flat because a day partition of daily bars is one row inside a
~2.9 KB file; the file is rewritten whole on each flush. Any reader building a bars
path must branch on `bar_type == 2` rather than assuming `date=`.

`HistoryStore` is the read-side facade — polars `scan_parquet(hive_partitioning=True)`
over the glob. `BAR_SCHEMA` / `TICK_SCHEMA` carry both a UTC and an
`America/New_York` timestamp; both are persisted so downstream tooling never has to
convert at query time. `_as_utc` normalises a caller's bound to UTC — a naive one
means ET, and an aware one must still be converted because polars refuses to compare a
UTC column against a literal in another zone.

### The five `el_*` quantities

**The wire carries EasyLanguage's reserved words verbatim, one column each:
`el_volume`, `el_ticks`, `el_upticks`, `el_downticks`, `el_open_interest`.** The
publisher selects nothing and converts nothing; the binding must not either.

**There are TWO inversions between intraday and daily, not one.** `Volume`/`Ticks` is
the well-known pair: intraday, `Volume` is *up-tick share volume only* and `Ticks` is
*total share volume*; daily, they swap. `DownTicks`/`OpenInt` is the second, and it
runs the other way — intraday `OpenInt` borrows `DownTicks`'s meaning, daily
`DownTicks` borrows `OpenInt`'s. Both come straight from TradeStation's own
reserved-word page, transcribed into `contract/semantics.md` §3.4 with the live
measurements beside them. Do not go looking for these definitions anywhere else, and
do not re-derive them: §3.4 is the copy.

This is a fact about TradeStation, not a thing to fix: the exporter used to swap the
fields so `vol` would mean one thing everywhere, and `pv` existed solely because that
swap happened off-wire where no number could reveal it. Both are gone.

Two consequences worth knowing before reading a column:
- Measured on live SPY, @ES, VXX and an SPY option, **`el_open_interest` returns
  `el_downticks` on every intraday chart** whatever the category. It is not open
  interest there.
- **A futures daily bar's `el_downticks` IS open interest.** Summing that column
  across futures dailies sums OI, not volume.

The `el_` prefix is the point. A column called plain `volume` invites the reading that
already cost this repo a systematically halved volume column, and the numbers looked
entirely plausible throughout. A consumer wanting "how much traded" reads the field the
§3.4 table names for its `bar_type` and `category` — which is why `category` is on
the wire at all (§3.5).

**Do not "verify" a daily bar by summing intraday bars.** The daily figure is the
exchange's official consolidated total (late prints, block trades, dark pool, closing
cross); intraday is what the live SIP stream happened to carry. Two different
measurements — §3.4 has the four reasons.

`--data-root` is only a fallback path used when `--sinks-config` is missing — the YAML's per-sink `root` parameter wins otherwise. When you need to redirect output, edit `sinks.yaml`, not the CLI flag.

### Timestamps and sessions

**These are contract rules, not local choices — `contract/semantics.md` is authoritative
and every binding must agree. Change them there first.**

- `ts_str` (`yyyy-MM/dd-HH:mm:ss`, 24-hour) is **authoritative** for `Bar.bar_time`:
  parsed as `America/New_York`, converted to UTC, **seconds and all**. `ts` is the
  DLL's receive clock and a last-resort fallback only — during historical replay every
  bar shares one `ts` and would collapse onto a single bucket. 24-hour is deliberate —
  `hh:mm:ss tt` broke on zh-TW Windows hosts where `FormatTime("tt")` emits localised
  AM/PM.
- **The seconds are real, and the publisher must use `BarDateTime` to produce them.**
  EL's `Date`/`Time` reserved words carry no seconds at all: on a 30-second chart
  (`BarType` 14) two adjacent, distinct, already-closed bars both formatted as
  `07:20:00`, the intra-bar buffer read the second as an update of the first, and one
  bar per minute was all that survived. `BarDateTime.Format(...)` gives `07:20:00` and
  `07:20:30`. `bar_time` used to be floored to the minute here, which was a harmless
  no-op while `Time` had nothing to floor — and became data loss the moment it did.
  `contract/semantics.md` §1.3 has the live measurement; `bars.jsonl`'s last two
  frames are the fixture. Never `elsystem.DateTime.CurrentTime`/`.Now` — those read
  the host clock, not the bar.
- **The wire's `ts` (receive clock) lands verbatim as its own column.** On a tick
  chart it is the only intra-second time there is — a second can hold many prints,
  so they share one `bar_time` and `ts` orders them.
- **The DLL no longer parses `ts_str`, so it no longer validates it.** `ts_utc` is gone
  from the wire: it was `zoned_time`'s reading of the same string that Python parses with
  `ZoneInfo`, and the >5s drift warning was the only signal that the two ends disagreed
  about a timezone database. Dropping it was a trade, recorded in `contract/wire.md` —
  an unparseable time string now arrives intact and fails one layer later, here.
- **A bar's time is the publisher's, verbatim.** `ts_str` parses as ET, converts to
  UTC, and lands as `bar_time`. There is no shift, no grid snap and no rounding.
  EasyLanguage's `BarDateTime` is the bar's *close*, so `bar_time` is a close time;
  a consumer wanting left edges subtracts for itself.

  This binding used to convert: subtract a minute, then snap onto a 09:30-anchored
  grid. It lost a bar a day. TradeStation restarts its intraday grid at the RTH open
  and close, so a 60-minute chart on a 06:00 session publishes fifteen bars including
  two stubs — and closes 09:00 and 09:30 both snapped onto 08:30, the second
  overwriting the first. No grid can fix it: the segment lengths follow the chart's
  session template, which the wire does not carry. `contract/semantics.md` §2 has
  the measurement.
- `aggregation/session.py` owns session-edge logic. US equity session = 09:30–16:00 ET;
  bars before 04:00 ET belong to the *previous* session. Per-symbol retention via
  `SessionPolicy`: `breadth` indices reset daily, everything else retains 60 min of
  pre-market. Defaults from `symbols.yaml::category`, overrides in `runtime/config.py`.
- **Every point carries `bid`/`ask`, and the binding blanks nobody's.** EL's
  `InsideBid`/`InsideAsk` return 0 when there is no quote (historical replay, non-live
  mode, breadth indices); the DLL normalises non-positive values to JSON `null`, so
  "absent" is spelled once for every caller of the C ABI.

  There used to be a second rule: a hard-coded list of index/breadth symbols whose
  quotes the binding discarded as meaningless. It was a guess in both directions —
  `VXX` was on it, and VXX is a tradeable ETN that reported 567,776 shares in one bar,
  so its real quote was being thrown away. It was also an opinion about what a number
  means, which is the consumer's to hold. `category` (4 = Index) now travels on every
  frame, so a consumer wanting that behaviour has a fact to key off.

  Bars used to carry no quote at all, on the grounds that a live-quote function
  describes the moment of the call rather than the bar. Same problem: true, and not
  this transport's call to make.

### Windows-specific event loop

pyzmq's asyncio integration uses `loop.add_reader()`, which the default Windows ProactorEventLoop does **not** support — SUB sockets connect but `recv_multipart()` never wakes, with no error to explain it. Every entry point must force a selector loop on `win32`. Preserve this when adding new ones.

The supported spelling is `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)` — `runtime/main.py` and `examples/_compat.py` both use it; copy `_compat.run()`. Do **not** reach for `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`: 3.14 deprecates the whole policy API and 3.16 removes it, which would stop the CLI from starting on the only platform TradeStation runs on.

`tests/conftest.py` is the one remaining policy caller and cannot move yet — pytest-asyncio owns loop creation there and 1.3.0 exposes no `loop_factory` hook. `pyproject.toml`'s `filterwarnings` carries three **narrow** ignores for exactly those messages; the blanket `ignore::DeprecationWarning` that used to sit there is what let this rot unnoticed, so do not widen them back.

### Shutdown ordering

`IngestionRuntime.run()` deliberately cancels tasks → closes provider → awaits tasks → calls `_shutdown()` in an *outer* finally; `runtime/main.py` then calls `pipeline.close()` one more time as belt-and-suspenders. `_shutdown()` drains the direct-bar buffer **before** closing the sink pipeline, so any final closed bar still reaches every sink. A second Ctrl+C while `zmq ctx.term()` was blocking previously skipped sink close and left `bars.parquet` without a footer — don't simplify the nested-finally structure without understanding that failure mode.

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
