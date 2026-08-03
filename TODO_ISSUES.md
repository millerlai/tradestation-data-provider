# Open architecture questions — 2026-08-02

Two findings from the `xhigh` code-review workflow (`wf_1971d6a5-e50`) that are
**not** safe to patch mechanically — each rests on a wire/architecture
assumption that needs a decision before any diff is written. Tracked
separately from `task.md`, which covers the nine findings that are.

**Branch:** `fix/code-review-2026-08-02`
**Source:** `xhigh` review of `ba87c53` (PR #31, `verify/live-tradestation-proto-1` merge)

---

## I1 — sub-minute chart silently loses ~59/60 bars

**Files:** `EL/TS2Python_Exporter.el:231`, `bindings/python/src/tradestation_data/runtime/ingestion.py:278`

**Status:** ⬜ blocked on a direction choice

### The problem

`BarType=1, BarInterval=1` is wire-indistinguishable between "a genuine
1-minute chart" and "a 1-second chart whose bars happen to close inside the
same minute" — `ts_str` only has minute resolution either way.

Before this diff, EL detected the collision (two consecutive `CurrentBar`s
sharing one `Date`/`Time`) and **latched a flag that stopped publishing** for
that chart. This diff removed the guard so publishing continues
unconditionally (matching the repo's "wire carries everything verbatim, refuse
nothing" design), but `ingestion.py:278`'s `_handle_provider_bar` still only
bypasses the intra-bar buffer for `bar_type == 0` (true tick charts). A
sub-minute `bar_type == 1` chart instead goes through the buffer keyed on
`(symbol, bar_type, bar_interval, bar_time)`: every sub-second print inside one
minute shares a `bar_time` and is treated as "refine the forming bar," so only
the last print of each minute ever reaches a sink. Roughly 59 of 60 real bars
are silently dropped, with no error, warning, or counter — the file on disk is
indistinguishable from genuine complete 1-minute data.

### Why it can't be patched inline

Any fix has to decide how Python tells "EL is still refining the current bar"
apart from "EL just closed one bar and opened a new one that happens to share
a minute," and the wire currently carries no field that answers this.

### Candidate directions

| # | Approach | Cost / risk |
|---|---|---|
| a | Behavioral inference: once a key shows the same `bar_time` twice in a way inconsistent with monotonic refinement, bypass the buffer for it | No reliable signal exists to tell this apart from a legitimate 1-minute "Update Every Tick" refine sequence — risks reintroducing the tick-chart bug this repo already fixed once, on ordinary 1-minute charts |
| b | Add a wire field (monotonic bar sequence number, or an explicit sub-minute flag) | Correct for every case, but is a `proto`/DLL-ABI bump — this repo's rule is "exactly one of each, and nothing else is supported"; touches EL, the C++ bridge, and `contract/` |
| c | Document as a known limitation: buffer correctness requires `bar_time` to name the bar uniquely; charts where it doesn't (anything other than `bar_type == 0`) are not guaranteed correct sub-minute — users wanting sub-second resolution use a tick chart | Doesn't fix the data loss, just stops it from being silent/undocumented |

**Recommendation:** (c) now — cheap, honest, and unblocks nothing else from
being wrongly relied on — with (b) as the real fix, worth folding into any
future proto/ABI bump (see I2, which already has one queued).

**Update (found while fixing `task.md` T5):** (c) is already half-done. The
`.el` file's own header comment (`TS2Python_Exporter.el:39-65`) already spells
out this exact caveat — it just wasn't surfaced anywhere a binding author
would read it. `EL/README.md`/`EL/README.zh-TW.md`'s "Supported chart
intervals" table now carries the same warning (T5's fix). Still missing:
`contract/semantics.md` has no mention of it at all (`grep` for
`sub-minute|coalesce` there returns nothing) — per this repo's own rule
("anything a second binding would have to guess belongs in
`contract/semantics.md`"), that's the one place this is genuinely absent, and
is a much smaller lift than originally scoped here. The Python-side data loss
itself (`ingestion.py:278`) is unchanged and still needs (a)/(b)/(c) decided.

---

## I2 — `verify_parquet.py` / `imputation_parquet.py` expected-bar grid is wrong for session-restarting intervals

**Files:** `bindings/python/scripts/verify_parquet.py:87`, `bindings/python/scripts/imputation_parquet.py:343`

**Status:** ⬜ blocked on `docs/plan-bar-start-on-wire.md` Phase 2 (B1–B3), not a standalone decision

### The problem

`_expected_bars()` generates one uniform grid from `--start-time`. TradeStation
actually restarts its intraday bar grid at each session sub-boundary (RTH
open/close), so an interval that doesn't evenly divide every session segment
(a 1h chart on a 06:00–20:00 session, per the measured case in
`docs/plan-bar-start-on-wire.md` A1–A3) legitimately produces stub bars at
non-grid times. `_expected_bars` doesn't know this, so `verify_parquet.py`
false-flags correct data as MISSING/extra, and `imputation_parquet.py` —
which imports and reuses the same function — fabricates OHLC rows at the
wrong timestamps to "fill" gaps that were never real.

### Why this isn't a fresh decision

This is the exact defect `docs/plan-bar-start-on-wire.md` Phase 2 already
exists to fix at the source: putting the bar's true start (`ts_str_prev`) on
the wire so downstream tools stop guessing session-segment boundaries. That
plan is blocked on B1–B3 in that same document (does `Time[1]`/`Date[1]`
return the previous bar's close reliably; is every session's first bar
guaranteed to be a full interval; which EL session reserved words are
available) — all things only you can answer from the EL editor / live account.

### Interim option, before Phase 2 lands

Narrow `_expected_bars`'s guarantee instead of pretending it's exact: for a
`--bar-interval` that doesn't evenly divide every configured session segment
(the same divisibility table as plan doc §A3), demote the MISSING/extra check
from a hard flag to a skip-with-warning rather than asserting a grid known to
be wrong. This is tracked as `task.md` item #2 only if you want the interim
mitigation done now — otherwise leave both scripts as-is until Phase 2's
B1–B3 are answered and the real fix (session-aware grid from `ts_str_prev`)
can be written once, correctly.

**Recommendation:** hold this one for Phase 2. A single-purpose interim patch
here would be thrown away once the wire carries the true segment boundary —
better to spend that effort once, on the real fix, than twice.

---

## Next step

Reply with a direction for I1 (a/b/c) and whether you want the I2 interim
mitigation now or to wait for Phase 2's B1–B3. Both get their own commit(s)
once decided; neither blocks `task.md`.
