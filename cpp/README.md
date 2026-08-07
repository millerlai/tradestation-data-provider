# cpp — TS2Python Bridge DLL

> 📖 [繁體中文版](README.zh-TW.md)

C++ DLL that bridges TradeStation EasyLanguage calls to a ZeroMQ PUB socket. The Python side ([`tradestation-data-provider`](../README.md), root of this repo) subscribes over `tcp://127.0.0.1:5555` and routes the events through its pluggable sink pipeline.

This subdirectory is the **publisher** half of the system. The current ABI is **DLL version 2**, carrying wire `proto` 2. There is exactly one of each — see [`../contract/wire.md`](../contract/wire.md).

## Wire format

Every publish call sends a two-frame ZMQ message:

| Frame | Content | Type |
| --- | --- | --- |
| 1 | Topic | UTF-8 symbol (e.g. `SPY`, `VXX`, `$TICK`) |
| 2 | Payload | JSON; one of two shapes below |

```jsonc
// One shape, whatever the chart is. No `kind`, no `tf`.
{ "proto": 2, "seq": 1, "sid": 1785646054360588,
  "ts": 1785646062.364744, "ts_str": "2026-04/18-13:30:45",
  "bar_type": 0, "bar_interval": 1, "category": 2,
  "o": 450.0, "h": 450.0, "l": 450.0, "c": 450.0,
  "el_volume": 100, "el_ticks": 195, "el_upticks": 100,
  "el_downticks": 80, "el_open_interest": 0,
  "bid": 449.99, "ask": 450.01 }
```

**The five `el_*` fields are EasyLanguage's reserved words forwarded verbatim.** This ABI selects nothing and converts nothing — notably, `Volume` and `Ticks` mean opposite things on an intraday chart and a daily one, and reconciling them here is what previously made the numbers unauditable. [`../contract/semantics.md`](../contract/semantics.md) §3.4 has the table.

`ts` is the DLL's receive-side wall clock (UTC epoch), useful only for latency measurement; `ts_str` is the raw EL `yyyy-MM/dd-HH:mm:ss` 24-hour timestamp passed through verbatim, and subscribers treat it as authoritative for `bar_time`, landing it as sent — no shift, no grid. **The DLL no longer parses `ts_str`, so it no longer validates it** — an unparseable string travels intact and fails in the subscriber. Bars carry no `bid`/`ask`: a live quote describes the moment of the call, not the bar. For the normative rules see [`../contract/wire.md`](../contract/wire.md) and [`../contract/semantics.md`](../contract/semantics.md) §1–2.

## Requirements

- **Target**: Win32 (x86) — TradeStation is a 32-bit process. **Do not deploy x64.** The x64 configuration exists only for quick local verification of the C++ code itself; it cannot be loaded into TradeStation.
- **Compiler**: Visual Studio 2022 / 2026 (Community edition or later) with the "Desktop development with C++" workload.
- **Dependencies**: `libzmq` + `cppzmq` — declared in `cpp/vcpkg.json`, fetched in vcpkg manifest mode.

## One-time setup

Two scripts. Run them from `cpp\`:

```powershell
cd cpp
.\setup-build-env.bat        # check out vcpkg, bootstrap it, install deps
.\verify-build-env.bat       # confirm every prerequisite; exit 0 = ready
```

`setup-build-env.bat` is idempotent — re-run it after changing `vcpkg.json`, or any time you are not sure. `verify-build-env.bat` changes nothing and prints, for each prerequisite, either `[ OK ]` or the exact command that fixes it.

Then build:

```powershell
.\build.bat                  # Release, x86 and x64
.\build.bat Debug            # Debug, x86 and x64
.\build.bat all              # all four configurations
.\build.bat --x86            # x86 only — what TradeStation loads
.\build.bat --rebuild        # full rebuild rather than incremental
```

`build.bat` finds MSBuild itself, so no Developer Command Prompt is needed. Or drive the solution directly:

```powershell
# Visual Studio: open cpp\TS2Python.sln, select Release | x86, Build
msbuild TS2Python.sln /p:Configuration=Release /p:Platform=x86

