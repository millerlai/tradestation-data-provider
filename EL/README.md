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

> **The DLL and the `.ELD` are a matched pair — always install both.**
> `EL_PublishTick` and `EL_PublishBar` once kept their names across a protocol
> rewrite but not their signatures, and under `__stdcall` a mismatched call
> corrupts the stack rather than returning an error. Two guards make every
> mismatched combination fail readably instead: this indicator binds
> `EL_Init3`, which an older DLL does not export (Verify fails), and this DLL
> keeps `EL_Init` / `EL_Init2` as tombstones returning `-6`, which stops an
> older `.ELD` at init before it can publish. There is also an
> `EL_DllVersion()` check after init. See
> [`../contract/wire.md`](../contract/wire.md) for the full table.

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

## Inputs

| input | default | what it does |
| --- | --- | --- |
| `ZMQEndpoint` | `tcp://127.0.0.1:5555` | where the DLL publishes |
| `Enabled` | `True` | master switch |
| `LogErrors` | `True` | init failures, sub-minute / aggregated-tick chart detection, non-zero publish return codes |
| `LogPublish` | `False` | one line per publish call — see below |

### `LogPublish`

Prints all five quantity words per publish call. Since they go on the wire
verbatim, this line is also exactly what the subscriber receives:

```
[TS2Python] bar  2026-07/24-15:59:00 bar_type=1.00 bar_interval=1.00
            o=742.31 h=742.60 l=742.28 c=742.55
            volume=13465 ticks=21152 upticks=13465 downticks=7687 openint=0
            rc=0
```

That correspondence is the point: a stored partition can be checked against its
source line by line, with no mapping step in between to reason about. `volume`
and `ticks` sit side by side because that is the pair whose meaning flips
between chart types.

No interval is refused and no chart type stops the publish — `LogErrors` covers
two detections that fire once per chart and let publishing continue:

```
[TS2Python] sub-minute chart detected on symbol=SPY — two consecutive bars
            share date=1260724.00 time=1600.00. ts_str has minute resolution...
[TS2Python] aggregated tick chart detected on symbol=SPY — bar_interval=100.
            each call carries 100 prints aggregated into one bar...
```

Both used to stop the publish entirely — that was this script deciding the
data was not worth sending. Now `bar_type`/`bar_interval` travel on every
frame so a consumer can see what the chart was, and the wire's receive-side
`ts` is what separates same-minute frames.

Leave `LogPublish` off in normal use. On a tick chart, or on any chart in
"update every tick" mode, it prints once per print.

## Supported chart intervals

Every chart type is forwarded — there is no refusal and no idle state.

| chart | behaviour |
| --- | --- |
| 1-tick series (`BarType = 0`, `BarInterval = 1`) | sent print by print |
| N-tick series (`BarType = 0`, `BarInterval > 1`) | one aggregated bar per call; detected and logged once (see above), publishing continues |
| 1 / 5 / 15 / 30 / 60 minute and any other intraday interval (`BarType = 1`) | full OHLC sent, `bar_type`/`bar_interval` travel verbatim |
| Daily (`BarType = 2`, `BarInterval = 0` or `1`) | full OHLC sent. TradeStation 10 reports `0` here — `1` is accepted too, because that is what the ABI documented before a live install was measured |
| Weekly / monthly / P&F / any other bar type | forwarded the same way; `bar_type` names it, nothing maps or rejects it |
| Second chart (`BarType = 14`, `BarInterval` = seconds per bar) | fully supported — `TsStr` carries real seconds, so a 30-second chart's `07:20:00` and `07:20:30` bars stay distinct. Detected and logged once, publishing continues |

> **This was broken until 2026-08-03, and the failure was silent.** `TsStr` was
> built from EL's `Date`/`Time`, which carry no seconds — so both bars above
> formatted as `07:20:00`, the reference binding's intra-bar buffer read the
> second as a refinement of the first (correct behaviour for a real 1-minute
> chart under "Update Every Tick"), and a 30-second chart stored one bar per
> minute. Nothing raised.
>
> The publisher now uses `BarDateTime`, which TradeStation documents as
> carrying seconds, and the binding no longer floors them away. If you are
> running an older `.ELD` or an older binding, sub-minute charts still lose
> data — upgrade both. `contract/semantics.md` §1.3 has the measurement.

### Why all five quantity words go out, and none is chosen for you

TradeStation defines these reserved words with **opposite meanings** on intraday
and daily charts (stock symbols):

