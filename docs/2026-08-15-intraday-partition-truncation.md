# A chart reload truncates the oldest session of an intraday `date=` partition

Measured 2026-08-15 against provider `9ff8c47`. Written as an upstream issue
draft — the body below is meant to be pasted into
`tradestation-data-provider` as-is. Kept here because the measurement was
taken here and the numbers are the argument.

---

## Summary

`BarWriter` merges a republished day into what is already on disk for daily
bars and silently replaces it for intraday bars. The asymmetry is not a
decision about reloads — it falls out of the single-file daily layout — but
it costs real recorded data, because **the oldest session of a republish
burst is truncated mid-day**: a chart's loaded history begins at an
arbitrary bar, not at a session boundary.

## Reproduction

One `$TICK` 5-minute chart, hub + a recording consumer running, market
closed.

1. Store holds `date=2026-08-03` with 79 bars, first close 09:40 — the
   09:30 bucket is missing, because 08-03 was the oldest session the chart
   had loaded.
2. Widen the chart's loaded range so 2026-08-03 is no longer the oldest
   session, then Verify. The DLL republishes, and the log shows the missing
   bar going out:
   ```
   [TS2Python] 2026-08/03-09:35:00 bar_type=1.00 bar_interval=5.00 ... rc=0.00
   ```
3. `date=2026-08-03` is now 80 bars, first close 09:35. **Repaired.**
4. `date=2026-07-31` is now 79 bars, first close 09:40. **It was 80 before.**
   The truncation moved to the burst's new oldest session.

The same round over `$ADD`, `$VOLD`, `$TRIN`, `$PCVA`, `IWM`, `RSP` repaired
2026-08-03/06/07/10 on each and truncated 2026-07-31 on all six. For the two
symbols that carry pre-market the loss is not one bar:

| symbol | 2026-07-30 (untouched) | 2026-07-31 (after republish) |
| --- | --- | --- |
| `$ADD` / `$VOLD` / `$TRIN` / `$PCVA` | 09:35, 78 bars | 09:40, **77 bars** |
| `IWM` | 06:05, 168 bars | 09:40, **77 bars** |
| `RSP` | 06:50, 127 bars | 09:40, **77 bars** |

`IWM` lost that session's entire pre-market — 91 bars — to a republish that
was only meant to add one.

There is a second, separate symptom on the same root: within one process a
past day can only be imported **once**. `date=2026-08-03` was written at
22:42, sealed ~60 s later by `_seal_elapsed_days`, and every republish after
that was refused at `write()` with `bar_partition_sealed` — the DLL reported
`rc=0`, the operator saw a successful publish, and nothing reached disk. The
run has to be restarted before the day can be re-imported.

## Where it comes from

```python
# storage/bar_writer.py:97
@property
def rewrites(self) -> bool:
    return self.day is None
```

`day` is `None` exactly when `bar.bar_type == 2` (`:210`), so `rewrites`
means "is this the single-file daily layout", not "should this merge".
`_flush_partition` branches on it (`:273`): daily goes to `_rewrite`
(`:318`), every `date=` partition goes to a streaming `pq.ParquetWriter`.

`_rewrite` is already exactly the behaviour wanted here — read the existing
rows back, concat, `unique(subset=["bar_time"], keep="last")`, sort, write a
temp file, `os.replace` — and its own docstring names the case:

> A repeated `bar_time` keeps the later row — that is a chart reload
> re-sending days we already have, and the fresher copy is the one
> TradeStation just adjusted.

It exists for daily only because `docs/architecture.md:638-641` gave daily a
single file per symbol (one row per day against ~2.9 KB of unavoidable
schema/footer), and a single file that must stay readable cannot be appended
to: `pq.ParquetWriter` writes its footer at `close()` and cannot be
reopened. Merge is a by-product of that constraint, not a policy.

Intraday keeps one file per session, which in normal operation is written
once and sealed, so the streaming writer fits and never re-reads. A
republish of an already-written day was therefore treated as a *readability*
problem (sealing) rather than a *merge* problem, and `write()` refuses a
sealed partition on that ground (`:223-226`):

> Reopening would truncate a finished day. Losing one late bar beats losing
> the session it belongs to.

That trade-off is correct while merging is impossible. It stops being
necessary the moment it isn't.

## Suggested fix

Make `rewrites` true for a `date=` partition whose day is already past, and
keep the streaming writer for the current session. `rewrites` is a bare
property today and has no clock, while `BarWriter` already holds the
injected `self._today_et` that `_is_finished` uses (`:393`) — so the
predicate needs the day passed in rather than reading one:

```python
def rewrites(self, today: date) -> bool:
    return self.day is None or self.day < today
```

with `_flush_partition` (`:273`) calling `part.rewrites(self._today_et())`.

- A republish of a finished session becomes additive; the burst's oldest
  session keeps whatever the store already had.
- `bar_partition_sealed` stops dropping bars — a sealed past day can simply
  be rewritten, so a past day no longer needs to be import-once-per-process.
- Today's partition is untouched, so nothing about the live path changes.

**Not proposing "always rewrite".** `date=` partitions also carry `bar_type`
0 — tick charts, where one session's row count is unbounded
(`contract/semantics.md` §1). Rewriting a growing tick partition on every
60-second flush is quadratic across a session. Bounding the change to past
days avoids that entirely, since a past day is rewritten only when a burst
actually re-sends it.

Worth noting but explicitly **out of scope** here: two documented operator
hazards — "Ctrl+C or the day has no footer" and "a mid-session restart
truncates the morning" — are the same `ParquetWriter` property on *today's*
partition, and the narrow fix above does not address them.

## Consumer-side workaround in use

`tools/merge_snapshot.py` in the monarch repository: snapshot `data/bars`,
republish, then union the snapshot back in with the same conflict rule
`_rewrite` uses (live wins on a repeated `bar_time`; snapshot rows survive
only where live has no bar at that instant). It works, and it needs the
snapshot to be taken *before* the chart is touched — which is exactly the
ordering a merge inside the writer would make unnecessary.
