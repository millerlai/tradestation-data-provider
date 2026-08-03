# Plan — resolving spec-vs-code drift in `contract/`

> **Status**: proposed, not started. Written 2026-08-03.
> **Scope**: changes to `contract/` (the SSoT) and, for D1, possibly to
> `bindings/python/` code. **Nothing in this plan is a documentation-only edit**
> — that is exactly why it was split out of the `docs/architecture.md` fixes
> landed alongside it.
> **Origin**: an xhigh code review of PR #35 (`docs/architecture.md` +
> `docs/architecture.zh-TW.md`) found 15 confirmed defects. Twelve were errors in
> the new documents and were fixed directly. The four below are not: the document
> faithfully reproduced `contract/`, and `contract/` is what disagrees with the
> code.

## Why these are not doc fixes

`contract/` is the language-neutral SSoT. `contract/README.md` states the change
order explicitly: **spec first, then `cpp/`, then each binding.** A descriptive
document may not quietly "correct" the spec by describing the code instead — that
produces two disagreeing normative sources, which is the failure mode
`contract/fixtures/` exists to prevent.

Each item below therefore needs a **decision on which side is wrong** before any
edit. `contract/fixtures/README.md` is blunt about this: "當 conformance 失敗，
先決定哪一邊錯了再動手 —— 至今兩次都是規格錯的." Twice so far the spec was the
wrong side; that is not a reason to assume it is again.

---

## D1 — `breadth` session reset: contract says 09:30 ET, code rolls at 04:00

### The contradiction

| Source | Claim |
| --- | --- |
| `contract/semantics.md` §4.1 | `breadth` → "**每日重置**（09:30 ET 清空）", 盤前保留 = 無 |
| `aggregation/snapshot.py::MarketSnapshot.on_bar` | Clears `recent_bars` only when `session_date_of(bar.bar_time) != st.session_date` |
| `aggregation/session.py` | `session_date_of` rolls at `PRE_SESSION_CUTOFF_LOCAL = time(4, 0)`, not 09:30 |
| `aggregation/session.py::SessionPolicy.for_category` | `breadth` → `pre_market_window_minutes=None`, documented in-file as "None = unlimited pre-market" |
| `snapshot.py` eviction branch | `_evict_before_premarket_window` sits under `if not policy.session_reset:` — so it never runs for `breadth` at all |

**Observable divergence**: at 09:31 ET, `MarketSnapshot.view_of("$TICK").recent_bars`
contains every bar since 04:00 ET. A second binding built to §4.1 returns only
post-09:30 bars. Same wire input, different session state.

### Options

**Option A — fix the contract to match the code (04:00 rollover, no intra-session eviction).**
Cheapest, no behavior change, nothing to re-measure. But it means asserting that
"reset at the session-date boundary" was always the intent and 09:30 was
shorthand — which is plausible (`session_date_of`'s whole purpose is to define
what session a bar belongs to) but is a claim about intent nobody has verified.

**Option B — fix the code to match the contract (clear at 09:30 ET, drop pre-market).**
Honours the stated market rationale: a breadth index like `$TICK` genuinely
resets at the open, so carrying pre-market prints into the RTH window is
arguably meaningless data. But it is a **behavior change to a live ingest path**,
it needs a new test, and it changes what existing consumers of `MarketSnapshot`
see mid-session.

**Recommendation: A**, with the §4.1 wording changed to name the 04:00 boundary
and state that `breadth` retains its pre-market bars within a session. Rationale:
`MarketSnapshot` is an in-memory convenience view, not the persisted record —
nothing on disk is affected either way — and Option B changes runtime behavior to
satisfy a sentence, which inverts the usual burden. Revisit if a consumer
actually depends on the 09:30 semantics.

### Steps (Option A)

1. Rewrite `contract/semantics.md` §4.1's `breadth` row: reset at the **04:00 ET
   session-date rollover**; pre-market bars within a session are retained.
2. Add the distinction to §4's table: `session_open_utc` (09:30, used for
   `session_open_bar`) and the **session-assignment cutoff** (04:00, used for
   retention) are two different boundaries. Both already exist in `session.py`;
   the contract currently names only the first.
3. Add a conformance fixture: a `breadth` symbol with bars at 03:59 / 04:01 /
   09:29 / 09:31 ET, expectation derived by hand from the revised §4.1.
4. Remove the ⚠️ block from `docs/architecture.md` §6.4 and its zh-TW counterpart.

---

## D2 — index/breadth quote list: contract mandates it, code deleted it

### The contradiction

