# 遷移筆記 — TradeStation-TradingAgent 改用 submodule 引入 dp

> **本文只做記錄，不執行。** 消費端（TradeStation-TradingAgent，以下簡稱 TA）目前
> 維持現狀；等 dp 依 [`../architecture.md`](../architecture.md) 完成設計並發版後再動。
>
> 架構原則見 [`../architecture.md` §7](../architecture.md)。

> ### ⚠ 2026-08-01 更新：dp 已完成 proto-1 重構，本文部分盤點已過時
>
> §1 的盤點做於 dp 還在 wire v2 / ABI 7 的時候。此後 dp 做了一次不相容的重寫：
> wire 版本欄位改為 `proto`（唯一值 1）、DLL ABI 歸 1，並且**刪除了所有衍生運算**
> —— `BarAggregator`、`Resampler`、`bar_coverage`、Tier-3 快取、`source` provenance
> 與 `publisher_version` 全部不再存在。dp 現在只做接收、標記時間、寫入。
>
> 受影響的段落已就地更正並標註。**§2 的遷移步驟與 §3 的待決策本身仍然成立**，
> 只是其中列舉的檔案清單要以 dp 現況為準。D3 已完成，改列為紀錄。
>
> 對 TA 最重要的一件事：**dp 不再從 tick 聚出 bar。** TA 若依賴那個行為，
> 遷移時必須自己實作，或改用 EL indicator 直接發布該間隔的 bar。

---

## 1. 現況盤點

TA 與 dp 目前是**同一套系統的兩份拷貝**，來自 dp 從 TA 抽離時的分家。

### 1.1 完全相同（可直接刪除 TA 那份）

`cpp/` 下 6 個關鍵檔案逐 byte 相同：

```
src/ts2python.cpp    include/ts2python.h    src/test_harness.cpp
src/TS2Python.def    CMakeLists.txt         vcpkg.json
```

`domain/` 4 個檔案（`bar.py` `tick.py` `order.py` `position.py`）逐 byte 相同。

### 1.2 僅套件名不同（`trading_agent.*` → `tradestation_data.*`）

| 檔案 | 差異 |
| --- | --- |
| `runtime/config.py` | 2 行，純 import 路徑 |
| `providers/base.py` | 4 行，純 import 路徑 |
| ~~`aggregation/bar_aggregator.py`~~ | 4 行，純 import 路徑。**dp 已刪除此模組**，TA 那份若還需要就變成 TA 自己的程式碼 |

### 1.3 dp 領先，尚未回流 TA

`providers/tradestation_el.py` — 19 行差異中 14 行是 dp 的 mypy strict 修正
（來自 dp commit `9e12e6e`）：

- `dict` → `dict[str, Any]`（`_parse_tick` / `_parse_bar`）
- `return  # type: ignore[unreachable]` + 6 行 concurrency false-positive 說明

**這些修正 submodule 化後自動生效，不需額外處理。**

### 1.4 真正分歧（需設計決策）

| 檔案 | dp | TA |
| --- | --- | --- |
| `runtime/ingestion.py` | 311 行，`sinks: SinkPipeline` | 424 行，`tick_writer`/`bar_writer` + `strategy`/`broker`/`risk_manager` |
| `runtime/main.py` | 346 行，`_build_pipeline()` 讀 `sinks.yaml` | 338 行，`HistoryStore` + `strategy.factory` |

TA 的 `IngestionRuntime` 多出 3 個方法：`_run_strategy_cycle`、`_call_on_bar`、
`_emit_new_fills`。

### 1.5 TA 獨有但屬 dp 職責的東西

| 項目 | 處置 |
| --- | --- |
| `TA/EL/TS2Python_Exporter.el` | **搬到 dp**（architecture.md 提案 A）。TA 那份撤除。**已完成** —— dp 的 `EL/` 就是那份，且已隨 proto 2 改寫。 |
| `TA/docs/design.md` §3.1–3.4 / §5 · `error_codes.md` | provider 相關段落搬到 dp。**已完成** —— 現在是 `dp/contract/{wire,semantics,error_codes}.md`；`cpp/README.md` 的斷鏈也已修掉。 |
| `TA/providers/tradestation_webapi.py` | 36 行 `NotImplementedError` stub。搬到 dp（屬 TradeStation 的第二種接入方式）。 |
| `TA/EL/monarch`（submodule） | **留在 TA**，是策略相關。 |

