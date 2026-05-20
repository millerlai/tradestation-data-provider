# cpp — TS2Python Bridge DLL

C++ DLL that bridges TradeStation EasyLanguage calls to a ZeroMQ PUB socket. Python subscribes over `tcp://127.0.0.1:5555` and receives tick messages.

## 要求

- **Target**: Win32 (x86) — TradeStation 是 32-bit process，**不可用 x64**（x64 config 僅為本機快速驗證 C++ 程式碼本身，絕不可部署到 TradeStation）
- **Compiler**: Visual Studio 2022 / 2026 (Community 以上)，含 "Desktop development with C++" workload
- **Dependencies**: `libzmq` + `cppzmq` — 由 `cpp/vcpkg.json` 以 manifest 模式管理

## 一次性準備 — 安裝 vcpkg

vcpkg 是 Microsoft 的 C/C++ 套件管理器。本專案用 **manifest mode**（`vcpkg.json`）宣告相依，Build 時 MSBuild 會呼叫 vcpkg 自動下載並編譯 `zeromq` + `cppzmq` 到 `cpp/vcpkg_installed/`。**沒 bootstrap vcpkg → 會 `LNK1104: 無法開啟檔案 'libzmq.lib'`**。

vcpkg 以 **git submodule** 形式釘在 `cpp/build-tools/vcpkg/`（commit 記錄在 `.gitmodules` 對應的 superproject 指標中），所以**每個 clone 都拿到完全相同的 vcpkg 版本**，ports 版本也可重現，不用自己挑一個全域安裝目錄。

### Step 1. Clone（含 submodule）

如果是**第一次 clone 這個 repo**：

```powershell
git clone --recurse-submodules <repo-url> TradeStation-TradingAgent
```

或已經 clone 過但當時沒帶 submodule：

```powershell
cd <your-repo-root>          # 例：D:\project\TradeStation-TradingAgent
git submodule update --init --recursive
```

> 以下指令一律以 repo 根目錄為起點；`<your-repo-root>` 請替換成你實際 clone 的位置。

完成後 `ls cpp\build-tools\vcpkg` 應該看到 `bootstrap-vcpkg.bat`、`ports\`、`scripts\` 等檔案。

### Step 2. Bootstrap（在 submodule 內產生 `vcpkg.exe`）

用**一般使用者權限的 PowerShell**（不需要系統管理員）：

```powershell
cd <your-repo-root>\cpp\build-tools\vcpkg
.\bootstrap-vcpkg.bat
```

跑完後 `ls vcpkg.exe` 應該存在（約 10 MB）。這支執行檔**不進 git**（被 `.gitignore` 忽略），每個 checkout 各自 bootstrap 一次即可。

### Step 3. 設 `VCPKG_ROOT` 環境變數（指向 submodule）

```powershell
setx VCPKG_ROOT <your-repo-root>\cpp\build-tools\vcpkg
```

> `setx` 寫入的是**永久**使用者環境變數，但**對目前這個 PowerShell 視窗無效**。  
> **關掉 PowerShell，重開一個新視窗**，然後 `echo $env:VCPKG_ROOT` 確認指向 submodule 路徑。

### Step 4. 整合進 Visual Studio

在**新開的 PowerShell**（`VCPKG_ROOT` 已生效）執行：

```powershell
& "$env:VCPKG_ROOT\vcpkg.exe" integrate install
```

這步是**全機生效、只要做一次**。它做的事：寫一個 `.targets` 到 `%LOCALAPPDATA%\vcpkg\`，指向**這個 submodule 裡的 `vcpkg.exe`**，讓 MSBuild 在 build 任何 `.vcxproj` 時自動注入 vcpkg 的 include / lib 路徑。

- **不需要管理員權限**（寫到 `%LOCALAPPDATA%`，是使用者層級）
- 成功訊息：`Applied user-wide integration for this vcpkg root.`
- 之後所有 VS / MSBuild 專案只要 `VcpkgEnabled=true` + 有 `vcpkg.json` 就會自動觸發 manifest install

> 如果這台機器有**其他專案也用 vcpkg submodule**，同一時間只有一個 `integrate install` 生效。切換專案時重跑 Step 3+4 指向該專案的 submodule 即可。  
> 要完全取消整合:`& "$env:VCPKG_ROOT\vcpkg.exe" integrate remove`

### Step 5. 驗證安裝成功

```powershell
cd <your-repo-root>\cpp
& "$env:VCPKG_ROOT\vcpkg.exe" install --triplet x86-windows
ls vcpkg_installed\x86-windows\lib\
```

預期看到版本化名稱的 lib 檔（如 `libzmq-mt-4_3_5.lib`）。vcxproj 靠 `VcpkgAutoLink=true` + `zmq.h` 裡的 `#pragma comment(lib, ...)` 自動 link，不用手動填 `<AdditionalDependencies>`。