`contract/semantics.md` §3.2 still **requires** a binding to treat
`$TICK $ADD $VOLD $TRIN $PCVA VXX` quotes as invalid, and §3.3 folds that into
its three-way test. It also names `TradeStationELProvider(index_symbols=...)` as
the override parameter — **a parameter that no longer exists**. The code removed
the list deliberately (`el_subscriber.py`: "The binding no longer blanks anyone's
quote"), on the grounds that the list was a guess in both directions and that
`VXX` — a tradeable ETN measured at 567,776 shares in one bar — was having a real
quote thrown away.

There is a second, related staleness in the same section: §3's preamble still
says "兩者都只適用於 tick —— bar 不帶報價", which proto 2 contradicts outright
(every point carries `bid`/`ask`, §5.2).

### Options

**Option A — delete §3.2, renumber §3.3's test to two conditions, fix the §3 preamble.**
Matches the code and the stated reasoning. `category` (§3.5) is already on the
wire as the fact a consumer keys off instead. Risk: a consumer that *wanted* the
old blanking now has to implement it, and the contract no longer tells them the
symbol set — mitigated by §3.5 already documenting `category` 4 = Index, plus the
measured note that `VXX` is category 2.

**Option B — keep the rule but move it from "binding must" to "consumer may".**
Preserves the knowledge (the symbol list is real institutional knowledge) without
mandating behavior no binding implements.

**Recommendation: A for the mandate, B for the knowledge** — delete the
requirement, and keep the symbol list as a non-normative note explaining what it
was, why it was removed, and the `VXX` measurement that killed it. That preserves
the audit trail without making a second binding implement a rule the reference
binding doesn't.

### Steps

1. `contract/semantics.md` §3 preamble: drop "兩者都只適用於 tick —— bar 不帶報價";
   state that every point carries a quote and that absence is spelled `null`.
2. Replace §3.2 with a non-normative note (history + `VXX` measurement + pointer
   to §3.5 `category`).
3. §3.3: reduce to two conditions (`null`, `<= 0`). Delete the
   `index_symbols=...` reference.
4. Check `contract/fixtures/README.md` — the `smoke.jsonl` row claims coverage of
   "index symbol 的 bid/ask 無效化（§3.2）", which will no longer be a rule. The
   fixture's `expected/` values must be re-derived by hand, **not** regenerated.
5. Remove the ⚠️ block from `docs/architecture.md` §6.3 and its zh-TW counterpart.

> ⚠️ **This one touches `expected/` fixtures.** Per `contract/fixtures/README.md`
> rule 2, expectations must be derived independently from `semantics.md` and never
> produced by the code under test. Do this by hand and review it as a separate
> commit.

---

## D3 — ABI compatibility matrix names the wrong missing export

### The contradiction

`contract/wire.md` (〈新舊部署不相容時會發生什麼〉, ~line 148) says the
"new `.ELD` + old DLL" case is caught because the old DLL "沒有 `EL_Init3` 匯出".
That is false, and verifiable from this repo's own history:

```
git show 7faeabf:cpp/src/TS2Python.def   # ABI-1: exports EL_Init3, NOT EL_Publish
git show HEAD:cpp/src/TS2Python.def      # ABI-2: adds EL_Publish
```

`EL/TS2Python_Exporter.el` already states the correct version in its
`DefineDLLFunc` comment block: "An ABI-1 DLL does not export EL_Publish at all,
so DefineDLLFunc fails to resolve at verify time and nothing runs. It DOES export
EL_Init3 with this same signature, so init alone would not catch it — the
EL_DllVersion latch below is what does."

**Impact**: an operator hitting this failure is sent to look at the wrong export.
Worse, a maintainer who believes "the init export name is the guard" could
reasonably re-add or rename a publish export thinking init alone protects them —
which is precisely the `__stdcall` stack-corruption hazard the tombstones exist
to prevent.

### Options

There is no real fork here — the claim is simply wrong and the correct version is
already written down in the EL indicator. **Fix `contract/wire.md`.**

### Steps

1. `contract/wire.md`: change the "新 `.ELD` + 舊 DLL" row's interception point to
   the missing **`EL_Publish`** export, and adjust the surrounding prose that
   generalises the guard to the init name.
2. Keep the init-tombstone reasoning intact — it is correct for the *reverse*
   direction (old `.ELD` + new DLL), which is what `-6` covers.
3. `docs/architecture.md` §4.3 and its zh-TW counterpart are **already fixed** in
   the same change that produced this plan; verify they still agree afterwards.
4. Consider a test or a `.def`-diff check, since this class of claim went stale
   silently once already.

---

## D4 — three files still advertise `proto` / ABI = 1

### The contradiction

| File | Says |
| --- | --- |
| `README.md` — Versioning table | Wire `proto` = 1, DLL ABI = 1 |
| `README.zh-TW.md` — same table, lines 153-154 | Wire `proto` = 1, DLL ABI = 1 |
| `contract/README.md` — 〈只有一個版本〉 | "`proto`，目前恆為 `1`" |

The code refuses anything but `2` (`el_subscriber.py::PROTO_VERSION`,
`ts2python.cpp::kDllVersion`), and `contract/wire.md` / `contract/semantics.md`
are already at 2.

This is the *lowest-risk* item — a pure find-and-replace with no behavioral
question — but it is listed here rather than fixed inline because
`contract/README.md` is part of the SSoT and the two READMEs are the first thing
a new binding author reads. Fixing it deserves its own reviewable commit rather
than being buried in a docs refactor.

### Steps

1. `README.md`: Versioning table → 2 / 2.
2. `README.zh-TW.md`: same table → 2 / 2. **Do not skip this one** — the original
   drift note named only the English README, which is how it survived.
3. `contract/README.md`: 〈只有一個版本〉 → `proto` 恆為 `2`.
4. Grep the repo for any remaining `proto.*1` / `ABI 1` claims before closing.

---

## Suggested execution order

1. **D4** — mechanical, no decision needed, unblocks reading everything else.
2. **D3** — factually settled, single-file edit, no fixture impact.
3. **D2** — needs the A/B decision confirmed, and touches `expected/` fixtures;
   do it as two commits (spec, then fixtures).
4. **D1** — needs the A/B decision confirmed; if Option B is chosen it becomes a
   code change with a new test and should be planned separately.

Each of D1-D4 should land as its own commit or PR. They are independent; nothing
here needs to be batched.