---

## 2. 遷移步驟

### 前提

- [ ] dp 完成 `contract/` 建置與 conformance suite
- [ ] dp 決定 `bindings/python/` 佈局是否採用（影響下方所有路徑）
- [ ] dp 發布版本 tag，供 TA pin

### Step 1 — 加入 submodule

```bash
cd TradeStation-TradingAgent
git submodule add git@github.com:millerlai/tradestation-data-provider.git \
    python/vendor/tradestation-data-provider
git -C python/vendor/tradestation-data-provider checkout <tag>
```

### Step 2 — 接上依賴

`python/pyproject.toml`：

```toml
[project]
dependencies = [
    "tradestation-data-provider",
    # ... 既有項目
]

[tool.uv.sources]
tradestation-data-provider = { path = "vendor/tradestation-data-provider", editable = true }
```

> 若採用 `bindings/python/` 佈局，path 改為
> `vendor/tradestation-data-provider/bindings/python`。

`pyzmq` / `polars` / `pyarrow` / `pyyaml` 由 dp 帶入，可從 TA 的直接依賴移除
（先確認 TA 沒有 dp 以外的使用點）。

### Step 3 — 刪除 TA 的重複拷貝

```
python/src/trading_agent/domain/          ← 全刪
python/src/trading_agent/aggregation/     ← 全刪
python/src/trading_agent/storage/         ← 全刪
python/src/trading_agent/providers/       ← 全刪
cpp/                                      ← 全刪（改用 submodule 的 cpp/）
EL/TS2Python_Exporter.el                  ← 刪（已搬 dp）；EL/monarch 保留
```

`scripts/` 下與 dp 同名的刪除，改呼叫 submodule 內的。dp 現在只剩 6 支：

```
_common.py  dedupe_bars.py  dump_parquet.py
imputation_parquet.py  run_ingestion.py  verify_parquet.py
```

> `aggregate_parquet.py` / `audit_bar_cache.py` / `clear_bar_cache.py` 已從 dp 刪除
> —— 它們全都在做衍生運算。TA 若仍需要那些功能，那是 TA 自己的程式碼。
> `imputation_parquet.py` 的語意也變了：`--output` 現在必填，寫到另一個 root、
> 用多一欄 `imputed: bool` 的獨立 schema，不再就地改寫。

TA 獨有的 6 支保留：`advisor_ui.py` `decision_inspector.py` `deploy_dll.py`
`llm_cost_report.py` `paper_daily_report.py` `r2_replay_compare.py`。

> `deploy_dll.py` 的 DLL 來源路徑需改指向 submodule 內的建置產物。

### Step 4 — 移除重複的 vcpkg submodule

TA 與 dp 目前各自宣告 `cpp/build-tools/vcpkg`，且**指向不同 commit**：

| repo | vcpkg commit |
| --- | --- |
| dp | `e5a4f54c` (2026.04.27-353) |
| TA | `256acc64` (2026.03.18-433) |

TA 刪除 `cpp/` 後，該 submodule 一併從 `.gitmodules` 移除，改由 dp 帶入（nested
submodule，clone 時需 `--recurse-submodules`）。

### Step 5 — 消費端自行定義契約

在 TA 建立自己的 Protocol，**不從 dp import**：

```python
# python/src/trading_agent/ports/market_data.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class MarketDataSource(Protocol):
    """TA 需要的訊號源形狀。dp 靠 structural typing 滿足，未來可整包替換。"""
    source_id: str
    async def connect(self) -> None: ...
    async def subscribe(self, symbols: list[str]) -> None: ...
    def events(self) -> AsyncIterator[MarketEvent]: ...
    async def close(self) -> None: ...
```

理由見 [`../architecture.md` §7.1](../architecture.md)。

### Step 6 — 改寫 import

TA 有 **259 個檔案**引用了資料層（`trading_agent.{domain,aggregation,storage,
providers,runtime}`，統計範圍含 `src/`、`tests/`、`scripts/`）。

```diff
- from trading_agent.domain.bar import Bar
+ from tradestation_data.domain.bar import Bar
```

作法見 §3 待決策 D2。

### Step 7 — 收斂 IngestionRuntime

見 §3 待決策 D1。

---

## 3. 待決策

### D1 — IngestionRuntime 如何收斂

