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
| 1-tick series (`BarType = 0`, `BarInterval = 1`) | sent print by print |
| N-tick series (`BarType = 0`, `BarInterval > 1`) | **idle**; each call is N prints aggregated and the tick wire cannot say so |
| 1 / 5 / 15 / 30 / 60 minute | full OHLC sent under the matching timeframe (`1m`, `5m`, …) |
| Daily (`BarType = 2`, `BarInterval = 0` or `1`) | full OHLC sent under the `1d` timeframe. TradeStation 10 reports `0` here — `1` is accepted too, because that is what the ABI documented before a live install was measured |
| Weekly / monthly / P&F / any other unsupported interval | **idle**; the DLL rejects it with `-5` and the reason is printed once |
| Second-based charts | **idle**; the indicator detects it itself, stops sending, and prints the reason once |

### Why `vol` does not come from EasyLanguage's `Volume`

TradeStation defines these two reserved words with **opposite meanings** on
intraday and daily charts (stock symbols):

| | intraday | daily and up |
| --- | --- | --- |
| `Volume` | shares traded on **up ticks** | total shares |
| `Ticks` | **total shares** | number of ticks |

So the intuitive reading — `Volume` is the quantity, `Ticks` is the count — is
true only on daily. This indicator used to send `Volume` as the wire's `vol` on
every chart, which on intraday shipped the up-tick share volume alone: roughly
half of what traded, and undetectable downstream because it is a perfectly
plausible number that is simply too small.

The indicator now selects by `BarType`, so `vol` is total share volume on every
timeframe, as [`../contract/semantics.md`](../contract/semantics.md) §3.4
requires. `tc` has no honest intraday value — EL exposes no word for the number
of trades on an intraday bar — so it is sent as `0` there.

`UpTicks` / `DownTicks` do carry the up/down share split intraday, which is real
order-flow information, but the wire has nowhere to put it; adding a field is a
version bump and has not been done.

### Why an N-tick chart is refused

A tick series is one print per call only when `BarInterval = 1`. On a 100-tick
chart each call carries a finished bar: `Close` is the last of the hundred
prints, `Volume` is their sum, `Ticks` is 100. `EL_PublishTick` has no field to
express that, so Tier 1 would store it as **one trade priced at the last print
and carrying a hundred prints' volume** — wrong by two orders of magnitude in
the volume column, with nothing downstream able to notice.

Unlike the second-based case, the information needed to detect this survives:
`BarInterval` says exactly how many prints went into the call. Measured on a
live install — a 100-tick chart reports `bar_interval=100.00` at `EL_Init`, and
a 1-tick chart reports `1.00` and calls `EL_PublishTick` once per print.

Note this also means `TsStr` cannot separate the prints inside one minute: a
1-tick chart happily emits eight calls all stamped `19:48:00`, because `Time`
has minute resolution. What separates them is the DLL's receive-side `ts`,
which is why [`../contract/semantics.md`](../contract/semantics.md) §1 makes
that — not `ts_str` — the tick's authoritative time.

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