# CMake (x86 only — CMakeLists.txt refuses anything else):
cmake --preset x86-release
cmake --build --preset x86-release
```

> **`vcpkg integrate install` is deliberately not part of this.** See [Why no `vcpkg integrate install`](#why-no-vcpkg-integrate-install) — running it is not merely unnecessary here, it is the thing that used to break the build.

### What setup-build-env.bat does

| Step | Action | Fails when |
| --- | --- | --- |
| 1 | Locate Visual Studio with the C++ toolset (via `vswhere`) | The "Desktop development with C++" workload is not installed |
| 2 | `git submodule update --init --recursive` if `cpp/build-tools/vcpkg/` is empty | The repo was downloaded as a `.zip` (no submodules) rather than cloned |
| 3 | `bootstrap-vcpkg.bat` if `vcpkg.exe` is missing | Network or antivirus blocks the download |
| 4 | `vcpkg install --triplet x86-windows` into `cpp/vcpkg_installed/` | A port fails to build; vcpkg prints the log path |

Pass `--with-x64` to also install the x64 triplet, which only the developer-only x64 configurations need. TradeStation loads the 32-bit DLL, so x86 is what matters.

vcpkg is pinned as a **git submodule** at `cpp/build-tools/vcpkg/`, so every clone resolves the same vcpkg revision and the same port versions.

### Upgrading / pinning vcpkg

```powershell
cd cpp\build-tools\vcpkg
git fetch origin
git checkout <new-commit-or-tag>      # e.g. tag 2026.04.15
cd ..\..\..
git add cpp/build-tools/vcpkg         # update the superproject pointer
git commit -m "bump vcpkg to 2026.04.15"
cd cpp && .\setup-build-env.bat       # re-bootstrap and re-install
```

## Troubleshooting

Run `verify-build-env.bat` first — it diagnoses everything below and names the fix. What follows is why each one happens.

### `error C1083: Cannot open include file: 'zmq.hpp'`

The compiler was never told where vcpkg's headers are. Either the dependencies are not installed, or vcpkg's MSBuild integration is not being applied.

```powershell
cd cpp
.\setup-build-env.bat
```

If it persists, check that `cpp\vcpkg_installed\x86-windows\x86-windows\include\zmq.hpp` exists.

**The triplet appears twice on purpose.** The outer directory is a *per-triplet install root*; the inner one is the ordinary triplet folder inside it. It looks like a mistake and is not. Collapsing the two into a single shared `vcpkg_installed\` makes the x86 and x64 builds delete each other's packages: manifest mode treats an install root as a managed tree and reconciles it against the current plan, so building x64 removes the x86 packages and the *next* x86 build fails with C1083 on a machine where it had just succeeded. Keep the roots separate.

If the header is missing, delete `cpp\vcpkg_installed\` and re-run `setup-build-env.bat`.

<a id="why-no-vcpkg-integrate-install"></a>
#### Why no `vcpkg integrate install`

The usual advice is to run it once per machine. It writes `%LOCALAPPDATA%\vcpkg\vcpkg.user.props` containing an **absolute path** to one vcpkg checkout — whichever one you happened to run it from. Every C++ project you build on that machine then uses that one.

Two ways it produces exactly this error:

1. **Never run.** Nothing imports vcpkg, no include directory is added, `zmq.hpp` is not found.
2. **Run from a different project, which then moved or was deleted.** The generated file guards its import with `Exists(...)`, so a stale path silently evaluates to false and nothing is imported — while `%LOCALAPPDATA%\vcpkg\vcpkg.user.props` still sits there looking correctly configured. This is not hypothetical; it is what happened in this repo, pointing at a `TradeStation-TradingAgent` checkout that no longer existed.

So the projects here import vcpkg **from the submodule instead**, via `cpp/vcpkg-local.props` and `cpp/vcpkg-local.targets`, and set vcpkg's own `VCPkgLocalAppDataDisabled` so the machine-global file is ignored even when present. A clone plus `setup-build-env.bat` is sufficient, and a stale global integration cannot affect this build.

`verify-build-env.bat` check `[6]` reports a stale global integration anyway — harmless here, but it will break every *other* vcpkg project on the machine until you re-run `vcpkg integrate install` from a checkout that exists, or `vcpkg integrate remove`.

### `LNK1104: cannot open file 'libzmq.lib'`

Same root cause as C1083, one stage later: headers resolved but the library directory did not. Run `setup-build-env.bat`.

If someone has hand-written `libzmq.lib` into `<AdditionalDependencies>`, remove it — vcpkg ships a *versioned* filename (`libzmq-mt-4_3_5.lib`) and the project links via `VcpkgAutoLink` plus the `#pragma comment(lib, ...)` inside `zmq.h`.

