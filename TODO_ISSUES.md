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

**Status:** ✅ **FIXED** 2026-08-03 — see `docs/plan-bardatetime-seconds.md` (P1–P6, branch `fix/bardatetime-seconds`). The publisher now builds `ts_str` from `BarDateTime`, which carries real seconds; the binding no longer floors them away; `contract/semantics.md` §2.1 is reversed and §1.3 records the measurement; `bars.jsonl` gained two `BarType=14` frames one minute apart in `bar_time` but 30 seconds apart in reality, so conformance now fails if any binding reintroduces the flooring.

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

**Update (2026-08-03, real `BarType` values measured live):** the user
supplied TradeStation's actual `BarType` enum (0=TickBar, 1=Minute, 2=Daily,
3=Weekly, 4=Monthly, 5=Point&Figure, 8=Kagi, 9=Kase, 10=Line Break,
11=Momentum, 12=Range, 13=Renko, 14=Second, 15=Renko Custom, 16=Renko Mean)
and a live log showing a 30-second chart reports `BarType=14, BarInterval=30`
— **not** `BarType=1` like a real minute chart. The "wire-indistinguishable
from a 1-minute chart" framing above is wrong for this case: `bar_type`
already, verbatim, tells the two apart. The collision that remains is
narrower than originally scoped — it is two DIFFERENT closed bars *within
the same* `bar_type=14` series sharing one minute-floored `bar_time`, not an
ambiguity between series.

Also ruled (a) out **on principle, not just cost**: this project reads
whatever TradeStation exports verbatim and does not build an inference layer
to guess meaning it wasn't given (an OHLC/volume-monotonicity heuristic was
proposed and rejected on exactly this basis — see the `feedback-
verbatim-no-assumptions` project memory). (a) is dead.

**Update (2026-08-03, live probe run — `EL/Probe_TimePrecision_And_B1.el`):**
ran the probe on a `BarType=14/BarInterval=30` chart (historical replay,
2026-07-31 19:55–20:00 ET) and a `BarType=1/BarInterval=1` chart.

- **Q1 answered: NO, `Time` carries no sub-minute precision, even on
  `BarType=14`.** Direct proof: two consecutive, distinct closed bars
  (`bar#=2`, `bar#=3`, both `newbar=Y`) produced the identical formatted
  close `2026-07/31-19:56:00`. This is not a Python-side artifact —
  `FormatTime("HH:mm:ss", ElTimeToDateTime(Time))` has nothing but `:00`
  seconds to show, because `Time` itself is minute-resolution on this chart
  type. This rules out "stop flooring in `_parse_el_str_as_et`" as a fix —
  there is no seconds precision to preserve; `el_subscriber.py:523`'s
  `.replace(second=0, microsecond=0)` is a no-op here, not the cause.
- **Q2 (B1) strongly supported, one gap remains.** Every transition in both
  logs has `prev` on line N+1 exactly equal to `now` on line N — including
  the `bar#=2`→`bar#=3` case where the two bars' own `now` values collide.
  `Time[1]`/`Date[1]` correctly tracks "the immediately preceding bar's own
  close," in lockstep with `CurrentBar`, even across a minute-resolution
  collision and across real data gaps (quiet periods with no bar at all).
  **Gap:** every logged call was `newbar=Y` — this run was a historical
  replay (chart load), which fires once per already-closed bar regardless of
  "Update Every Tick." Not yet observed directly: whether `Time[1]` holds
  perfectly steady across `newbar=N` refire calls for a bar still forming
  live. Live confirmation planned for the 2026-08-03 US market open
  (`BarType=14/BarInterval=30`, SPY, Update Every Tick genuinely on,
  real-time not history).

**Revised recommendation (superseded by the update below):** (b) was the
confirmed direction, converging with I2 via `Time[1]`/`Date[1]`
(`ts_str_prev`). Kept for the record — see below for why an even more
direct fix looks available.

**Update (2026-08-03, `ELSystem.DateTime.BarDateTime` researched and
measured):** the user pointed out that TradeStation's `elsystem` object
model exposes date/time with real precision, and asked specifically about
`ELSystem.DateTime`. Research + a live probe run found something better
than `ts_str_prev`:

