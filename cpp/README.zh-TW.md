# cpp — TS2Python Bridge DLL

> 📖 [English version](README.md)

C++ DLL，把 TradeStation EasyLanguage 的呼叫橋到 ZeroMQ PUB socket。Python 端（[`tradestation-data-provider`](../README.md)，本 repo 根目錄）透過 `tcp://127.0.0.1:5555` 訂閱，再把收到的事件交給可插拔的 sink pipeline。

這個子目錄是整個系統的**發布端**。當前 ABI 是 **DLL version 6**。

## Wire format

每次 publish 會送一個雙 frame ZMQ 訊息：

| Frame | 內容 | 型別 |
| --- | --- | --- |
| 1 | Topic | UTF-8 symbol（例：`SPY`、`VXX`、`$TICK`）|
| 2 | Payload | JSON；以下兩種 shape 之一 |

```jsonc
// Tick (EL_PublishTick) — 單筆成交
{ "v": 1, "kind": "tick", "ts": 1747700000.123, "ts_utc": 1747700000.0,
  "ts_str": "2026-04/18-13:30:45",
  "px": 450.0, "vol": 100, "bid": 449.99, "ask": 450.01, "tc": 1 }

// Bar (EL_PublishTickEx) — 已成形的 1 分鐘 OHLC
{ "v": 1, "kind": "bar_1m", "ts": 1747700060.0, "ts_utc": 1747700060.0,
  "ts_str": "2026-04/18-13:31:00",
  "o": 450.1, "h": 450.75, "l": 449.8, "c": 450.4,
  "vol": 12000, "bid": 450.39, "ask": 450.41, "tc": 140 }
```

`ts` 是 DLL 收到時刻的 wall clock（UTC epoch）；`ts_utc` 是 EL 字串透過 `std::chrono::zoned_time`（"America/New_York" zone）轉成 UTC；`ts_str` 是 EL 原始 `yyyy-MM/dd-HH:mm:ss` 24 小時字串，原文 pass-through。Subscriber 對 bar 的 `bucket_start` **以 `ts_str` 為準**。規範見 [`../contract/semantics.md`](../contract/semantics.md) §1–2 及 [`../contract/v1/envelope.md`](../contract/v1/envelope.md)。

## 要求

- **Target**：Win32 (x86) — TradeStation 是 32-bit process，**絕對不可部署 x64**。x64 設定只供本機快速驗證 C++ 程式碼本身，無法被 TradeStation 載入。
- **Compiler**：Visual Studio 2022 / 2026（Community 以上），含 "Desktop development with C++" workload。
- **Dependencies**：`libzmq` + `cppzmq` — 由 `cpp/vcpkg.json` 以 manifest 模式管理。

## 一次性準備

兩支腳本，都在 `cpp\` 底下執行：

```powershell
cd cpp
.\setup-build-env.bat        # 取出 vcpkg、bootstrap、安裝相依套件
.\verify-build-env.bat       # 逐項確認環境；exit 0 代表可以 build
```

`setup-build-env.bat` 可重複執行 —— 改過 `vcpkg.json` 之後、或任何時候不確定環境狀態，直接再跑一次即可。`verify-build-env.bat` 不改動任何東西，只逐項印出 `[ OK ]` 或**修正指令**。

接著 build：

```powershell
.\build.bat                  # Release，x86 與 x64
.\build.bat Debug            # Debug，x86 與 x64
.\build.bat all              # 四種組態全跑
.\build.bat --x86            # 只 build x86 —— TradeStation 載入的那個
.\build.bat --rebuild        # 完整重建，不做增量
```

`build.bat` 會自己找到 MSBuild，不需要開 Developer Command Prompt。或者直接操作方案：

```powershell
# Visual Studio：開 cpp\TS2Python.sln，選 Release | x86，Build
msbuild TS2Python.sln /p:Configuration=Release /p:Platform=x86