| | intraday | daily and up |
| --- | --- | --- |
| `Volume` | shares traded on **up ticks** | total shares |
| `Ticks` | **total shares** | number of ticks |
| `UpTicks` | up-tick shares | total shares |
| `DownTicks` | down-tick shares | 0 |

So the intuitive reading — `Volume` is the quantity, `Ticks` is the count — is
true only on daily. This indicator used to resolve that itself: one wire field
called `vol`, filled from whichever word the chart type made "total share
volume". Two things went wrong with that.

First, the original version always sent `Volume`, which on intraday is the
up-tick share volume alone — roughly half of what traded, and undetectable
downstream because it is a perfectly plausible number that is simply too small.

Second, and worse, the fix did not remove the problem so much as move it. The
choice still happened off the wire, so a **publisher-convention version number**
had to ride along on every payload just to say which rule had produced the
numbers — and that number could itself go stale, because this file lives in the
user's TradeStation install and nothing updates it when the DLL or a subscriber
is upgraded.

Shipping all five words verbatim, each in its own field named after the reserved
word, removes the choice and with it the need to declare anything. The
inversion is still a fact; it is now a table the consumer reads
([`../contract/semantics.md`](../contract/semantics.md) §3.4) rather than a
decision this file makes on their behalf. A consumer who wants total share
volume takes `el_ticks` intraday and `el_volume` on daily.

`OpenInt` is NOT open interest on an intraday chart. Measured live on SPY,
`@ES`, `VXX` and an SPY option (2026-08-02), and on every partition collected
since: **`el_open_interest` equals `el_downticks` on every intraday row,
whatever the category** — futures included, where real open interest would be
orders of magnitude larger. Open interest only appears on daily bars and up,
and there it is `DownTicks` that carries it. Two inversions, not one; both are
tabulated in [`../contract/semantics.md`](../contract/semantics.md) §3.4.

### What an N-tick chart actually sends

A tick series is one print per call only when `BarInterval = 1`. On a 100-tick
chart each call carries a finished bar: `Close` is the last of the hundred
prints, and the volume words cover all hundred under the intraday rule above —
`Ticks` their total share volume, `Volume` the up-tick part. Neither reports
`100`; EL has no count intraday.

**Nothing is refused.** `bar_interval` travels on the wire and says exactly how
many prints went into the call, so a consumer can see what it is holding. The
indicator used to stop publishing on these charts, back when the tick frame had
no field able to say the call was an aggregate — a consumer would then have
stored it as one trade priced at the last print but carrying a hundred prints'
volume, wrong by two orders of magnitude with nothing able to notice. The field
exists now, so the decision belongs downstream. It is still announced once in
the Print Log.

Measured on a live install: a 100-tick chart reports `bar_interval=100.00` at
init and `Ticks = 760951` (the hundred prints' share volume, not `100`), while
a 1-tick chart reports `1.00` and calls `EL_Publish` once per print.

Note this also means `TsStr` cannot separate the prints inside one **second**:
a 1-tick chart emits many calls stamped with the same second. What separates
them is the DLL's receive-side `ts`, which is why
[`../contract/semantics.md`](../contract/semantics.md) §1 makes that — not
`ts_str` — the tick's authoritative ordering.

### Second-based charts: what the guard is, and is not

`BarType` **does** tell a second-based chart from a minute one: TradeStation
reports `BarType = 14` for a Second chart, with `BarInterval` in seconds.
An earlier version of this file claimed both reported `1` / `1` and were
indistinguishable — that was written without measuring, and it is wrong.

The sub-minute latch in the indicator is now **informational only**. It fires
when two consecutive bars repeat the same minute-resolution `Date` / `Time`,
which tells you the chart is finer than a minute — but that no longer implies
anything is lost, because `TsStr` is built from `BarDateTime` and carries real
seconds. It does not stop publishing, and never should have.

> TradeStation also exposes `BarType_ext`. Its values differ between versions
> and have not been confirmed against a real installation, so nothing here uses
> it. To pin those values down: `Print(BarType_ext)` once on a known 1-minute
> chart and once on a 1-second chart.

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
  **They travel on every point, bars included.** Bars used to carry no quote,
  on the grounds that a live-quote function describes the moment of the call
  rather than the bar. That is true, and it is not this transport's call to
  make — the same reasoning that removed the hard-coded index-symbol list.
- The five quantity words are forwarded without conversion or selection. Any
  interpretation of them belongs to the consumer.

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
