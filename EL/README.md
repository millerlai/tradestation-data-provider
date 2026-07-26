# EL — EasyLanguage Exporter

> 📖 [繁體中文版](README.zh-TW.md)

The TradeStation EasyLanguage indicator that hands tick / bar data to the C++
bridge DLL ([`../cpp/`](../cpp/)), which publishes it over ZeroMQ for subscribers
in any language.

This is the **upstream origin** of the whole feed:

```
TradeStation Chart → TS2Python_Exporter.el → TS2Python.dll → ZMQ PUB → subscriber
```

## Files

- `TS2Python_Exporter.el` — the exporter indicator's EasyLanguage source (paste it
  into the TradeStation EasyLanguage Editor)

## Deploying

Install the DLL first, then the indicator — Verify needs the DLL to be in place
already:

```powershell
cd cpp
.\install-to-tradestation.bat     # finds TradeStation, copies the matching build
```

You do not have to build it yourself; [`../cpp/prebuilt/`](../cpp/prebuilt/) holds
ready-made binaries. See [`../cpp/README.md`](../cpp/README.md) for the details.

1. Open the EasyLanguage Editor in TradeStation
2. Create a new Indicator named `TS2Python_Exporter`
3. Paste in the contents of `TS2Python_Exporter.el`
4. Confirm `TS2Python.dll` is on TradeStation's DLL search path (the installer
   above takes care of this)
5. Verify the indicator (F3)
6. Apply it to the chart you want (SPY, QQQ, `$TICK`, …) with the data series set
   to **tick or 1 minute**

## Supported chart intervals

| chart | behaviour |
| --- | --- |
| Tick series (`BarType = 0`) | sent print by print |
| 1 / 5 / 15 / 30 / 60 minute | full OHLC sent under the matching timeframe (`1m`, `5m`, …) |
| Daily (`BarType = 2`, `BarInterval = 1`) | full OHLC sent under the `1d` timeframe |
| Weekly / monthly / P&F / any other unsupported interval | **idle**; the DLL rejects it with `-5` and the reason is printed once |
| Second-based charts | **idle**; the indicator detects it itself, stops sending, and prints the reason once |

### Why second-based charts need their own guard

`BarType` and `BarInterval` **cannot tell** a 1-second chart from a 1-minute one —
both can report `1` / `1`. Sent as minutes, those bars would land in the
`bars/timeframe=1m/` partition and be undetectable downstream: `TsStr` is built
from `Time`, and `Time` has minute resolution, so **the seconds are gone before
they ever leave the indicator**.

The guard depends on no version-specific constant. On a minute chart, `Date` /
`Time` advance on every bar; on a second-based chart they repeat within the same
minute. When the indicator sees two consecutive bars with identical `Date` and
`Time` (and `BarType = 1`, which excludes a tick series, where several prints per
minute are normal), it latches and stops sending.

> TradeStation also exposes `BarType_ext`, which does distinguish second-based
> from minute-based intraday — but its values differ between versions and have not
> been confirmed against a real installation, so it is **not** used as the test.
> To pin those values down: `Print(BarType_ext)` once on a known 1-minute chart
> and once on a 1-second chart.

## Design constraints

- The indicator does **no strategy computation whatsoever**. Its only job is to
  call the DLL and export the data.
- The payload format is governed by [`../contract/`](../contract/), not by this
  indicator. Change the contract before changing a field.
- `InsideBid` / `InsideAsk` are passed through as-is; EL makes no judgement about
  them. With no quote available (historical replay, non-live mode, breadth
  symbols) they return 0, and the DLL normalises that to JSON `null` — in one
  place in the C ABI, so that every EL caller agrees. See
  [`../contract/semantics.md`](../contract/semantics.md) §3.1.

## Pre-market data

What span of data the exporter sends is decided entirely by the **chart's session
settings**; no EL code has to change:

1. On the chart carrying `TS2Python_Exporter`: right-click → **Format Symbol →
   Settings**
2. Change **Session** from "Regular Session" to a template that includes
   pre-market (a custom 08:00–16:00 ET template, say, or a built-in extended one)
3. The exporter sends the pre-market bars as usual

> Note that changing the session affects the **range of data** only, not
> session-boundary semantics. `session_open_utc` is fixed at 09:30 ET regardless
> of the chart's session settings — see
> [`../contract/semantics.md`](../contract/semantics.md). A consumer with
> calculation windows that assume RTH-only should check its own behaviour once
> extended sessions are enabled.
