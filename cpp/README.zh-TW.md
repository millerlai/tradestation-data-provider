# cpp — TS2Python Bridge DLL

> 📖 [English version](README.md)

C++ DLL，把 TradeStation EasyLanguage 的呼叫橋到 ZeroMQ PUB socket。Python 端（[`tradestation-data-provider`](../README.md)，本 repo 根目錄）透過 `tcp://127.0.0.1:5555` 訂閱，再把收到的事件交給可插拔的 sink pipeline。

這個子目錄是整個系統的**發布端**。當前 ABI 是 **DLL version 2**，承載 wire `proto` 2。兩者都只有一個版本 —— 見 [`../contract/wire.md`](../contract/wire.md)。

## Wire format

每次 publish 會送一個雙 frame ZMQ 訊息：

| Frame | 內容 | 型別 |
| --- | --- | --- |
| 1 | Topic | UTF-8 symbol（例：`SPY`、`VXX`、`$TICK`）|
| 2 | Payload | JSON；以下兩種 shape 之一 |

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

**五個 `el_*` 欄位是 EasyLanguage reserved word 的原文轉送。** 這層 ABI 不做選擇也不做換算 —— 特別是 `Volume` 與 `Ticks` 在 intraday 圖與 daily 圖上意義相反，先前在這裡替使用者「調和」它們，正是讓數字變得無法事後稽核的原因。對照表見 [`../contract/semantics.md`](../contract/semantics.md) §3.4。

`ts` 是 DLL 收到時刻的 wall clock（UTC epoch），只用於量測延遲；`ts_str` 是 EL 原始 `yyyy-MM/dd-HH:mm:ss` 24 小時字串，原文 pass-through，subscriber 的 `bar_time` **以它為準**，原樣落地，不位移也不對齊格線。**DLL 不再解析 `ts_str`，因此也不再驗證它** —— 無法解析的字串會原樣送出，在 subscriber 端才失敗。Bar 不帶 `bid`/`ask`：即時報價描述的是呼叫當下，不是那根 bar。規範見 [`../contract/wire.md`](../contract/wire.md) 與 [`../contract/semantics.md`](../contract/semantics.md) §1–2。

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

```powershell
.\build.bat --x86                 # 不想自己 build 就跳過，直接裝 prebuilt\ 裡的
.\install-to-tradestation.bat
```

`install-to-tradestation.bat` 會在 `C:` 與 `D:` 的常見根目錄下找出 TradeStation 的 `Program` 資料夾（靠 `ORPlat.exe` / `TSDev.exe` / `TSCLUtil.exe` 辨識 —— 那裡沒有 `TradeStation.exe`），找不到就直接問你並附上範例路徑。複製任何檔案之前它會：

- 讀**平台執行檔的 PE header** 決定要裝 x86 還是 x64，架構不符的 DLL 直接拒絕。TradeStation 是跑在 64 位元 Windows 上的 32 位元 process，「機器是 x64 所以裝 x64」正是這裡要擋的錯。
- 複製**與 `TS2Python.dll` 並排的每一個 `.dll`**，ZeroMQ runtime 就是這樣一起帶過去的 —— **`libzmq-mt-4_3_5.dll`**，不是 `libzmq.dll`。vcpkg 輸出的是帶版號的檔名，會隨釘住的 vcpkg 版本改變，所以絕不憑印象打檔名。
- 檢查每個即將被覆蓋的檔案**能不能開起來寫**，開不了才拒絕。TradeStation 開著本身不是停下來的理由：DLL 要等 EasyLanguage 真的載入它之後才會被 Windows 鎖住，所以平台開著、但指標從沒上過圖時照樣能裝。目標不可寫時會告訴你要用系統管理員身分執行；缺少 **Visual C++ 2015-2022 Redistributable (x86)** 時會警告 —— DLL 連的是 dynamic CRT，而 EasyLanguage 只會回報一個沒有原因的載入失敗。
- 目標已有 `TS2Python.dll` 時，會單獨問你要不要取代，並列出兩個檔案的日期。

來源的優先順序是：.sln 建置（`cpp\Release`）→ CMake 建置（`cpp\build\x86-release\Release`）→ 給沒有 C++ 工具鏈的人用、已 commit 進 repo 的 [`prebuilt/`](prebuilt/)。本機建置永遠優先於隨 repo 附上的版本。