### `MSB4126: invalid solution configuration "Release|Win32"`

The solution's platform is named **`x86`**; `Win32` is the *project*-level name it maps to. Visual Studio's dropdown shows `x86`. From the command line:

```powershell
msbuild TS2Python.sln /p:Configuration=Release /p:Platform=x86
```

### `The build tools for v145 cannot be found`

`v145` ships with Visual Studio 2026. On VS 2022, build with its toolset instead:

```powershell
msbuild TS2Python.sln /p:TS2PythonToolset=v143 /p:Configuration=Release /p:Platform=x86
```

`verify-build-env.bat` check `[2]` lists the toolsets actually installed.

### `cpp/build-tools/vcpkg/` is empty

The repo was downloaded as a `.zip`, which does not carry submodules. Clone it instead:

```powershell
git clone --recurse-submodules https://github.com/millerlai/tradestation-data-provider.git
```

### First build takes minutes

Expected — vcpkg is compiling `zeromq` from source for the x86-windows triplet. Later builds hit the binary cache and finish in seconds.

### `warning MSB4011: GetGlobalProperties.task ... already imported`

Harmless, and upstream: vcpkg's own `vcpkg.targets` and `Bootstrap.targets` both import that task file. MSBuild skips the duplicate and the build succeeds.

## Build — option A: Visual Studio solution (recommended for daily work)

Open `cpp/TS2Python.sln`, select **`Release | x86`** (the deploy configuration) or any Debug variant, then Build.

- On first build, VS reads `vcpkg.json` and downloads + compiles `zeromq` + `cppzmq` into `cpp/vcpkg_installed/<triplet>/`.
- The solution contains two projects:
  - **TS2Python** — `src/ts2python.cpp` → `TS2Python.dll` (plus `TS2Python.lib` import lib).
  - **TS2Python_TestHarness** — `src/test_harness.cpp` → `TS2Python_TestHarness.exe`, linked against the above via project reference.
- Output paths:
  - Win32 Debug: `cpp/Debug/`
  - Win32 Release: `cpp/Release/`
  - x64 variants: `cpp/x64/<Debug|Release>/`
- vcpkg copies the ZeroMQ runtime DLL to the output folder (applocal deployment) so the harness runs without further setup. Note the filename is **versioned** — currently `libzmq-mt-4_3_5.dll`, not `libzmq.dll` — and changes when the pinned vcpkg revision moves.

## Build — option B: CMake + command line

```powershell
cd cpp
cmake --preset x86-release
cmake --build --preset x86-release
cmake --install build/x86-release --prefix build/x86-release/stage
```

Output lands in `cpp/build/x86-release/stage/bin/`: `TS2Python.dll`, `TS2Python_TestHarness.exe`, and the versioned ZeroMQ runtime (`libzmq-mt-4_3_5.dll` at the current pin).