- **`BarDateTime` (a reserved word returning a `DateTime`-class object,
  `BarDateTime[BarsAgo].FieldName`) is documented by TradeStation itself as
  giving "the current date and time properties of the bar, INCLUDING
  SECONDS"** — a different reserved word from the classic `Date`/`Time`
  this script and `TS2Python_Exporter.el` both use. `Date`/`Time` are
  genuinely minute-resolution (Q1, confirmed above); `BarDateTime` is not.
- **Confirmed live** (`EL/Probe_TimePrecision_And_B1.el`,
  `BarType=14/BarInterval=30`): `bar#=1` printed `bdt_now=19:41:00`,
  `bar#=2` (a different closed bar) printed `bdt_now=19:41:30` — exactly
  where the classic-`Time`-based column collided (`19:41:00` for both).
  `BarDateTime[1]` also tracked the true previous bar correctly across
  every transition in the log, same as `Time[1]` did.
- Important distinction found during research: **do not confuse this with
  `elsystem.DateTime.CurrentTime`/`.Now`** — those read the computer's wall
  clock, not the bar's own time, and would reintroduce the exact
  "historical replay collapses onto one instant" bug `ts` already has.
  `BarDateTime` is bar-scoped; `CurrentTime`/`Now` are clock-scoped.

**This changes the fix entirely.** If `BarDateTime` behaves correctly
mid-formation (still being confirmed — see below), the real fix is:
**rewrite `TsStr` in `TS2Python_Exporter.el` to build from `BarDateTime`
instead of `Date`/`Time`.** No Python buffer change. No wire/ABI bump —
`TsStr`'s wire format (`"yyyy-MM/dd-HH:mm:ss"`) already has a seconds field;
it would just start carrying a real value, and `bar_time` would become
genuinely unique per closed bar without any disambiguation logic at all.
This would make I1 independent of I2/Phase 2's B1–B3 entirely — no need to
wait on `ts_str_prev`.

**Two things confirmed still needed before writing that change** (probe
already extended to test both, live run pending — see "Next step"):
1. Does `BarDateTime.Format("%Y-%m/%d-%H:%M:%S")` work directly (it's a
   `DateTime`-class object, and `Format()` is documented on that class), or
   does `TsStr` need to be built by hand from `Hour`/`Minute`/`Second` with
   manual zero-padding?
2. What does `BarDateTime` read **mid-formation**, during an "Update Every
   Tick" refire call for a bar that has not actually closed yet? Every run
   so far has been historical replay (every line `newbar=Y`), which never
   exercises a refire regardless of the Update Every Tick setting. This is
   the same gap Q2 already had for `Time[1]`, now doubled for `BarDateTime`
   too — needs a genuinely live, currently-forming bar to observe.

(a) remains ruled out on principle (see above). (c) (the documented
limitation in `EL/README.md`) stays true and harmless regardless of which
fix lands — it just may end up describing a limitation that gets lifted.

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

**Cross-reference (2026-08-03):** the probe run investigating I1 (see I1's
updates above) produced first-hand evidence for this document's B1 question
too — `Time[1]`/`Date[1]` correctly tracked the previous bar's close across
every transition observed, including a minute-resolution collision and real
data gaps. One gap remains (live "Update Every Tick" refire behavior, not
yet observed — historical replay only fires once per bar). Live confirmation
planned for the 2026-08-03 market open. If it holds, B1 in
`docs/plan-bar-start-on-wire.md` may be answerable from this evidence, and
Phase 2's `ts_str_prev` would unblock I1 and I2 together with one field.

---

## Next step

**I1 is closed** (`docs/plan-bardatetime-seconds.md`). Two things it leaves
behind:

1. **Redeploy both halves.** The `.ELD` has NOT been compiled in
   TradeStation — offline checks only (Begin/End balance, unchanged
   `EL_Publish` arity). The DLL and the indicator must be reinstalled as a
   pair, and any Parquet already collected from a sub-minute chart is
   missing bars that cannot be recovered.
2. **I2 stays open**, still parked on `docs/plan-bar-start-on-wire.md`'s
   B1–B3. Unrelated to the `BarDateTime` fix — I2 is about the bar's
   *start*, which the wire still does not carry. The probe runs did produce
   first-hand B1 evidence (`Time[1]`/`BarDateTime[1]` track the previous bar
   correctly, and hold steady across refires), so B1 is arguably answerable
   now from `docs/plan-bardatetime-seconds.md` §A4 without another capture.