最後在 TradeStation EasyLanguage Editor 裡重新 Verify 使用這支 DLL 的 indicator。

## C ABI

DLL 版本 `EL_DllVersion() == 1`。return codes 見 [`../contract/error_codes.md`](../contract/error_codes.md)。

```c
int __stdcall EL_DllVersion(void);
int __stdcall EL_Init3(const char* zmq_endpoint);

// 單筆成交。量值是 EasyLanguage reserved word 的原文。
// 型別是 double，因為 DefineDLLFunc 沒有 64 位元整數型別；
int __stdcall EL_Publish(
    const char* symbol, const char* el_timestamp,
    int bar_type, int bar_interval, int category,
    double o, double h, double l, double c,
    double volume, double ticks, double upticks,
    double downticks, double open_interest,
    double bid, double ask);
```

Return codes：`0` 成功、`1` 已初始化（重複呼叫 idempotent）、`-1` 未初始化、`-2` ZMQ send 失敗、`-3` init 失敗（bind / socket create）、`-4` 參數無效、`-5` 無法對應的 bar 間隔、`-6` ABI 不符（墓碑）。

### 為什麼要留墓碑

`EL_PublishTick` 與 `EL_PublishBar` **名字沒改，簽章改了**。兩者都是 `__stdcall`（callee 清堆疊），所以參數個數不符的呼叫會**損毀堆疊** —— 不是回傳錯誤，是 TradeStation 崩潰或隨機行為。

改掉 init 的名字，就讓那條路徑走不到。indicator 裡每一次 publish 都由 `InitDone` 守衛，而 `InitRC < 0` 時 `InitDone` 永遠是 False，所以 **init 是唯一的攔截點**：

| 部署組合 | 攔截點 | 結果 |
| --- | --- | --- |
| 新 `.ELD` + 舊 DLL | 舊 DLL 沒有 `EL_Init3` 匯出 | `DefineDLLFunc` 在 Verify 階段就失敗，錯誤訊息指名道姓 |
| 舊 `.ELD` + 新 DLL | 墓碑回 `-6` | Print Log 出現 `EL_Init FAILED rc=-6`，一次都不會 publish |
| 新 `.ELD` + 未來某版 DLL | indicator 端的 `EL_DllVersion()` latch | `EL_DllVersion` 沒有參數，呼叫永遠安全；回值 `<> 1` 就 latch 停止發布 |

墓碑各只有三行，而且**必須留在 `TS2Python.def` 裡**。把匯出刪掉一樣安全，但 operator 拿到的會是一個 symbol 解析失敗，而不是一句看得懂、能照著做的話。

DLL 在第一次成功 `EL_Init3` 時，**把自己 pin 在 host process 的位址空間裡**（Windows `GetModuleHandleExW` 加 `GET_MODULE_HANDLE_EX_FLAG_PIN`）。這是為了避免 TradeStation 呼叫 `FreeLibrary` 觸發 C runtime 的 static destructor — 在 loader lock 下 `zmq_ctx_term()` join ZMQ I/O thread 會 deadlock TradeStation。`EL_Shutdown()` 僅供 standalone test harness 使用。

## 獨立測試

不用 TradeStation 就能驗證 wire end-to-end：harness 直接餵 Python smoke subscriber。

```powershell
# Terminal A — 先跑 subscriber
python contract/tools/record.py --latency

# Terminal B — 跑 harness
cpp\Release\TS2Python_TestHarness.exe --mode stress --rate 10000 --seconds 10
```

Harness 退 `0` 表示沒掉訊。Subscriber 端應看到 ~100 000 筆 `SPY` topic 訊息，以及 p50 / p95 / p99 延遲統計。其他 harness 模式：`--mode smoke`（3 個 topic + 1 根 bar）、`--mode noquote`、`--mode bars`（每個非 1m 的 `tf`，外加 `-5` 拒收路徑）、`--mode session`、`--mode multithread --threads 8 --per-thread 5000`。各 fixture 對應的 mode 與 frame 數列在 [`../contract/fixtures/README.md`](../contract/fixtures/README.md)。

**每次啟動都會先驗 ABI**：在任何 init 之前斷言 `EL_DllVersion() == 1`、且兩個墓碑都回 `-6`。只有想到才會跑的檢查，不算檢查。
