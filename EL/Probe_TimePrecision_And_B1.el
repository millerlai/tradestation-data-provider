{ ===========================================================================
  Probe_TimePrecision_And_B1 — throwaway diagnostic indicator.

  NOT part of the shipped TS2Python_Exporter. Does not touch TS2Python.dll,
  ZeroMQ, or anything installed — it only Prints. Safe to apply to any chart
  alongside (or instead of) the real exporter.

  Answers three open questions at once:

  Q1 — does `Time` actually carry seconds on a sub-minute chart?
  ALREADY ANSWERED, 2026-08-03: NO. A live BarType=14/BarInterval=30 run
  showed two distinct, consecutive closed bars printing the IDENTICAL
  formatted close (both "...19:56:00") — `Time` is genuinely minute-only,
  on this chart type, regardless of the "ss" in the format string. Kept
  here for reference / re-confirmation, not because it's still open.

  Q1b — ANSWERED YES, 2026-08-03 (historical replay run): a live
  BarType=14/BarInterval=30 log showed call=1 (bar#1) print
  bdt_now=19:41:00 and call=2 (bar#2, a DIFFERENT closed bar) print
  bdt_now=19:41:30 — exactly where the classic Time-based "now" column
  collided (both showed 19:41:00). `BarDateTime` genuinely carries seconds
  on this chart type; `Time` does not. The fix is: build TsStr from
  BarDateTime instead of Date/Time in TS2Python_Exporter.el. That needs NO
  Python buffer change and NO wire/ABI bump — TsStr's format already has a
  seconds field, it would just start carrying a real value.

  Q1c — NEW, 2026-08-03: does BarDateTime expose a `.Format(...)` method
  directly (DateTime Class docs list Format() among its methods), so TsStr
  can be built as ONE call instead of manually zero-padding
  Hour/Minute/Second? (":2:0" print formatting space-pads single digits —
  "19:41: 0" — which is fine for a log but wrong for the wire string.)
  Still unconfirmed: what BarDateTime reads mid-formation, during an
  "Update Every Tick" refire call for a bar that has not actually closed
  yet — same live-vs-replay gap as Q2 below, now for two reserved words
  instead of one.

  Do NOT confuse this with `elsystem.DateTime.CurrentTime` / `.Now` — those
  read the COMPUTER'S wall clock, not the bar's own time, and would
  reintroduce exactly the "historical replay collapses onto one instant"
  bug `ts` already has and `ts_str` was built to avoid (see
  contract/semantics.md / CLAUDE.md on `ts` as last-resort only).
  `BarDateTime` is bar-scoped, not clock-scoped — that is the one to test.

  Q2 — docs/plan-bar-start-on-wire.md B1: does Time[1]/Date[1] reliably hold
  steady across "Update Every Tick" refire calls for a bar still forming, and
  only change once CurrentBar actually advances to a new bar?

  HOW TO RUN
  ----------
  Every run so far has been historical replay (chart load / scroll back) —
  every printed line shows newbar=Y, meaning no refire has been observed yet
  regardless of the Update Every Tick setting. Replay already answered Q1
  and Q1b conclusively (those don't depend on refire), but Q1c's live
  behavior and Q2 both need a LIVE, currently-forming bar:

  1. Paste this into a NEW EasyLanguage Indicator (any name) and Verify (F3).
  2. Apply it to the BarType=14/BarInterval=30 SPY chart. Confirm "Update
     Every Tick" is ON in Format Symbol / Properties.
  3. Watch it DURING market hours, live, while a bar is actively forming —
     not by loading/scrolling history. A couple of minutes at the open is
     enough; SPY prints often enough to generate several refire calls per
     30-second bar.
  4. Open the EasyLanguage Print Log and copy back a stretch that includes
     at least one bar# with multiple newbar=N lines in a row.

  READING THE OUTPUT
  -------------------
  One line per call. Columns:
    call      this script's own call counter (1, 2, 3, ...)
    bar#      CurrentBar — EL's own bar index, the ground truth
    newbar    Y on the first call after CurrentBar advanced, N otherwise
              (N = an "Update Every Tick" refire of the SAME forming bar)
    BarType / BarInterval / Category   so multiple charts' pasted logs can
              still be told apart
    now       this bar's own Date/Time, formatted exactly like the wire's
              TsStr (including seconds)
    prev      Date[1]/Time[1], formatted the same way
    bdt_now   BarDateTime.Hour:Minute:Second for the current bar
    bdt_prev  BarDateTime[1].Hour:Minute:Second for the previous bar
    fmt_now   BarDateTime.Format("%Y-%m/%d-%H:%M:%S") for the current bar
    fmt_prev  BarDateTime[1].Format(...) for the previous bar

  Q1b check (already confirmed YES from a replay run — see above). Kept in
  the probe so a live run reconfirms it under real "Update Every Tick"
  conditions, not just replay.

  Q1c check (the one still open): does fmt_now come out zero-padded and
  correct on its own (e.g. "2026-08-03-09:30:05"), matching bdt_now's
  Hour/Minute/Second read manually? If Format() works, TS2Python_Exporter.el
  can build TsStr in one call. If fmt_now is blank/errors, fall back to
  manual NumToStr + zero-pad from Hour/Minute/Second.

  Q2 check (the other one still open): on a LIVE bar that is still forming
  (several newbar=N refire calls in a row for the same bar#), do "prev",
  bdt_prev, AND fmt_prev all stay IDENTICAL across every refire call for
  that bar#? And does bdt_now / fmt_now for that SAME still-forming bar
  behave sensibly (e.g. does it read as "not yet closed" or does it already
  show the bar's eventual close time, same as classic Time does)? This
  needs a real intraday capture — replay only ever shows newbar=Y, never a
  refire, regardless of "Update Every Tick".

  Baseline replay checks (already passing in every run so far): every line
  with newbar=N must show the same "prev"/bdt_prev as the line before it.
  Every line with newbar=Y must show "prev"/bdt_prev equal to "now"/bdt_now
  from the last line printed for the PREVIOUS bar#. The very first printed
  line (bar#=1) is a boundary case — ignore it.
  =========================================================================== }

Inputs:
    LogErrors(True);

Variables:
    CallNum(0),
    PrevBarNum(0),
    NewBarStr(""),
    Cat(0),
    NowStr(""),
    PrevStr(""),
    FmtNowStr(""),
    FmtPrevStr("");

CallNum = CallNum + 1;

If CurrentBar <> PrevBarNum Then
    NewBarStr = "Y"
Else
    NewBarStr = "N";
PrevBarNum = CurrentBar;

{ Category must be assigned to a numeric variable before it can be read —
  same TradeStation requirement TS2Python_Exporter.el already documents. }
Cat = Category;

NowStr  = FormatDate("yyyy-MM/dd", ELDateToDateTime(Date))
        + "-" + FormatTime("HH:mm:ss", ElTimeToDateTime(Time));
PrevStr = FormatDate("yyyy-MM/dd", ELDateToDateTime(Date[1]))
        + "-" + FormatTime("HH:mm:ss", ElTimeToDateTime(Time[1]));

{ Q1c — does BarDateTime's own .Format() give a clean, zero-padded string
  in one call? If this errors or prints blank, TS2Python_Exporter.el would
  need to build the string manually from Hour/Minute/Second instead. }
FmtNowStr  = BarDateTime.Format("%Y-%m/%d-%H:%M:%S");
FmtPrevStr = BarDateTime[1].Format("%Y-%m/%d-%H:%M:%S");

Print(
    "[Probe] call=", CallNum:5:0,
    " bar#=", CurrentBar:6:0,
    " newbar=", NewBarStr,
    " BarType=", BarType:0:0,
    " BarInterval=", BarInterval:0:0,
    " Category=", Cat:0:0,
    " now=", NowStr,
    " prev=", PrevStr,
    { BarDateTime is bar-scoped (references THIS bar's own close), not
      clock-scoped — do not swap this for elsystem.DateTime.CurrentTime/Now,
      which read the computer's wall clock instead. See header comment. }
    " bdt_now=", BarDateTime.Hour:2:0, ":", BarDateTime.Minute:2:0,
                 ":", BarDateTime.Second:2:0,
    " bdt_prev=", BarDateTime[1].Hour:2:0, ":", BarDateTime[1].Minute:2:0,
                  ":", BarDateTime[1].Second:2:0,
    " fmt_now=", FmtNowStr,
    " fmt_prev=", FmtPrevStr
);
