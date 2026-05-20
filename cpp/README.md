# cpp — TS2Python Bridge DLL

> 📖 [繁體中文版](README.zh-TW.md)

C++ DLL that bridges TradeStation EasyLanguage calls to a ZeroMQ PUB socket. The Python side ([`tradestation-data-provider`](../README.md), root of this repo) subscribes over `tcp://127.0.0.1:5555` and routes the events through its pluggable sink pipeline.

This subdirectory is the **publisher** half of the system. The current ABI is **DLL version 6**.

## Wire format

Every publish call sends a two-frame ZMQ message:

| Frame | Content | Type |
| --- | --- | --- |
| 1 | Topic | UTF-8 symbol (e.g. `SPY`, `VXX`, `$TICK`) |
| 2 | Payload | JSON; one of two shapes below |

```jsonc
// Tick (EL_PublishTick) — single trade print
{ "v": 1, "kind": "tick", "ts": 1747700000.123, "ts_utc": 1747700000.0,
  "ts_str": "2026-04/18-13:30:45",
  "px": 450.0, "vol": 100, "bid": 449.99, "ask": 450.01, "tc": 1 }

// Bar (EL_PublishTickEx) — already-formed 1-min OHLC
{ "v": 1, "kind": "bar_1m", "ts": 1747700060.0, "ts_utc": 1747700060.0,
  "ts_str": "2026-04/18-13:31:00",
  "o": 450.1, "h": 450.75, "l": 449.8, "c": 450.4,
  "vol": 12000, "bid": 450.39, "ask": 450.41, "tc": 140 }
```

`ts` is the DLL's receive-side wall clock (UTC epoch); `ts_utc` is the EL string converted to UTC via `std::chrono::zoned_time` ("America/New_York" zone); `ts_str` is the raw EL `yyyy-MM/dd-HH:mm:ss` 24-hour timestamp passed through verbatim. The Python side treats `ts_str` as authoritative for bar `bucket_start`. For details see `../docs/design.md §3.2 / §5` and `../docs/error_codes.md`.

## Requirements

- **Target**: Win32 (x86) — TradeStation is a 32-bit process. **Do not deploy x64.** The x64 configuration exists only for quick local verification of the C++ code itself; it cannot be loaded into TradeStation.
- **Compiler**: Visual Studio 2022 / 2026 (Community edition or later) with the "Desktop development with C++" workload.
- **Dependencies**: `libzmq` + `cppzmq` — declared in `cpp/vcpkg.json`, fetched in vcpkg manifest mode.

## One-time setup — install vcpkg

vcpkg is Microsoft's C/C++ package manager. This project uses **manifest mode** (`vcpkg.json`): at build time MSBuild calls vcpkg, which downloads and compiles `zeromq` + `cppzmq` into `cpp/vcpkg_installed/`. **If vcpkg is not bootstrapped you will see `LNK1104: cannot open file 'libzmq.lib'`.**

vcpkg is pinned as a **git submodule** under `cpp/build-tools/vcpkg/` so every clone gets the same vcpkg revision and the same port versions — reproducible without picking a global install location.

### Step 1. Clone (with submodules)

First clone of the repo:

```powershell
git clone --recurse-submodules https://github.com/millerlai/tradestation-data-provider.git
```

Already cloned without submodules:

```powershell
cd <your-repo-root>          # e.g. D:\project\tradestation-data-provider
git submodule update --init --recursive
```

> All commands below assume the repo root as `<your-repo-root>`. Substitute your actual path.

After this, `ls cpp\build-tools\vcpkg` should show `bootstrap-vcpkg.bat`, `ports\`, `scripts\`, etc.

### Step 2. Bootstrap (produce `vcpkg.exe` inside the submodule)

Run from a **regular user-level PowerShell** (no admin needed):

```powershell
cd <your-repo-root>\cpp\build-tools\vcpkg
.\bootstrap-vcpkg.bat
```

When it finishes, `ls vcpkg.exe` should exist (≈10 MB). This executable is **not committed** to git (it's `.gitignore`'d); each checkout bootstraps it once.

### Step 3. Set `VCPKG_ROOT` to the submodule

```powershell
setx VCPKG_ROOT <your-repo-root>\cpp\build-tools\vcpkg
```

> `setx` persists a **user-level** environment variable but **does not affect the current PowerShell window**. Close PowerShell and open a fresh window, then `echo $env:VCPKG_ROOT` to confirm the value points at the submodule.

### Step 4. Integrate vcpkg with Visual Studio

In the **new** PowerShell window (with `VCPKG_ROOT` set):

```powershell
& "$env:VCPKG_ROOT\vcpkg.exe" integrate install
```

This is **machine-wide and one-shot**. It writes a `.targets` file to `%LOCALAPPDATA%\vcpkg\` pointing at the `vcpkg.exe` inside this submodule, so MSBuild auto-injects vcpkg's include / lib paths whenever a `.vcxproj` builds.

- No administrator privileges needed (writes to `%LOCALAPPDATA%`, user-level).
- Success message: `Applied user-wide integration for this vcpkg root.`
- Any `.vcxproj` with `VcpkgEnabled=true` and a `vcpkg.json` will now trigger manifest install automatically.

> Only one `integrate install` is active per machine at a time. If you have another project with its own vcpkg submodule, switching projects means re-running Steps 3 + 4 against that submodule.
>
> Undo with `& "$env:VCPKG_ROOT\vcpkg.exe" integrate remove`.

### Step 5. Verify the install

```powershell
cd <your-repo-root>\cpp
& "$env:VCPKG_ROOT\vcpkg.exe" install --triplet x86-windows
ls vcpkg_installed\x86-windows\lib\
```

You should see versioned lib names (e.g. `libzmq-mt-4_3_5.lib`). The vcxproj relies on `VcpkgAutoLink=true` plus the `#pragma comment(lib, ...)` inside `zmq.h` to link automatically — no manual `<AdditionalDependencies>` entry is needed.