# CMake（只支援 x86，CMakeLists.txt 會擋掉其他架構）：
cmake --preset x86-release
cmake --build --preset x86-release
```

> **這裡刻意不需要 `vcpkg integrate install`。** 見[為什麼不用 `vcpkg integrate install`](#why-no-vcpkg-integrate-install-zh) —— 跑它不只是多餘，它正是以前把 build 弄壞的元凶。

### setup-build-env.bat 做什麼

| 步驟 | 動作 | 什麼情況會失敗 |
| --- | --- | --- |
| 1 | 用 `vswhere` 找出含 C++ toolset 的 Visual Studio | 沒安裝 "Desktop development with C++" workload |
| 2 | `cpp/build-tools/vcpkg/` 是空的就跑 `git submodule update --init --recursive` | 用 `.zip` 下載而非 `git clone`（`.zip` 不含 submodule） |
| 3 | 沒有 `vcpkg.exe` 就跑 `bootstrap-vcpkg.bat` | 網路或防毒軟體擋下載 |
| 4 | `vcpkg install --triplet x86-windows` 裝到 `cpp/vcpkg_installed/` | 某個 port 編譯失敗，vcpkg 會印出 log 路徑 |

加 `--with-x64` 可以連 x64 triplet 一起裝，那只有開發用的 x64 組態需要。TradeStation 載入的是 32 位元 DLL，x86 才是真正要緊的那個。

vcpkg 以 **git submodule** 釘在 `cpp/build-tools/vcpkg/`，所以每個 clone 解析到的 vcpkg 版本與 port 版本完全一致。

### 升級 / 釘版 vcpkg

```powershell
cd cpp\build-tools\vcpkg
git fetch origin
git checkout <new-commit-or-tag>      # 例 tag 2026.04.15
cd ..\..\..
git add cpp/build-tools/vcpkg         # 更新 superproject 指標
git commit -m "bump vcpkg to 2026.04.15"
cd cpp && .\setup-build-env.bat       # 重新 bootstrap 並重裝
```

## 疑難排解

先跑 `verify-build-env.bat` —— 底下每一項它都會診斷並直接給出修正指令。以下說明的是「為什麼會發生」。

### `error C1083: 無法開啟包含檔案: 'zmq.hpp'`

編譯器從來不知道 vcpkg 的 header 在哪。不是相依套件沒裝，就是 vcpkg 的 MSBuild 整合沒有生效。

```powershell
cd cpp
.\setup-build-env.bat
```

若還是一樣，檢查 `cpp\vcpkg_installed\x86-windows\x86-windows\include\zmq.hpp` 是否存在。

**triplet 名稱出現兩次是刻意的。** 外層是「每個 triplet 各自的 install root」，內層才是該 root 裡面正常的 triplet 資料夾。看起來像寫錯，其實不是。若把兩層併成單一共用的 `vcpkg_installed\`，x86 與 x64 的 build 會互相刪掉對方的套件：manifest 模式把 install root 當成受管理的樹，每次都對齊當前的 install plan，所以 build x64 會移除 x86 的套件，於是**下一次** x86 build 就在剛剛才成功的機器上噴 C1083。兩個 root 必須分開。

若 header 不存在，刪掉整個 `cpp\vcpkg_installed\` 再跑一次 `setup-build-env.bat`。

<a id="why-no-vcpkg-integrate-install-zh"></a>
#### 為什麼不用 `vcpkg integrate install`

一般教學都叫你每台機器跑一次。它會寫出 `%LOCALAPPDATA%\vcpkg\vcpkg.user.props`，裡面是**指向某一個 vcpkg checkout 的絕對路徑** —— 就是你當時所在的那一個。之後這台機器上所有 C++ 專案都會用那一份。

它有兩種方式產生這個錯誤：

1. **從來沒跑過。** 沒有東西匯入 vcpkg，沒有任何 include 目錄被加進去，`zmq.hpp` 自然找不到。
2. **在別的專案跑過，而那個專案後來被搬走或刪掉了。** 產生出來的檔案用 `Exists(...)` 當匯入條件，所以失效路徑會靜默判為 false、什麼都不匯入 —— 而 `%LOCALAPPDATA%\vcpkg\vcpkg.user.props` 還好端端躺在那裡，看起來設定完全正常。這不是假設：本 repo 遇到的就是這個，它指向一個早已不存在的 `TradeStation-TradingAgent` checkout。

所以這裡的專案改成**直接從 submodule 匯入 vcpkg**（`cpp/vcpkg-local.props` 與 `cpp/vcpkg-local.targets`），並設定 vcpkg 自己的 `VCPkgLocalAppDataDisabled`，讓那個全域檔案即使存在也被忽略。clone 之後跑 `setup-build-env.bat` 就夠了，失效的全域整合再也影響不到這個 build。

`verify-build-env.bat` 的 `[6]` 仍然會回報失效的全域整合 —— 對本 repo 無害，但這台機器上**其他**用 vcpkg 的專案都會壞，直到你從一個存在的 checkout 重跑 `vcpkg integrate install`，或用 `vcpkg integrate remove` 清掉為止。

### `LNK1104: 無法開啟檔案 'libzmq.lib'`

跟 C1083 同一個根因，只是晚一個階段：header 找到了，但 library 目錄沒有。跑 `setup-build-env.bat`。

如果有人手動在 `<AdditionalDependencies>` 填了 `libzmq.lib`，請移除 —— vcpkg 輸出的是**版本化**檔名（`libzmq-mt-4_3_5.lib`），專案是靠 `VcpkgAutoLink` 加上 `zmq.h` 裡的 `#pragma comment(lib, ...)` 自動連結的。

