# Examples

Four runnable scripts, in the order they build on each other. Read them
through and you have seen the whole package: receive events, handle them
your way, read them back.

**Two of the four need no TradeStation and no DLL** — start there if you
just want to see the shapes.

| | Example | Needs a publisher? | What it shows |
| --- | --- | --- | --- |
| 01 | [`01_print_events.py`](01_print_events.py) | yes | The whole receive loop in ~20 lines |
| 02 | [`02_custom_sink.py`](02_custom_sink.py) | yes | Writing your own sink; the full runtime |
| 03 | [`03_read_history.py`](03_read_history.py) | **no** | Storage tiers; one tick store, many timeframes |
| 04 | [`04_replay_fixtures.py`](04_replay_fixtures.py) | **no** | Replaying recorded frames through the real binding |

Run everything from `bindings/python/`:

```powershell
uv sync --extra dev

uv run python examples/03_read_history.py       # offline, self-contained
uv run python examples/04_replay_fixtures.py    # offline, replays contract/fixtures/
```

## Getting a publisher without TradeStation

Examples 01 and 02 need something on the other end of the socket. You do not
need TradeStation for that — the C++ harness drives the DLL directly:

```powershell
# Terminal A. --warmup-ms gives you time to start the subscriber; a PUB
# socket silently drops everything it sends while nobody is attached.
cpp\build\x86-release\Release\TS2Python_TestHarness.exe --mode smoke --warmup-ms 8000

# Terminal B
cd bindings\python
uv run python examples\01_print_events.py --count 6
```

Harness modes: `smoke` (ticks + a bar over three symbols), `noquote`
(absent quotes), `bars` (every non-1m timeframe plus the refusal path),
`session` (first/last bar of a session), `stress`, `multithread`. Building it
is in [`cpp/README.md`](../../../cpp/README.md).

## Testing your own sink

Example 04 is the one to copy. It publishes the frames recorded in
[`contract/fixtures/`](../../../contract/fixtures/) over a real in-process
ZeroMQ socket and lets an ordinary `TradeStationELProvider` consume them —
nothing stubbed, nothing monkey-patched, the same decode path that runs in
production. Point the pipeline at your sink instead of the printing one and
you get byte-exact market data with no TradeStation, no market hours, and no
waiting for a bar to close.

The fixtures are recorded from a real DLL, never hand-written. Which harness
mode produced each one is tabulated in
[`contract/fixtures/README.md`](../../../contract/fixtures/README.md).

## Nothing arrives, and nothing errors

Three causes, in order of likelihood:

1. **The publisher started first.** ZeroMQ PUB drops messages while no
   subscriber is attached — it does not queue them. Start the subscriber
   first, or give the harness a longer `--warmup-ms`.
2. **Wrong event loop on Windows.** pyzmq registers its wakeup FD through
   `loop.add_reader()`, which the default ProactorEventLoop does not
   implement: the socket connects, and `recv` never fires. There is no error
   to see. [`_compat.py`](_compat.py) is why the examples do not hit this;
   copy `run()` into your own entry point.
3. **Symbol mismatch.** Subscriptions are exact after filtering, so a
   subscription to `SPY` will not show you `SPYG` — and a typo shows you
   nothing at all rather than failing loudly.

## Notes

- `_compat.py` is shared plumbing, not an example. It exists because
  `asyncio.set_event_loop_policy()` — the spelling `runtime/main.py` uses —
  is deprecated in Python 3.14 and slated for removal in 3.16, while this
  package still supports 3.11.
- The examples import `tradestation_data` as an installed package, so they
  work the same way your own code will. `uv sync` puts it there.
- They are excluded from the published sdist and wheel; they are repository
  documentation, not shipped code.
