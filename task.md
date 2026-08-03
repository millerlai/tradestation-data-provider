# Code-review fixes — 2026-08-02

The nine mechanical findings from the `xhigh` workflow review
(`wf_1971d6a5-e50`) of `ba87c53` (PR #31 merge) that don't need a design
decision first. The two that do are tracked in `TODO_ISSUES.md` instead.

**Branch:** `fix/code-review-2026-08-02`
**Started from:** `ba87c53`

## How to resume

1. Read the status table below. The first row that is not ✅ is where to resume.
2. `git log --oneline ba87c53..HEAD` shows what has actually landed.
3. Each finished row names its commit **by subject, not by SHA**. Match the
   subject against the log; trust the history over the table.
4. After each fix: run the verification gate below, update the row, then
   commit the code and the row together. One commit per fix.

## Status

| # | File | Defect | Status | Commit subject |
|---|---|---|---|---|
| T3 | `bindings/python/tests/test_history_store.py:7` | Regression test for "load_bars never derives from ticks" was deleted, no replacement | ✅ | `test(storage): restore the load_bars-never-derives regression test` |
| T4 | `docs/architecture.md:498` | Table row contradicts the row above it on interval refusal | ✅ | `docs(el): stop documenting the deleted refuse/idle chart-interval model` |
| T5 | `EL/README.md:100`, `EL/README.zh-TW.md` | "Supported chart intervals" table still describes deleted refuse/idle/-5 model | ✅ | `docs(el): stop documenting the deleted refuse/idle chart-interval model` |
| T6 | `cpp/README.md:236`, `cpp/README.zh-TW.md` | Still documents removed `-5 unmappable bar interval` return code | ✅ | `docs(cpp): drop the retired -5 return code from cpp/README` |
| T7 | `bindings/python/src/tradestation_data/runtime/ingestion.py:42` | Class docstring still describes deleted Tick/Bar split | ✅ | `docs(runtime): fix IngestionRuntime docstring after the tick/bar split removal` |
| T8 | `bindings/python/tests/test_ingestion_runtime.py:118` | Comment still describes removed left-edge shift | ✅ | `test(runtime): fix stale left-edge comments in test_ingestion_runtime.py` |
| T9 | `bindings/python/src/tradestation_data/sinks/callback.py:100` | Docstring has garbled duplicated `on_bar` fragment | ✅ | `docs(sinks): fix garbled CallbackSink docstring left by the on_tick removal` |
| T10 | `bindings/python/scripts/imputation_parquet.py:83` | Docstring column counts stale after schema grew (4th partition level + 4 new columns) | ⬜ | |
| T11 | `bindings/python/scripts/imputation_parquet.py:298` | `--bar-interval`/`--bar-type` validation block duplicated instead of imported from `verify_parquet.py` | ⬜ | |

Legend: ⬜ not started · 🔄 in progress · ✅ done

## Verification gate

Before marking any row ✅, from `bindings/python/`:

```
uv run pytest -q
uv run ruff check . ; uv run ruff format --check .
uv run mypy
```

Doc-only rows (T4, T5, T6) have no test gate — a read-through against the
current (post-PR#31) code/behavior they describe is the check.

## Not covered by this list

`TODO_ISSUES.md` — I1 (sub-minute chart data loss) and I2 (session-restart
expected-bar grid) — both need a direction decision before any diff.
