{ ===========================================================================
  TS2Python_Exporter — TradeStation EasyLanguage Indicator
  ---------------------------------------------------------------------------
  Job:   Forward every bar/tick on the chart's symbol to TS2Python.dll so the
         Python agent can consume it via ZeroMQ.

  Host:  Insert as an Indicator on any supported chart.
         - BarType = 0   → EL_PublishTick(Close, ...)
         - otherwise     → EL_PublishBar(O, H, L, C, ...) carrying BarType and
                           BarInterval, which the DLL maps to a wire timeframe

         Supported intervals: 1 / 5 / 15 / 30 / 60 minute, and daily.
         Anything else is refused by the DLL with rc = -5 and logged once, and
         sub-minute charts are refused by this script (see below).

         Bars go out as full OHLC so the subscriber can rebuild them losslessly
         instead of collapsing to a single close price.

  QUANTITIES GO OUT VERBATIM: Volume, Ticks, UpTicks, DownTicks and OpenInt
  each get their own wire field, named after the reserved word. This script
  makes no choice between them and performs no conversion. Volume and Ticks
  swap meaning between intraday and daily charts (contract/semantics.md 3.4);
  resolving that here is what used to force a publisher-convention version
  number onto every payload, because the choice was invisible on the wire.

  VERSIONED WITH THE DLL: this file binds EL_Init3 and checks EL_DllVersion
  once, after init. Publishing latches off on a mismatch. The publish
  signatures changed without the names changing, and __stdcall makes a
  mismatched call corrupt the stack rather than fail — so the DLL and the
  .ELD must be installed as a pair. contract/wire.md tabulates what each
  mismatched combination looks like.

  WHY BarInterval IS PASSED: BarType = 1 covers every intraday minute chart —
  1-min, 5-min, 15-min and 60-min are all BarType 1, told apart only by
  BarInterval. This indicator does not decide what that means; it forwards both
  numbers and lets the DLL map them, so every caller of the C ABI agrees on the
  vocabulary. Guessing here would file 5-minute bars under the 1-minute
  partition, which nothing downstream can detect.

  SUB-MINUTE CHARTS ARE DETECTED AND REFUSED: a second-based chart also reports
  BarType = 1, and can report BarInterval = 1 exactly like a 1-minute chart, so
  the two numbers cannot tell them apart. Published as 1m, those bars would fill
  the 1-minute partition with sub-minute data wearing minute-shaped timestamps —
  and nothing downstream could notice, because TsStr is built from Time, which
  has minute resolution, so the seconds are gone before the bar leaves this
  script.

  What survives is the repetition: on a minute chart Date and Time advance every
  bar; on a sub-minute chart consecutive bars share both. This script latches on
  that and stops publishing. The test needs no version-specific constant, unlike
  BarType_ext — whose values differ across TS releases and have never been
  checked against a live install here. To pin those down, Print(BarType_ext) on
  a known 1-minute chart and on a known 1-second one.

  Zero state. All symbol/value plumbing comes from TS built-ins at runtime,
  so one compiled indicator covers every chart.

  Wire format and semantics: ../contract/. Error codes: ../contract/error_codes.md.
  =========================================================================== }

Inputs:
    ZMQEndpoint("tcp://127.0.0.1:5555"),
    Enabled(True),
    LogErrors(True),
    { Print one line per publish call showing all five quantity words. Since
      they now go on the wire verbatim, this is also exactly what the
      subscriber receives — which makes it the switch to turn on when working
      out what a chart type really hands over, or when checking a stored
      partition against its source. Volume and Ticks appear side by side, and
      that is the pair whose meaning flips between intraday and daily
      (contract/semantics.md 3.4).

      Off by default and worth leaving off: on a tick chart, or on any chart in
      "update every tick" mode, this prints once per print. }
    LogPublish(False);

Variables:
    InitRC(0),
    PubRC(0),
    DllVer(0),
    InitDone(False),
    VersionMismatch(False),
    UnsupportedLogged(False),
    SubMinuteChart(False),
    AggregatedTickChart(False),
    Sym(""),
    TsStr("");