### 升級 / 釘版 vcpkg

vcpkg 是 submodule，升級方式和一般 submodule 相同：

```powershell
cd cpp\build-tools\vcpkg
git fetch origin
git checkout <new-commit-or-tag>     # e.g. 2026.04.15 tag
cd ..\..\..
git add cpp/build-tools/vcpkg        # 更新 superproject 指標
git commit -m "bump vcpkg to 2026.04.15"
# 重 bootstrap（vcpkg.exe 可能有更新）
cd cpp\build-tools\vcpkg && .\bootstrap-vcpkg.bat
```

### 常見問題

- **`cpp/build-tools/vcpkg/` 是空的** — 忘記 `git submodule update --init --recursive`。
- **`'vcpkg.exe' 找不到`** — 沒做 Step 2 bootstrap，或 bootstrap 失敗（網路問題 / 防毒軟體擋）。重跑 `bootstrap-vcpkg.bat` 看詳細錯誤。
- **`LNK1104: 'libzmq.lib'`** — 通常是 (a) 沒做 Step 4 `integrate install`、(b) `VCPKG_ROOT` 在 VS 啟動時還沒生效（關掉 VS 全部視窗重開），或 (c) 手動在 vcxproj `<AdditionalDependencies>` 寫了 `libzmq.lib` 但 vcpkg 實際輸出的是版本化檔名（移掉讓 AutoLink 處理）。
- **第一次 Build 卡很久** — 正常。vcpkg 在下載並編譯 `zeromq` 源碼，x86-windows triplet 約 3-5 分鐘。之後會從 binary cache 秒讀。

## Build — 方式 A：Visual Studio 解決方案（推薦日常開發）

開 `cpp/TS2Python.sln`，選 **`Release | x86`**（部署給 TradeStation）或 Debug 任一，直接 Build。

- 第一次 Build 時 VS 會依 `vcpkg.json` 自動下載並編譯 `zeromq` + `cppzmq`，落地到 `cpp/vcpkg_installed/<triplet>/`
- 解決方案含兩個專案：
  - **TS2Python** — `src/ts2python.cpp` → `TS2Python.dll`（+ `TS2Python.lib` import lib）
  - **TS2Python_TestHarness** — `src/test_harness.cpp` → `TS2Python_TestHarness.exe`（透過 project reference 連結 TS2Python.lib）
- 輸出路徑：
  - Win32 Debug: `cpp/Debug/`
  - Win32 Release: `cpp/Release/`
  - x64 變體: `cpp/x64/<Debug|Release>/`
- vcpkg 自動把 `libzmq.dll` 複製到輸出資料夾（applocal deployment），harness 可直接執行

## Build — 方式 B：CMake + 命令列

```powershell
cd cpp
cmake --preset x86-release
cmake --build --preset x86-release
cmake --install build/x86-release --prefix build/x86-release/stage
```

輸出：`cpp/build/x86-release/stage/bin/TS2Python.dll`、`TS2Python_TestHarness.exe`、`libzmq.dll`

> 兩種方式可以並存，但不要混用：VS .sln 用 `cpp/Debug`、`cpp/Release` 做輸出；CMake 用 `cpp/build/`。如果切換工具鏈，建議先刪掉另一邊的 output 資料夾避免 stale DLL 被 linker 抓到。

## 部署到 TradeStation

1. Build **Release | x86**（TS 絕對是 32-bit process，x64 不會被載入）
2. 把下列檔案複製到 `C:\Program Files (x86)\TradeStation <version>\Program\`：
   - `TS2Python.dll`
   - `libzmq.dll`（vcpkg 從 `vcpkg_installed/x86-windows/bin/` 或 `stage/bin/` 取）
3. 用 `python scripts/deploy_dll.py` 可以一鍵複製並做 hash 比對
4. TradeStation EasyLanguage Editor 裡重新 Verify 使用這支 DLL 的 indicator

## 介面

見 [`../docs/design.md`](../docs/design.md) §3.2 / §5 和 [`../docs/error_codes.md`](../docs/error_codes.md)。

```c
int __stdcall EL_Init(const char* zmq_endpoint);
int __stdcall EL_PublishTick(
    const char* symbol,
    double timestamp, double price, double volume,
    double bid, double ask, double tick_count);
int __stdcall EL_Shutdown();
int __stdcall EL_DllVersion();
```

## 獨立測試

```powershell
# Terminal A — 先跑 subscriber
cd ..
python scripts/simple_sub.py --latency

# Terminal B — 跑 harness
cpp\Release\TS2Python_TestHarness.exe --mode stress --rate 10000 --seconds 10
```

Harness 退 0 表示沒掉訊。subscriber 端應看到 ~100000 筆 `SPY` topic 與 p50/p95/p99 延遲統計。