> Both build paths can coexist, but don't mix them: the VS solution writes to `cpp/Debug` / `cpp/Release`; CMake writes to `cpp/build/`. When you switch toolchains, delete the other side's output folder first to avoid a stale DLL being picked up by the linker.

## Deploy to TradeStation

```powershell
.\build.bat --x86                 # skip this to install the binary in prebuilt\
.\install-to-tradestation.bat
```

`install-to-tradestation.bat` locates the TradeStation `Program` folder under the usual roots on `C:` and `D:` (recognised by `ORPlat.exe` / `TSDev.exe` / `TSCLUtil.exe` — there is no `TradeStation.exe`), and asks for the path with an example when it finds none. Then, before copying anything:

- It reads the **PE header of the platform executable** to decide whether to install the x86 or the x64 build, and refuses a DLL whose architecture disagrees. TradeStation is a 32-bit process on a 64-bit Windows, so "x64 machine, therefore x64 DLL" is the mistake being guarded against.
- It copies **every `.dll` beside `TS2Python.dll`**, which is how the ZeroMQ runtime comes along — **`libzmq-mt-4_3_5.dll`**, not `libzmq.dll`. vcpkg emits a versioned filename that moves when the pinned revision does, so it is never typed from memory.
- It checks that each file it is about to replace can be **opened for writing**, and refuses only when one cannot. TradeStation being open is not on its own a reason to stop: Windows locks the DLL only once EasyLanguage has loaded it, so installing while the platform runs but the indicator has never been charted works. It reports a destination it cannot write to as needing an elevated prompt, and warns when the **Visual C++ 2015-2022 Redistributable (x86)** is missing — the DLL is linked against the dynamic CRT, and EasyLanguage reports its absence only as an unexplained load failure.
- It asks separately before replacing a `TS2Python.dll` that is already installed, showing both files' dates.

Source preference is: solution build (`cpp\Release`) → CMake build (`cpp\build\x86-release\Release`) → the binaries checked into [`prebuilt/`](prebuilt/) for people without a C++ toolchain. A local build always wins over the shipped one.

Then, in the EasyLanguage editor, Verify the indicator that imports the DLL.

## C ABI

DLL version `EL_DllVersion() == 3`. See [`../contract/error_codes.md`](../contract/error_codes.md) for return codes.

```c
int __stdcall EL_DllVersion(void);

// Bind the publisher (once per process) and announce this chart. Returns -7
// until a subscriber is attached — see below.
int __stdcall EL_Init(
    const char* zmq_endpoint,
    const char* symbol, int category, int bar_type, int bar_interval);

// Single data point. Quantities are EasyLanguage reserved words, verbatim.
// They arrive as double because DefineDLLFunc has no 64-bit integer type;
int __stdcall EL_Publish(
    const char* symbol, const char* el_timestamp,
    int bar_type, int bar_interval, int category,
    double o, double h, double l, double c,
    double volume, double ticks, double upticks,
    double downticks, double open_interest,
    double bid, double ask);
```

Return codes: `0` success, `1` this chart already announced, `-1` not initialized, `-2` ZMQ send failed, `-3` init failed (bind / socket create), `-4` invalid argument, `-6` ABI mismatch (tombstone), `-7` no subscriber yet.

### `-7`, and why init can refuse to succeed

The socket is **XPUB**, not PUB. Send semantics are identical; the difference is that subscriptions come back as readable messages, so the DLL can tell whether anyone is attached.

`EL_Init` returns `-7` and publishes nothing until a subscriber covers the control topic `__ts2py__`. That is not a failure — it is the normal state whenever TradeStation starts before the consumer. The indicator leaves `InitDone` False on any negative rc and retries on the next bar.

It exists because PUB/SUB discards everything sent with nobody attached and reports nothing at all. The previous init returned `0` the moment `bind()` succeeded, so the Print Log said "init ok" while every frame went in the bin.