{ -- DLL prototypes ---------------------------------------------------------
  Calling convention is __stdcall (TS default for DefineDLLFunc).
  Keep types in sync with cpp/include/ts2python.h.

  EL_PublishTick and EL_PublishBar kept their names across the protocol
  rewrite but NOT their signatures. Under __stdcall the callee pops the
  arguments, so calling either one with the wrong arity corrupts the stack —
  TradeStation misbehaves or dies, it does not return an error. What makes
  that unreachable is the init guard: every publish call below sits behind
  InitDone, and the DLL this file is built for renamed its init to
  EL_Init3. An older DLL has no such export, so DefineDLLFunc fails to
  resolve at verify time and no publish ever runs. Conversely this DLL keeps
  EL_Init and EL_Init2 as tombstones returning -6, so an older .ELD stops at
  init too. See contract/wire.md.

  The quantity parameters are double because EasyLanguage has no 64-bit
  integer type; the DLL casts them to int64 before they reach the wire. }
DefineDLLFunc: "TS2Python.dll", int, "EL_Init3", LPSTR;
DefineDLLFunc: "TS2Python.dll", int, "EL_PublishTick",
    LPSTR, LPSTR, double,
    double, double, double, double, double,
    double, double;
DefineDLLFunc: "TS2Python.dll", int, "EL_PublishBar",
    LPSTR, LPSTR, int, int,
    double, double, double, double,
    double, double, double, double, double;
DefineDLLFunc: "TS2Python.dll", int, "EL_Shutdown";
DefineDLLFunc: "TS2Python.dll", int, "EL_DllVersion";

{ -- One-shot init: first bar only. EL_Init3 is idempotent; return code 1
     means "another chart already bound the socket" — still success.

     rc = -6 means this DLL is newer than this file: the tombstoned EL_Init /
     EL_Init2 return it. That cannot happen here, since this file binds
     EL_Init3 — it is listed in contract/error_codes.md for the operator who
     is running an older .ELD against this DLL.

     The ABI check runs here, once, rather than per bar. EL_DllVersion takes
     no arguments, so its signature can never drift and calling it is safe
     against any build of the DLL — it is the one export that can be asked
     "who are you" unconditionally. A mismatch latches publishing off instead
     of letting the version-specific signatures below be called. }
If Enabled and InitDone = False Then Begin
    InitRC = EL_Init3(ZMQEndpoint);
    If InitRC < 0 Then Begin
        If LogErrors Then
            Print("[TS2Python] EL_Init3 FAILED rc=", InitRC,
                  " endpoint=", ZMQEndpoint,
                  " symbol=", GetSymbolName);
        { leave InitDone = False so we retry on the next tick }
    End Else Begin
        InitDone = True;
        DllVer = EL_DllVersion;
        If DllVer <> 1 Then Begin
            VersionMismatch = True;
            If LogErrors Then
                Print("[TS2Python] DLL ABI mismatch: EL_DllVersion=", DllVer,
                      " but this indicator is built for 1.",
                      " Publishing stopped on symbol=", GetSymbolName, ".",
                      " Reinstall TS2Python.dll and re-import the .ELD that",
                      " shipped with it — they are versioned together.");
        End Else If LogErrors Then
            Print("[TS2Python] EL_Init3 ok rc=", InitRC,
                  " dll_version=", DllVer,
                  " symbol=", GetSymbolName,
                  " bar_type=", BarType,
                  " bar_interval=", BarInterval);
    End;
End;

{ -- Sub-minute chart guard. Runs before the publish block, so the bar that
     reveals the chart is itself never sent.

     BarType and BarInterval cannot separate a 1-second chart from a 1-minute
     one — both can read 1 and 1. The bar times can: Date and Time advance on
     every bar of a minute chart, and repeat within a minute on a sub-minute
     one. Tick charts (BarType = 0) legitimately produce many prints per minute
     and are excluded.

     Latching is deliberate. Once a chart has shown itself to be sub-minute it
     does not become minute-based later, and a chart that goes quiet must not
     silently resume publishing. }
If Enabled and InitDone and SubMinuteChart = False
   and BarType = 1 and CurrentBar > 1
   and Date = Date[1] and Time = Time[1] Then Begin
    SubMinuteChart = True;
    If LogErrors Then
        Print("[TS2Python] sub-minute chart detected on symbol=", GetSymbolName,
              " — two consecutive bars share date=", Date, " time=", Time, ".",
              " Publishing stopped: these bars carry minute-resolution",
              " timestamps and would be filed as 1-minute data with nothing",
              " downstream able to tell. Use minute or daily charts.");
End;