### Upgrading / pinning vcpkg

vcpkg is a submodule; upgrade it the way you upgrade any submodule:

```powershell
cd cpp\build-tools\vcpkg
git fetch origin
git checkout <new-commit-or-tag>      # e.g. tag 2026.04.15
cd ..\..\..
git add cpp/build-tools/vcpkg         # update the superproject pointer
git commit -m "bump vcpkg to 2026.04.15"
# Re-bootstrap because vcpkg.exe may have changed
cd cpp\build-tools\vcpkg && .\bootstrap-vcpkg.bat
```

### Common setup errors

- **`cpp/build-tools/vcpkg/` is empty** → forgot `git submodule update --init --recursive`.
- **`'vcpkg.exe' is not recognized`** → Step 2 not done, or bootstrap failed (network / antivirus). Re-run `bootstrap-vcpkg.bat` and read the error.
- **`LNK1104: 'libzmq.lib'`** → usually (a) Step 4 `integrate install` was skipped; (b) `VCPKG_ROOT` was not set when Visual Studio launched (close *all* VS windows and reopen); or (c) someone hand-wrote `libzmq.lib` into `<AdditionalDependencies>` but vcpkg actually ships the versioned filename — remove the manual entry and let auto-link handle it.
- **First build is slow** → expected. vcpkg is downloading and compiling `zeromq` source for the x86-windows triplet (~3–5 min). Subsequent builds hit the binary cache and finish in seconds.

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
- vcpkg copies `libzmq.dll` to the output folder (applocal deployment) so the harness runs without further setup.

## Build — option B: CMake + command line

```powershell
cd cpp
cmake --preset x86-release
cmake --build --preset x86-release
cmake --install build/x86-release --prefix build/x86-release/stage
```

Output lands in `cpp/build/x86-release/stage/bin/`: `TS2Python.dll`, `TS2Python_TestHarness.exe`, `libzmq.dll`.

> Both build paths can coexist, but don't mix them: the VS solution writes to `cpp/Debug` / `cpp/Release`; CMake writes to `cpp/build/`. When you switch toolchains, delete the other side's output folder first to avoid a stale DLL being picked up by the linker.

## Deploy to TradeStation

1. Build **Release | x86** (TradeStation is strictly 32-bit; x64 will not load).
2. Copy both DLLs into `C:\Program Files (x86)\TradeStation <version>\Program\`:
   - `TS2Python.dll`
   - `libzmq.dll` (from `cpp/vcpkg_installed/x86-windows/bin/` or the CMake `stage/bin/`).
3. In the EasyLanguage editor, Verify the indicator that imports the DLL.

## C ABI

DLL version `EL_DllVersion() == 6`. See `../docs/design.md` and `../docs/error_codes.md` for full semantics (return codes, threading rules, lifecycle).

```c
int __stdcall EL_DllVersion(void);
int __stdcall EL_Init(const char* zmq_endpoint);

// Single trade print — Python aggregates ticks into 1-min bars.
int __stdcall EL_PublishTick(
    const char* symbol,
    const char* el_timestamp,   // "yyyy-MM/dd-HH:mm:ss" 24h, America/New_York; may be NULL/""
    double price, double volume,
    double bid, double ask, double tick_count);

// Already-formed OHLC bar — bypasses Python's tick aggregator.
// Used when the EL indicator runs in bar-close (or "update every tick") mode.
int __stdcall EL_PublishTickEx(
    const char* symbol,
    const char* el_timestamp,
    double bar_open, double bar_high, double bar_low, double bar_close,
    double volume, double bid, double ask, double tick_count);

int __stdcall EL_Shutdown(void);
```

Return codes: `0` success, `1` already initialized (idempotent re-init), `-1` not initialized, `-2` ZMQ send failed, `-3` init failed (bind / socket create), `-4` invalid argument.

The DLL pins itself into the host process on first successful `EL_Init` (Windows `GetModuleHandleExW` with `GET_MODULE_HANDLE_EX_FLAG_PIN`) so that TradeStation calling `FreeLibrary` does not trigger the C runtime's static-destructor chain — `zmq_ctx_term()` joining the ZMQ I/O thread under loader lock would deadlock TS otherwise. `EL_Shutdown()` exists for the standalone test harness only.

## Standalone test

Run the harness against the Python smoke subscriber to verify end-to-end without TradeStation:

```powershell
# Terminal A — start the subscriber first
python scripts/simple_sub.py --latency

# Terminal B — fire the harness
cpp\Release\TS2Python_TestHarness.exe --mode stress --rate 10000 --seconds 10
```

A successful harness exits with code `0` (no dropped sends). The subscriber should print ~100 000 `SPY`-topic messages and per-percentile latency stats. Other harness modes: `--mode smoke` (5 ticks across 3 topics, one bar via `EL_PublishTickEx`), `--mode multithread --threads 8 --per-thread 5000`.
