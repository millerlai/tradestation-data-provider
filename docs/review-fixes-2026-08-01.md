# Post-merge code-review fixes — 2026-08-01

Findings from the `xhigh` workflow review of the proto-1 refactor (`ac9103b`,
diff `923ba05..27937fe`). Run `wf_4fbfc25e-361`, 46/46 agents, 54 candidates →
15 distinct defects after synthesis.

**Branch:** `fix/post-merge-review-findings`
**Started from:** `4717f92`

## How to resume

This file is the source of truth for progress. If a session is interrupted:

1. Read the status table below. The first row that is not ✅ is where to resume.
2. `git log --oneline 4717f92..HEAD` shows what has actually landed.
3. Each finished row names its commit **by subject, not by SHA** — a SHA cannot
   be written into the commit that carries it, and an amend invalidates it
   anyway. Match the subject against the log. Trust the history over the table.
4. After each fix: run the verification gate below, update the row, then commit
   the code and the row together. One commit per fix, so an interruption never
   leaves a half-fix.

## Status

| # | Severity | File | Defect | Status | Commit subject |
|---|---|---|---|---|---|
| F1 | data loss | `storage/bar_writer.py:226` | One legacy-schema partition aborts every flush for the whole run | ✅ | `fix(storage): confine an unwritable partition to its own series` |
| F2 | data loss | `wire/el_subscriber.py:305` | `TypeError` from a bad frame kills the ingest task; process still reports healthy | ✅ | `fix(wire): drop an unparseable frame instead of ending the stream` |
| F3 | blocks reads | `storage/history_store.py:147` | Still-open footerless partition makes every read of that symbol raise | ✅ | `fix(storage): select partitions by path before opening them` |
| F4 | data loss | `wire/el_subscriber.py:410` | 1h RTH stub bar collides with, and overwrites, the preceding hour | ✅ | `fix(wire): label a bar from the minute before its close, not one whole tf` |
| F5 | false signal | `wire/el_subscriber.py:332` | `messages_lost` reports 0 when 100% of frames were refused | ⬜ | |
| F6 | false signal | `storage/history_store.py:147` | No `BAR_SCHEMA` guard despite docs claiming one; empty vs populated disagree on width | ⬜ | |
| F7 | wrong data | `scripts/imputation_parquet.py:184` | Imputed rows get NULL `timeframe`/`symbol`/`date`; `_passthrough_table` inherits it | ⬜ | |
| F8 | false signal | `wire/el_subscriber.py:330` | `seq` is schema-required but read with `.get()` and silently skipped | ⬜ | |
| F9 | rare, plausible | `storage/history_store.py:39` | `replace(tzinfo=)` pins `fold=0` across the DST-ambiguous hour | ⬜ | |
| F10 | test gap | `cpp/src/test_harness.cpp:52` | Fixture + test quantities are mutually derivable, so a column swap passes everything | ⬜ | |
| F11 | doc wrong | `bindings/python/README.md:286` | Claims no script rewrites the store; `dedupe_bars.py` rewrites in place by default | ⬜ | |
| F12 | doc wrong | `examples/03_read_history.py:191` | States daily `el_ticks` is a trade count, which the contract marks unconfirmed | ⬜ | |

Legend: ⬜ not started · 🔄 in progress · ✅ done · ⏸️ deferred (reason in notes)

## Already fixed before this list (commit `4717f92`)

- Unparseable `ts_str` silently fell back to the receive clock → now refused and logged
- `examples/01_print_events.py` read the renamed `event.volume`
- `install-to-tradestation.bat` named a `.eld` the repo does not ship

## Notes per finding

### F1 — legacy partition aborts the flush
`_rewrite` reads back a pre-existing `timeframe=1d/.../bars.parquet` whose columns
are `volume, tick_count, source` and concatenates it with the new `el_*` frame.
`flush()` has no per-partition try/except, so one bad partition takes down every
partition after it, the buffer never clears, the retry repeats forever, and the
heartbeat still reports healthy. Two things to decide: refuse a mixed root loudly
at open, and isolate per-partition failures so one cannot starve the rest.

### F2 — narrow except tuple
`_quantities` does `int(data[name])` and `_parse_tick` does `float(data["px"])`;
a JSON null raises `TypeError`, and a payload decoding to a non-object makes
`data.get("seq")` raise `AttributeError`. Neither is caught. The drop path's own
comment says "Dropping malformed message", so continuing was the intent.

### F3 — footerless partition
`_read` globs `date=*` and filters on `bucket_start`, not on the hive `date`
column, so no pruning happens and polars opens the current day's open file.
Filtering on the hive key first would prune it; catching and skipping an
unreadable partition is the other half.

### F4 — session-truncated bar
A 60-minute RTH chart is 6 full bars plus a 15:30-16:00 stub, which EL stamps
Time=1600. Subtracting a full 60m and snapping to the 09:30-anchored grid maps
both 15:30 and 16:00 to 14:30 ET. The runtime then treats the stub as an
intra-bar refresh and replaces the real hour. Needs a rule that does not assume
every bar spans a full interval.

### F5 — messages_lost conflation
The observe-before-gate ordering is deliberate and
`test_refused_frame_still_counts_against_the_sequence` pins it — skipping
observe would fabricate gaps. **Do not reorder.** The defect is that no counter
distinguishes "nothing lost" from "everything refused". Add a refusal counter and
surface it next to `messages_lost`.

### F7 — hive columns in the imputation output
Verified empirically: `pq.read_table()` on a single BarWriter file returns 14
columns — the 11 `BAR_SCHEMA` fields plus `timeframe`/`symbol`/`date` inferred
from the path. `_output_schema` therefore builds 15 columns while
`_build_imputed_row` supplies 12. Both `impute_day` and the `_passthrough_table`
helper added in `4717f92` should project to `BAR_SCHEMA + imputed` and let the
path carry the partitioning, as `BarWriter` does.

### F9 — marked PLAUSIBLE, not CONFIRMED
The verifier did not show that TradeStation emits a bar inside the repeated
01:00-02:00 ET hour for any timeframe this binding accepts; pre-market starts at
04:00 ET. Decide whether it is reachable before spending effort.

### F10 — needs the C++ toolchain
Fixing the harness means rebuilding the DLL and re-recording all four fixtures,
then re-deriving the `expected/*.json` by hand (the repo rule: expectations must
never be generated from the code under test). Heavier than the rest. The Python
half — test helpers using mutually derivable quantities — can be fixed
independently and is worth doing first.

## Verification gate

Before marking any row ✅, from `bindings/python/`:

```
uv run --with pytest-timeout pytest -q --timeout=30
uv run ruff check . ; uv run ruff format --check .
uv run mypy
```

## Not covered by this list

- End-to-end verification against a live TradeStation is still outstanding from
  the parent refactor — `docs/refactor-proto-1.md` §驗證, steps 3-6.
- Six review agents ran without the safety classifier available; their findings
  were not independently safety-reviewed. The four touching code changed on
  2026-08-01 were re-verified by hand.