{ -- Aggregated tick chart guard. Same shape as the sub-minute one, and for
     the same reason: the data would be filed under a name that misdescribes it
     and nothing downstream could tell.

     A tick series is only one print per call when BarInterval = 1. On an
     N-tick chart each call carries a whole bar — Close is the last print of
     the N, and the volume words cover all N under the intraday rule below
     (Ticks their total share volume, Volume the up-tick part). Neither word
     reports N; EL exposes no count intraday at all. EL_PublishTick has no
     field to say the call is a bar either, so Tier 1 would record it as a
     single trade whose price is one print and whose volume is all hundred.
     Nothing raises; the volume column is simply wrong by about two orders of
     magnitude.

     Measured on a live install: a 100-tick chart reports BarInterval = 100
     and Ticks = 760951 — the share volume of the hundred prints, not 100 —
     while a 1-tick chart reports BarInterval = 1 and calls once per print. }
If Enabled and InitDone and AggregatedTickChart = False
   and BarType = 0 and BarInterval <> 1 Then Begin
    AggregatedTickChart = True;
    If LogErrors Then
        Print("[TS2Python] aggregated tick chart detected on symbol=", GetSymbolName,
              " — bar_interval=", BarInterval, ".",
              " Publishing stopped: each call carries ", BarInterval,
              " prints aggregated into one bar, and the tick wire has no way",
              " to say so — it would be stored as a single trade with the",
              " volume of ", BarInterval, ". Use a 1-tick chart, or a minute",
              " chart if you want bars.");
End;

{ -- Observation on a refused chart. LogPublish's other two Prints live inside
     the publish block below, which both guards latch off on the very first
     bar — so on exactly the chart types nobody has measured yet, the switch
     documented for measuring them printed nothing at all.

     Only the EasyLanguage words are available here; rc does not exist unless
     the publish ran. That is enough, because the question a refused chart
     raises is what the five quantity words return on it — §3.4's table was
     established from exactly this measurement.

     Gated on the refusal so a supported chart's output is unchanged; it would
     otherwise print twice per bar. LogPublish defaults False, so opting in is
     what accepts one line per bar. }
If Enabled and InitDone and LogPublish
   and (SubMinuteChart or AggregatedTickChart) Then
    Print("[TS2Python] refused ", GetSymbolName,
          " bar_type=", BarType, " bar_interval=", BarInterval,
          " date=", Date, " time=", Time,
          " volume=", Volume, " ticks=", Ticks,
          " upticks=", UpTicks, " downticks=", DownTicks,
          " openint=", OpenInt);

{ -- Per-bar publish. Dispatch on BarType *and* BarInterval: BarType alone
     cannot tell a 1-minute chart from a 5-minute one. }
