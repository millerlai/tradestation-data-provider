{ ===========================================================================
  TS2Python_Exporter — TradeStation EasyLanguage Indicator
  ---------------------------------------------------------------------------
  Job:   Forward every bar/tick on the chart's symbol to TS2Python.dll so the
         Python agent can consume it via ZeroMQ.

  Host:  Insert as an Indicator on a chart the wire can describe.
         - BarType = 0                      → EL_PublishTick(Close, ...)
         - BarType = 1 and BarInterval = 1  → EL_PublishTickEx(O, H, L, C, ...)
         - anything else                    → idle, logged once

         On a 1-min chart we send the full OHLC so the subscriber can rebuild
         the bar losslessly instead of collapsing it to a single close price.

  WHY THE INTERVAL CHECK: BarType = 1 covers every intraday minute chart —
  1-min, 5-min, 15-min, 60-min are all BarType 1, distinguished only by
  BarInterval. The wire currently states "bar_1m" unconditionally, so
  publishing from a 5-min chart would write 5-minute bars into the 1-minute
  partition, and downstream resampling would then derive "5m" from data that
  was already 5m. Nothing would report an error. Refusing to publish is the
  honest failure: the chart goes quiet and the operator notices.

  Multi-timeframe support is planned via a `tf` field on the wire; until the
  wire can say which interval a bar is, this indicator only publishes the one
  interval it can name.

  KNOWN GAP — VERIFY BEFORE RELYING ON THIS: a second-based chart may also
  report BarType = 1 with BarInterval = 1, in which case 1-second bars would
  pass the check below and be published as 1-minute bars. TradeStation exposes
  BarType_ext to separate second-based from minute-based intraday, but the
  exact values differ across TS versions and have not been checked against a
  live install here. Use minute charts only until that is confirmed.

  Zero state. All symbol/value plumbing comes from TS built-ins at runtime,
  so one compiled indicator covers every chart.

  Wire format and semantics: ../contract/. Error codes: ../contract/error_codes.md.
  =========================================================================== }

Inputs:
    ZMQEndpoint("tcp://127.0.0.1:5555"),
    Enabled(True),
    LogErrors(True);

Variables:
    InitRC(0),
    PubRC(0),
    InitDone(False),
    UnsupportedLogged(False),
    WrongIntervalLogged(False),
    Sym(""),
    TsStr("");

{ -- DLL prototypes ---------------------------------------------------------
  Calling convention is __stdcall (TS default for DefineDLLFunc).
  Keep types in sync with cpp/include/ts2python.h. }
DefineDLLFunc: "TS2Python.dll", int, "EL_Init", LPSTR;
DefineDLLFunc: "TS2Python.dll", int, "EL_PublishTick",
    LPSTR, LPSTR, double, double, double, double, double;
DefineDLLFunc: "TS2Python.dll", int, "EL_PublishTickEx",
    LPSTR, LPSTR, double, double, double, double, double, double, double, double;
DefineDLLFunc: "TS2Python.dll", int, "EL_Shutdown";
DefineDLLFunc: "TS2Python.dll", int, "EL_DllVersion";

{ -- One-shot init: first bar only. EL_Init is idempotent; return code 1
     means "another chart already bound the socket" — still success. }
If Enabled and InitDone = False Then Begin
    InitRC = EL_Init(ZMQEndpoint);
    If InitRC < 0 Then Begin
        If LogErrors Then
            Print("[TS2Python] EL_Init FAILED rc=", InitRC,
                  " endpoint=", ZMQEndpoint,
                  " symbol=", GetSymbolName);
        { leave InitDone = False so we retry on the next tick }
    End Else Begin
        InitDone = True;
        If LogErrors Then
            Print("[TS2Python] EL_Init ok rc=", InitRC,
                  " dll_version=", EL_DllVersion,
                  " symbol=", GetSymbolName,
                  " bar_type=", BarType,
                  " bar_interval=", BarInterval);
    End;
End;

{ -- Per-bar publish. Dispatch on BarType *and* BarInterval: BarType alone
     cannot tell a 1-minute chart from a 5-minute one. }
If Enabled and InitDone Then Begin
    Sym = GetSymbolName;
    { Bar-time string "yyyy-MM/dd-HH:mm:ss" 24-hour (e.g. "2026-04/18-13:30:45").
      DLL parses this as the EL-side event time (ts_utc on the wire). The
      authoritative wall-clock ts is still stamped by the DLL.

      24-hour format is deliberate: the AM/PM designator ("tt") is locale-
      dependent on Windows — a zh-TW TradeStation host emits "上午"/"下午",
      which breaks both the DLL sscanf path and the Python strptime path,
      silently collapsing every bar onto today's receive-time minute. }
    TsStr = FormatDate("yyyy-MM/dd", ELDateToDateTime(Date))
          + "-"
          + FormatTime("HH:mm:ss", ElTimeToDateTime(Time));

    { InsideBid / InsideAsk are live-quote functions. They return 0 when
      there is no quote to report — during historical replay (chart load,
      any non-realtime bar), and for symbols that carry no quote at all
      such as breadth indices ($TICK, $ADD, ...).

      They are passed through raw on purpose. The DLL normalises a
      non-positive quote to JSON null, so the "absent" case is expressed
      once, in one place, for every caller of the C ABI rather than being
      re-derived by each EL script. See contract/semantics.md §3. }

    If BarType = 0 Then Begin
        { Tick data series — one call per trade print. }
        PubRC = EL_PublishTick(
            Sym,
            TsStr,
            Close,
            Volume,
            Insidebid,
            Insideask,
            Ticks);
    End Else If BarType = 1 and BarInterval = 1 Then Begin
        { 1-minute bar — send the full OHLC. }
        PubRC = EL_PublishTickEx(
            Sym,
            TsStr,
            Open,
            High,
            Low,
            Close,
            Volume,
            Insidebid,
            Insideask,
            Ticks);
    End Else If BarType = 1 Then Begin
        { Intraday, but not 1-minute. The wire has no way to say which
          interval this is, so publishing would silently file these bars
          under timeframe=1m. Stay quiet and say why. }
        If WrongIntervalLogged = False Then Begin
            If LogErrors Then
                Print("[TS2Python] chart interval is ", BarInterval,
                      " minutes; only 1-minute intraday bars can be published",
                      " (the wire cannot yet state the interval).",
                      " symbol=", Sym,
                      " — indicator is idle on this chart");
            WrongIntervalLogged = True;
        End;
        PubRC = 0;
    End Else Begin
        { Daily/Weekly/Monthly/P&F — out of scope for the Python agent.
          Log once so the operator notices if they attach the indicator
          to the wrong chart by mistake. }
        If UnsupportedLogged = False Then Begin
            If LogErrors Then
                Print("[TS2Python] unsupported BarType=", BarType,
                      " on symbol=", Sym,
                      " — indicator is idle on this chart");
            UnsupportedLogged = True;
        End;
        PubRC = 0;
    End;

    If PubRC < 0 and LogErrors Then
        Print("[TS2Python] publish rc=", PubRC,
              " symbol=", Sym,
              " bar_type=", BarType,
              " bar_interval=", BarInterval,
              " ts=", TsStr,
              " close=", Close);
End;

{ No-op plot so the indicator shows up cleanly on charts. }
Plot1(0, "ts2python");