### `MSB4126: 指定的方案組態 "Release|Win32" 無效`

方案層級的平台叫 **`x86`**，`Win32` 是它對應到的**專案**層級名稱。Visual Studio 下拉選單顯示的是 `x86`。命令列要寫：

```powershell
msbuild TS2Python.sln /p:Configuration=Release /p:Platform=x86
```

### `找不到 v145 的建置工具`

`v145` 隨 Visual Studio 2026 提供。在 VS 2022 上請改用它的 toolset：

```powershell
msbuild TS2Python.sln /p:TS2PythonToolset=v143 /p:Configuration=Release /p:Platform=x86
```

`verify-build-env.bat` 的 `[2]` 會列出實際安裝了哪些 toolset。

### `cpp/build-tools/vcpkg/` 是空的

repo 是用 `.zip` 下載的，而 `.zip` 不含 submodule。改用 clone：

```powershell
git clone --recurse-submodules https://github.com/millerlai/tradestation-data-provider.git
```

### 第一次 Build 要跑好幾分鐘

正常 —— vcpkg 正在為 x86-windows triplet 從原始碼編譯 `zeromq`。之後會走 binary cache，數秒完成。

### `warning MSB4011: GetGlobalProperties.task ... 已經匯入`

無害，且是上游問題：vcpkg 自己的 `vcpkg.targets` 與 `Bootstrap.targets` 都匯入了同一個 task 檔。MSBuild 會略過重複的那次，build 仍然成功。

## Build — 方式 A：Visual Studio 解決方案（推薦日常開發）

開 `cpp/TS2Python.sln`，選 **`Release | x86`**（部署給 TradeStation）或 Debug 任一，直接 Build。

- 第一次 Build 時 VS 會依 `vcpkg.json` 自動下載並編譯 `zeromq` + `cppzmq`，落地到 `cpp/vcpkg_installed/<triplet>/`。
- 解決方案含兩個專案：
  - **TS2Python** — `src/ts2python.cpp` → `TS2Python.dll`（+ `TS2Python.lib` import lib）。
  - **TS2Python_TestHarness** — `src/test_harness.cpp` → `TS2Python_TestHarness.exe`（透過 project reference 連結 TS2Python.lib）。
- 輸出路徑：
  - Win32 Debug：`cpp/Debug/`
  - Win32 Release：`cpp/Release/`
  - x64 變體：`cpp/x64/<Debug|Release>/`
- vcpkg 自動把 ZeroMQ runtime DLL 複製到輸出資料夾（applocal deployment），harness 可直接執行。注意檔名是**帶版本的** —— 目前是 `libzmq-mt-4_3_5.dll`，不是 `libzmq.dll` —— 且會隨釘住的 vcpkg 版本變動。

## Build — 方式 B：CMake + 命令列

```powershell
cd cpp
cmake --preset x86-release
cmake --build --preset x86-release
cmake --install build/x86-release --prefix build/x86-release/stage
```

