# tradestation-data-provider

[![CI](https://github.com/millerlai/tradestation-data-provider/actions/workflows/ci.yml/badge.svg)](https://github.com/millerlai/tradestation-data-provider/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/millerlai/tradestation-data-provider/blob/main/bindings/python/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)

> 📖 [繁體中文版 README](README.zh-TW.md)

A pure-Python data pipeline that subscribes to a TradeStation EasyLanguage feed (over a C++/ZeroMQ bridge) and routes ticks and 1-minute bars to a **pluggable set of output sinks** — Parquet, in-memory, user callbacks, or anything you implement.

This is the **reference binding** for the wire protocol defined in [`contract/`](../../contract/) — one subscriber among the languages that repo supports. The EasyLanguage indicator and the C++ DLL that publish the feed live in [`EL/`](../../EL/) and [`cpp/`](../../cpp/).

**No strategy / broker / risk wiring lives here** — the runtime is data-collection-only.

> Before changing how anything on the wire is parsed, read [`contract/semantics.md`](../../contract/semantics.md). Rules that live only in this package are the ones the next binding gets wrong.

## Why use it

- **Plug your own output format.** Declare a sink in `config/sinks.yaml`, point it at any `module:attr` you can import, and the runtime fans every tick/bar to it. No fork needed.
- **Defaults that match the historical layout.** Built-in `ParquetBarSink` / `ParquetTickSink` keep the same Hive-partitioned schema as before.
- **Receive ticks/bars in your own code.** `CallbackSink` lets you register Python functions per symbol or catch-all, dispatched synchronously from the ingest loop.
- **Battery-included tooling.** Aggregate, verify, audit, dedupe, and back-fill the resulting Parquet store with the scripts under [`scripts/`](scripts/).
- **Checked against the shared contract.** `tests/conformance/` replays recorded DLL output from [`contract/fixtures/`](../../contract/fixtures/) and asserts this binding matches expectations derived independently of it.

## Architecture

```mermaid
flowchart TD
    DLL["TradeStation EL DLL"]
    Provider["TradeStationELProvider<br/>(asyncio ZMQ SUB)"]
    Runtime["IngestionRuntime<br/>(intra-bar buffer · dedupe)"]
    Snapshot["MarketSnapshot"]
    Aggregator["BarAggregator"]
    Pipeline[["SinkPipeline · fan-out"]]
    OnBar(["optional on_bar callback"])

    PBar["ParquetBarSink<br/>(default)"]
    PTick["ParquetTickSink<br/>(default)"]
    Memory["InMemorySink"]
    Callback["CallbackSink"]
    Custom["Your custom sinks"]

    DLL -- "ZMQ PUB" --> Provider
    Provider -- "Tick" --> Runtime
    Provider -- "Bar (EL_PublishTickEx)" --> Runtime
    Runtime -- "Tick" --> Snapshot
    Runtime -- "Tick" --> Aggregator
    Aggregator -- "closed Bar" --> Runtime
    Runtime -- "closed Bar" --> Snapshot
    Runtime -- "Tick / closed Bar" --> Pipeline
    Runtime --> OnBar
    Pipeline --> PBar
    Pipeline --> PTick
    Pipeline --> Memory
    Pipeline --> Callback
    Pipeline --> Custom
```

Background loops inside `IngestionRuntime`: **ingest** / **advance** (wall-clock) / **flush** (sinks that ask for it) / **heartbeat**.

> The diagram above renders on GitHub; on PyPI it displays as a Mermaid code block.

## Install

### As a dependency (pip / uv / poetry)

From PyPI (once published):

```bash
pip install tradestation-data-provider
uv add tradestation-data-provider
poetry add tradestation-data-provider
```

Directly from GitHub. The Python package is **not at the repo root** — the
repo root holds the wire contract, the EL indicator and the C++ bridge — so
the `subdirectory` fragment is required:

```bash
pip install "git+https://github.com/millerlai/tradestation-data-provider.git#subdirectory=bindings/python"
uv add "git+https://github.com/millerlai/tradestation-data-provider.git#subdirectory=bindings/python"
poetry add "git+https://github.com/millerlai/tradestation-data-provider.git#subdirectory=bindings/python"

# Pin to a tag, branch, or commit
pip install "git+https://github.com/millerlai/tradestation-data-provider.git@v0.2.0#subdirectory=bindings/python"
```

> Without the fragment pip fails with *"neither 'setup.py' nor 'pyproject.toml'
> found"*. A bare `...git@v0.1.0` does install — but only because that tag
> predates the move, so it silently gives you a pre-wire-v2 package.

### For development on this repo

```powershell
uv sync                       # base deps
uv sync --extra dev           # + pytest / ruff / mypy
uv run pytest                 # full suite, a few seconds
```

## Quick start

After install, import what you need and start the runtime — or run the bundled console script.

```python
import asyncio
from tradestation_data.aggregation import BarAggregator, MarketSnapshot
from tradestation_data.wire.el_subscriber import TradeStationELProvider
from tradestation_data.runtime.ingestion import IngestionRuntime
from tradestation_data.sinks import SinkPipeline
from tradestation_data.sinks.parquet import ParquetBarSink, ParquetTickSink

async def main() -> None:
    runtime = IngestionRuntime(
        provider=TradeStationELProvider(endpoint="tcp://127.0.0.1:5555"),
        symbols=["SPY", "QQQ"],
        snapshot=MarketSnapshot(),
        aggregator=BarAggregator(),
        sinks=SinkPipeline([
            ParquetBarSink(name="bars",  root="data/bars"),
            ParquetTickSink(name="ticks", root="data/ticks"),
        ]),
    )
    await runtime.run()

asyncio.run(main())
```

Or use the console entry point with a YAML config:

```bash
tradestation-data-ingest --sinks-config config/sinks.yaml
```

## Examples

Four runnable scripts in [`examples/`](examples/), each building on the last.
Between them they cover the whole package: receive events, handle them your
way, read them back.

| | Example | Needs a publisher? | What it shows |
| --- | --- | --- | --- |
| 01 | [`01_print_events.py`](examples/01_print_events.py) | yes | The whole receive loop in ~20 lines |
| 02 | [`02_custom_sink.py`](examples/02_custom_sink.py) | yes | Writing your own sink; the full runtime |
| 03 | [`03_read_history.py`](examples/03_read_history.py) | **no** | Storage tiers; one tick store, many timeframes |
| 04 | [`04_replay_fixtures.py`](examples/04_replay_fixtures.py) | **no** | Replaying recorded frames through the real binding |

Run them from `bindings/python/`:

```powershell
uv sync --extra dev

# Offline — no TradeStation, no DLL, nothing to set up.
uv run python examples/03_read_history.py
uv run python examples/04_replay_fixtures.py --fixture bars
```

**Examples 01 and 02 need something publishing on the other end.** That does
not have to be TradeStation — the C++ harness drives the DLL directly:

```powershell
# Terminal A — from the repo root. --warmup-ms buys time to attach: a PUB
# socket silently drops whatever it sends while no subscriber is listening.
cpp\build\x86-release\Release\TS2Python_TestHarness.exe --mode smoke --warmup-ms 8000

# Terminal B — from bindings\python
uv run python examples\01_print_events.py --count 6
```

**To test your own sink, copy example 04.** It replays the DLL output
recorded in [`contract/fixtures/`](../../contract/fixtures/) over a real
in-process ZeroMQ socket, so your sink sees byte-exact market data through
the same decode path production uses — with no TradeStation, no market
hours, and no waiting for a bar to close.

[`examples/README.md`](examples/README.md) has the full index, the harness
modes, and what to check when frames arrive but nothing prints.

## Pluggable sinks

Every tick and every closed bar is broadcast to every sink registered in `config/sinks.yaml`. One sink raising an exception is logged and isolated — it never blocks the others. Adding a new output destination means writing one class.

### Built-in sinks

| Sink | Purpose |
| --- | --- |
| `tradestation_data.sinks.parquet:ParquetBarSink` | Hive-partitioned 1-minute bar Parquet (enabled by default) |
| `tradestation_data.sinks.parquet:ParquetTickSink` | Hive-partitioned tick Parquet (enabled by default) |
| `tradestation_data.sinks.memory:InMemorySink` | Buffer events in memory (tests / notebooks); not for long runs |
| `tradestation_data.sinks.callback:CallbackSink` | Dispatch to Python callbacks registered per-symbol or catch-all |

### `config/sinks.yaml` example

```yaml
sinks:
  - name: bars_parquet
    class: tradestation_data.sinks.parquet:ParquetBarSink
    params:
      root: data/bars
      compression: zstd

  - name: dispatch
    class: tradestation_data.sinks.callback:CallbackSink

  - name: my_csv
    class: my_pkg.sinks:HourlyCsvSink     # your own sink
    params:
      root: out/csv
```

### Writing a custom sink

Subclass `BaseSink` and implement only the hooks you care about. The constructor must accept `name=` as a keyword argument so the runtime can wire the YAML name onto the instance.

```python
# my_pkg/sinks.py
from tradestation_data.domain.bar import Bar
from tradestation_data.sinks.base import BaseSink

class HourlyCsvSink(BaseSink):
    def __init__(self, *, name: str, root: str) -> None:
        self.name = name
        self.root = root
        # open file / set up buffers ...

    def on_bar(self, bar: Bar) -> None:
        # write one CSV row
        ...

    def close(self) -> None:
        # final flush, close handles
        ...
```

Reference it from `sinks.yaml` as `class: my_pkg.sinks:HourlyCsvSink` — as long as `my_pkg` is importable, the runtime will pick it up.

The full protocol:

```python
class Sink(Protocol):
    name: str
    def on_tick(self, tick: Tick) -> None: ...
    def on_bar(self, bar: Bar) -> None: ...
    def should_flush(self) -> bool: ...   # default False — only for buffered sinks
    def flush(self) -> None: ...          # default no-op
    def close(self) -> None: ...
```

### Receiving events with `CallbackSink`

Declare a `CallbackSink` in `sinks.yaml`, then register callbacks anywhere in your application:

```python
from tradestation_data.sinks.callback import get_sink

sink = get_sink("dispatch")     # name from sinks.yaml

def on_spy_bar(bar):
    print(bar.symbol, bar.close)

sink.on("SPY", "bar", on_spy_bar)
sink.on_any("tick", lambda t: ...)   # every symbol
```

Callbacks run synchronously inside the ingest loop — keep them fast (a few microseconds). Spawn `asyncio.create_task` or a thread inside the callback if you need to do real work. A callback that raises is logged and isolated; other callbacks for the same event still fire.

## Bundled offline tools

The runtime collects data; the scripts in [`scripts/`](scripts/) operate on the resulting Parquet store. Run them with plain `python scripts/<name>.py` — they re-enter the project venv via `uv run`.

```powershell
python scripts/run_ingestion.py                                  # live ingestion
python scripts/aggregate_parquet.py --symbol all --timeframe 5m `
  --input data/bars/timeframe=1m --output data/bars              # 1m -> Nm
python scripts/verify_parquet.py --start-date 2026-03-20 --end-date 2026-04-17
python scripts/imputation_parquet.py --start-date 2026-03-20 --end-date 2026-04-17 --dry-run
python scripts/audit_bar_cache.py                                # weekly audit
python scripts/clear_bar_cache.py                                # clear Tier-3 cache
python scripts/dedupe_bars.py                                    # drop duplicate bars
python scripts/dump_parquet.py                                   # inspect a parquet file
python ../../contract/tools/record.py                            # raw ZMQ wire inspector
```

## Project layout

```
bindings/python/                   # this binding; repo root is two levels up
├── pyproject.toml
├── .python-version                # 3.12, matching the CI matrix
├── LICENSE                        # copy — packaging cannot reach above this dir
├── config/
│   ├── sinks.yaml                 # pluggable sink pipeline (module:attr paths)
│   └── symbols.yaml               # symbol universe + per-symbol session policy
├── scripts/                       # human-facing CLI wrappers (see above)
├── src/tradestation_data/
│   ├── domain/                    # Bar / Tick — the value range of the wire
│   ├── wire/                      # frame decoding, gap detection  [core]
│   ├── aggregation/               # BarAggregator / MarketSnapshot   [app]
│   ├── storage/                   # BarWriter / TickWriter / HistoryStore / Resampler
│   ├── sinks/                     # Sink protocol, pipeline, registry, built-ins
│   ├── runtime/                   # IngestionRuntime + CLI entry
│   └── tools/                     # audit / clear cache helpers used by scripts
└── tests/
    └── conformance/               # replays contract/fixtures/ against this binding
```

`domain/` and `wire/` are the part any language binding must reimplement;
everything else is this reference application. See
[`docs/architecture.md`](../../docs/architecture.md) §5.

## Release flow (maintainers)

1. Bump `version` in `pyproject.toml`, commit.
2. `git tag vX.Y.Z && git push --tags`.
3. `.github/workflows/release.yml` builds sdist + wheel, smoke-tests the install, and publishes to PyPI via Trusted Publishing.

> First publish requires registering the workflow on the PyPI project page as a Trusted Publisher (workflow filename `release.yml`, environment `pypi`).

## Notes

- The C++ DLL is built and deployed by the upstream parent project — this repo is the Python side only.
- Default data root is `<project-root>/data/`; overridable via `--data-root` (only effective as a fallback when `sinks.yaml` is missing — the YAML's per-sink `root` parameter wins otherwise).
- Use `--no-storage` for ephemeral smoke tests (empty sink pipeline).
- `pytest` is configured with `filterwarnings = ["error", "ignore::DeprecationWarning"]` — a *new* warning fails the build. Fix the cause, don't broaden the filter.

## License

MIT — see [LICENSE](LICENSE).