On success `EL_Init` publishes a **hello** frame on `__ts2py__` naming the chart (`symbol`, `category`, `bar_type`, `bar_interval`). The DLL remembers every chart and re-announces all of them whenever a subscriber attaches, so restarting the consumer does not require touching TradeStation. Subscription matching is by **prefix**, exactly as ZMQ filters, so a consumer using `SUBSCRIBE ""` counts.

### The tombstones, and the hole this revision opened

`EL_PublishTick` and `EL_PublishBar` once **kept their names while changing signature**. They are `__stdcall`, where the callee cleans the stack, so a call with the wrong argument count corrupts the stack — TradeStation crashes or misbehaves rather than returning an error. They remain exported, returning `-6`.

**They no longer protect anything.** The guard was that init's name changed on every signature change (`EL_Init` → `EL_Init2` → `EL_Init3`), so an old `.ELD` failed at init and never reached a moved publish signature. This revision reuses `EL_Init` with five parameters where the old one had one — and `DefineDLLFunc` resolves by name alone, so an old `.ELD` resolves it, calls it, and **corrupts the stack inside init**, before any tombstone is reachable. Nothing on the callee side can detect the argument count.

| Deployment | Caught by | Result |
| --- | --- | --- |
| new `.ELD` + old DLL | old DLL has no 5-parameter `EL_Init` export | `DefineDLLFunc` fails at Verify, with a named error |
| old `.ELD` calling `EL_PublishTick`/`Bar` + new DLL | tombstone returns `-6` | `rc=-6` in the Print Log; never publishes |
| new `.ELD` + a future DLL | the indicator's `EL_DllVersion()` latch | `EL_DllVersion` takes no arguments, so calling it is always safe; anything `!= 3` latches publishing off |
| **old `.ELD` calling 1-arg `EL_Init` + new DLL** | **nothing** | **stack corruption; TradeStation crashes or misbehaves** |

**Install the DLL and re-Verify the `.ELD` together.** That procedure is now the only thing preventing the last row.

The tombstones still **stay in `TS2Python.def`**: dropping an export only turns a `-6` into a symbol-resolution failure that names no cause.

The DLL pins itself into the host process on first successful `EL_Init` (Windows `GetModuleHandleExW` with `GET_MODULE_HANDLE_EX_FLAG_PIN`) so that TradeStation calling `FreeLibrary` does not trigger the C runtime's static-destructor chain — `zmq_ctx_term()` joining the ZMQ I/O thread under loader lock would deadlock TS otherwise. `EL_Shutdown()` exists for the standalone test harness only.

## Standalone test

**Start the subscriber first — this is now mandatory, not a race-avoidance tip.** `EL_Init` returns `-7` until one is attached, so a harness run with no SUB waits out `--subscriber-timeout-ms` (default 15000) and exits non-zero.

```powershell
# Terminal A — the subscriber MUST be up first
python contract/tools/record.py --latency

# Terminal B — fire the harness
cpp\Release\TS2Python_TestHarness.exe --mode stress --rate 10000 --seconds 10
```

A successful harness exits with code `0` (no dropped sends). The subscriber should print two `__ts2py__` hello frames, then ~100 000 `SPY`-topic messages and per-percentile latency stats. Other harness modes: `--mode smoke` (3 topics plus one bar), `--mode noquote`, `--mode bars` (every `BarType`/`BarInterval` combination, none refused — including ones a `-5` mapping used to reject outright), `--mode session`, `--mode multithread --threads 8 --per-thread 5000`. Each fixture's mode and frame count is tabulated in [`../contract/fixtures/README.md`](../contract/fixtures/README.md).

**Every run first asserts the ABI**: `EL_DllVersion() == 3`, and both tombstones returning `-6`, before any init. It then checks that re-announcing the same chart returns `1` while a second, different chart returns `0`. A check that only runs when someone remembers to run it is not a check.