輸出：`cpp/build/x86-release/stage/bin/` 底下的 `TS2Python.dll`、`TS2Python_TestHarness.exe`，以及帶版本的 ZeroMQ runtime（目前釘版為 `libzmq-mt-4_3_5.dll`）。

> 兩種方式可以並存，但不要混用：VS .sln 用 `cpp/Debug`、`cpp/Release` 做輸出；CMake 用 `cpp/build/`。如果切換工具鏈，建議先刪掉另一邊的 output 資料夾避免 stale DLL 被 linker 抓到。

## 部署到 TradeStation

1. Build **Release | x86**（TS 絕對是 32-bit process，x64 不會被載入）。
2. 把下列檔案複製到 `C:\Program Files (x86)\TradeStation <version>\Program\`：
   - `TS2Python.dll`
   - 旁邊那個 ZeroMQ runtime —— **`libzmq-mt-4_3_5.dll`**，不是 `libzmq.dll`。vcpkg 輸出的是帶版本的檔名，所以請直接複製 `cpp\Release\` 裡與 `TS2Python.dll` 並排的那個 `.dll`，不要憑印象打檔名；它會隨釘住的 vcpkg 版本改變。

   ```powershell
   # 在 cpp\ 底下，build 完 Release|x86 之後：
   Copy-Item Release\*.dll "C:\Program Files (x86)\TradeStation <version>\Program\"
   ```
3. TradeStation EasyLanguage Editor 裡重新 Verify 使用這支 DLL 的 indicator。

## C ABI

DLL 版本 `EL_DllVersion() == 8`。return codes 見 [`../contract/error_codes.md`](../contract/error_codes.md)，ABI × wire 版本對應見 [`../contract/compat.md`](../contract/compat.md)。

```c
int __stdcall EL_DllVersion(void);
int __stdcall EL_Init(const char* zmq_endpoint);

// 單筆成交 — Python 端會把 tick 聚合成 1 分鐘 bar。
int __stdcall EL_PublishTick(
    const char* symbol,
    const char* el_timestamp,   // "yyyy-MM/dd-HH:mm:ss" 24h，America/New_York；可為 NULL/""
    double price, double volume,
    double bid, double ask, double tick_count);

// 已成形 OHLC bar — 跳過 Python 的 tick aggregator。
// EL indicator 用 bar-close（或 "update every tick"）模式時使用。
int __stdcall EL_PublishTickEx(
    const char* symbol,
    const char* el_timestamp,
    double bar_open, double bar_high, double bar_low, double bar_close,
    double volume, double bid, double ask, double tick_count);

int __stdcall EL_Shutdown(void);
```

Return codes：`0` 成功、`1` 已初始化（重複呼叫 idempotent）、`-1` 未初始化、`-2` ZMQ send 失敗、`-3` init 失敗（bind / socket create）、`-4` 參數無效。

DLL 在第一次成功 `EL_Init` 時，**把自己 pin 在 host process 的位址空間裡**（Windows `GetModuleHandleExW` 加 `GET_MODULE_HANDLE_EX_FLAG_PIN`）。這是為了避免 TradeStation 呼叫 `FreeLibrary` 觸發 C runtime 的 static destructor — 在 loader lock 下 `zmq_ctx_term()` join ZMQ I/O thread 會 deadlock TradeStation。`EL_Shutdown()` 僅供 standalone test harness 使用。

## 獨立測試

不用 TradeStation 就能驗證 wire end-to-end：harness 直接餵 Python smoke subscriber。

```powershell
# Terminal A — 先跑 subscriber
python contract/tools/record.py --latency

# Terminal B — 跑 harness
cpp\Release\TS2Python_TestHarness.exe --mode stress --rate 10000 --seconds 10
```

Harness 退 `0` 表示沒掉訊。Subscriber 端應看到 ~100 000 筆 `SPY` topic 訊息，以及 p50 / p95 / p99 延遲統計。其他 harness 模式：`--mode smoke`（5 筆 ticks 跨 3 個 topic + 1 個 `EL_PublishTickEx` bar）、`--mode multithread --threads 8 --per-thread 5000`。