| 選項 | 內容 | 評估 |
| --- | --- | --- |
| **(a) TA 改用 dp 的 SinkPipeline**（建議） | `tick_writer`/`bar_writer` → `ParquetBarSink`（tick sink 已隨 tick/bar 合併移除）；`strategy`/`broker`/`risk` 移出到 TA 自己的 orchestrator，透過 `on_bar` 掛入 | runtime 真正共用一份。dp 維持 data-collection-only。改動最大 |
| (b) dp 開放 hook 供注入 | dp 的 `IngestionRuntime` 加泛型 bar hook 鏈 | dp 要接受策略導向的抽象，違反 §1.3 Non-Goals |
| (c) 只共用 domain/aggregation/storage/providers | `runtime/` 不進 submodule | 改動最小，但 runtime 仍是兩份會各自 drift 的程式碼 |

**建議 (a)。** dp 的 `sinks/parquet.py` docstring 已寫明其設計意圖：

> "A thin adapter over `BarWriter` … It exists so the
> sink-driven runtime can keep doing **exactly what the old `tick_writer` /
> `bar_writer` parameters did**, just behind the `Sink` protocol."

也就是 dp 的 sink 重構本來就是為了從 TA 這種 writer 直連設計遷移過去而做的。

此外，(a) 對「將來換掉 dp」最有利：換 provider 時 TA 只需升 submodule pointer，
`strategy/`（68 檔）與其餘程式碼完全不動。(c) 則仍需改 TA 的 `runtime/`。

### D2 — 259 檔 import 如何處理

| 選項 | 內容 | 評估 |
| --- | --- | --- |
| **(a) 全面改名**（建議） | 259 檔機械改寫為 `tradestation_data.*` | 乾淨、無間接層、IDE 可直達 submodule 原始碼。diff 大但模式單一 |
| (b) shim 轉發 | TA 保留 `trading_agent/domain/__init__.py` 等薄殼 re-export | 改動極小，但多一層間接，mypy/IDE 跳轉停在 shim |
| (c) 分階段 | 先 shim 讓測試綠燈，再分批改名，最後拆 shim | 每步可獨立驗證與回滾，總工時最長 |

### D3 — sequence number（**已完成**，改列為紀錄）

`seq`（per-symbol 單調遞增）與 `sid`（publisher session id）已是 wire 的必備欄位。
原本寫的是「將於 wire v2 加入、ABI 升至 7」，那個版本序列後來被 proto-1 重寫取代：
現在是 **wire `proto` 2 / DLL ABI 2**，兩者都只有一個版本，更舊的一律拒收。

TA 側需配合的事項：

- 部署的 `TS2Python.dll` 需為 **ABI 2**，且 **`.ELD` 必須同時更新** —— indicator 綁
  `EL_Init3`，舊 DLL 沒有這個匯出。`scripts/deploy_dll.py` 的部署驗證應檢查
  `EL_DllVersion() == 1`。
- 不相容的組合不會降級，而是**明確拒收**：舊 payload 沒有 `proto` 欄位，binding 直接
  丟棄並記錄一則指名該重裝什麼的訊息。這與原本「降級為不偵測缺漏」的預期相反。
- 每個 proto-1 frame 都帶 `seq`，所以 `messages_lost` 不再有「無法判斷」這個狀態。
  TA 的 observability 層可以直接消費。
- 既有錄製的 Parquet 資料是舊 schema（`volume` / `tick_count` / `source` /
  `publisher_version`），與現在的 `el_*` 欄位**不相容**，且 intraday 的 `volume` 存的
  其實是上漲量。dp 刻意不提供 migration script —— 原因見 `CHANGELOG.md`。

---

## 4. 驗收

- [ ] `uv sync` 於 TA 成功，`tradestation-data-provider` 由 submodule 解析
- [ ] TA 既有測試（README 稱 363 tests）全數通過
- [ ] `mypy --strict` 在 TA 無新增錯誤
- [ ] `isinstance(provider, MarketDataSource)` 對 dp 的 provider 為 True
      （驗證 structural typing 契約成立）
- [ ] 全 repo 搜尋 `from tradestation_data` 在 TA 的 `strategy/` 下**零命中**
      （驗證策略層與 provider 解耦，將來可整包替換）
- [ ] 端到端：TradeStation 掛載 EL indicator → TA 錄到 Parquet，資料與遷移前一致