If Enabled and InitDone and VersionMismatch = False
   and SubMinuteChart = False
   and AggregatedTickChart = False Then Begin
    Sym = GetSymbolName;
    { Bar-time string "yyyy-MM/dd-HH:mm:ss" 24-hour (e.g. "2026-04/18-13:30:45").
      Goes on the wire verbatim as ts_str. The authoritative wall-clock ts is
      stamped by the DLL; this string is what the subscriber derives the bar's
      bucket from, and the DLL no longer parses it at all.

      24-hour format is deliberate: the AM/PM designator ("tt") is locale-
      dependent on Windows — a zh-TW TradeStation host emits "上午"/"下午",
      which breaks both the DLL sscanf path and the Python strptime path,
      silently collapsing every bar onto today's receive-time minute.

      Time IS THE BAR'S CLOSE, so TsStr is right-labelled: the first RTH
      1-minute bar goes out as 09:31, not 09:30. That is left as-is on
      purpose — the wire carries EL's raw fact and each binding converts to
      the contract's left label itself (contract/semantics.md §2). Shifting
      it here would change the meaning of the wire without any binding
      knowing, which is the one thing this transport must never do. }
    TsStr = FormatDate("yyyy-MM/dd", ELDateToDateTime(Date))
          + "-"
          + FormatTime("HH:mm:ss", ElTimeToDateTime(Time));

    { Volume, Ticks, UpTicks, DownTicks and OpenInt all go out VERBATIM, one
      wire field each, named after the reserved word that produced them.
      This script makes no choice between them.

      That is a deliberate reversal. Volume and Ticks mean OPPOSITE things on
      intraday and on daily charts — TradeStation's own definition, for stock
      symbols:

                     intraday                     daily and up
        Volume       shares traded on UP TICKS    total shares
        Ticks        TOTAL shares                 number of ticks

      This file used to resolve that here, picking whichever word the chart
      type made "total share volume" and putting it in a single `vol` field.
      The choice happened off the wire, so both outcomes looked equally
      plausible downstream, and a separate publisher-convention version had to
      ride along on every payload just to say which rule had been applied —
      a number that could itself fall out of sync, since this file lives in
      the user's TradeStation install and nothing updates it when the DLL or a
      binding does.

      Shipping all five words removes the choice, and with it the need to
      declare anything. The intraday/daily inversion is still a fact; it is
      now a table the consumer reads (contract/semantics.md §3.4) rather than
      a decision this file makes on their behalf.

      UpTicks / DownTicks carry the up/down share split, which is real
      order-flow information that the old single-field wire had nowhere to
      put. OpenInt is 0 on stocks and ETFs and only means anything on futures
      and options. }

    { InsideBid / InsideAsk are live-quote functions. They return 0 when
      there is no quote to report — during historical replay (chart load,
      any non-realtime bar), and for symbols that carry no quote at all
      such as breadth indices ($TICK, $ADD, ...).

      They are passed through raw on purpose. The DLL normalises a
      non-positive quote to JSON null, so the "absent" case is expressed
      once, in one place, for every caller of the C ABI rather than being
      re-derived by each EL script. See contract/semantics.md §3.

      Ticks only. A bar carries no quote: InsideBid/InsideAsk describe the
      moment of the call, which on a bar is its last print, not the bar. }

    If BarType = 0 Then Begin
        { Tick data series, BarInterval = 1 — one call per trade print,
          confirmed on a live install. Anything coarser was refused above.
          TsStr has minute resolution, so the prints inside one minute are
          indistinguishable here; the DLL's receive-side ts is what separates
          them, which is why contract/semantics.md §1 makes it the tick's
          authoritative time rather than ts_str. }
        PubRC = EL_PublishTick(
            Sym,
            TsStr,
            Close,
            Volume,
            Ticks,
            UpTicks,
            DownTicks,
            OpenInt,
            Insidebid,
            Insideask);

        If LogPublish Then
            Print("[TS2Python] tick ", TsStr,
                  " bar_type=", BarType, " bar_interval=", BarInterval,
                  " px=", Close,
                  " volume=", Volume, " ticks=", Ticks,
                  " upticks=", UpTicks, " downticks=", DownTicks,
                  " openint=", OpenInt,
                  " bid=", InsideBid, " ask=", InsideAsk,
                  " rc=", PubRC);
    End Else Begin
        { Any bar series. BarType and BarInterval go out as-is; the DLL owns
          the mapping to a wire timeframe and returns -5 for intervals it
          cannot name. }
        PubRC = EL_PublishBar(
            Sym,
            TsStr,
            BarType,
            BarInterval,
            Open,
            High,
            Low,
            Close,
            Volume,
            Ticks,
            UpTicks,
            DownTicks,
            OpenInt);

        If LogPublish Then
            Print("[TS2Python] bar  ", TsStr,
                  " bar_type=", BarType, " bar_interval=", BarInterval,
                  " o=", Open, " h=", High, " l=", Low, " c=", Close,
                  " volume=", Volume, " ticks=", Ticks,
                  " upticks=", UpTicks, " downticks=", DownTicks,
                  " openint=", OpenInt,
                  " rc=", PubRC);

        If PubRC = -5 and UnsupportedLogged = False Then Begin
            If LogErrors Then
                Print("[TS2Python] no wire timeframe for bar_type=", BarType,
                      " bar_interval=", BarInterval,
                      " on symbol=", Sym,
                      " — supported: 1/5/15/30/60 minute and daily.",
                      " Indicator is idle on this chart");
            UnsupportedLogged = True;
        End;
    End;

    { -5 is excluded: the block above already printed it once, with the
      actionable message. Repeating this generic line would put a second entry
      in the Print Log for every bar — for every tick in "update every tick"
      mode — on a chart the DLL has already refused, drowning the errors an
      operator actually needs to see. That also restores what the header
      promises: unsupported intervals are "logged once". }
    If PubRC < 0 and PubRC <> -5 and LogErrors Then
        Print("[TS2Python] publish rc=", PubRC,
              " symbol=", Sym,
              " bar_type=", BarType,
              " bar_interval=", BarInterval,
              " ts=", TsStr,
              " close=", Close);
End;

{ No-op plot so the indicator shows up cleanly on charts. }
Plot1(0, "ts2python");
