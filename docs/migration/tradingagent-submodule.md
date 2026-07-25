# 遷移筆記 — TradeStation-TradingAgent 改用 submodule 引入 dp

> **本文只做記錄，不執行。** 消費端（TradeStation-TradingAgent，以下簡稱 TA）目前
> 維持現狀；等 dp 依 [`../architecture.md`](../architecture.md) 完成設計並發版後再動。
>
> 架構原則見 [`../architecture.md` §7](../architecture.md)。

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
| `aggregation/bar_aggregator.py` | 4 行，純 import 路徑 |

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
| `TA/EL/TS2Python_Exporter.el` | **搬到 dp**（architecture.md 提案 A）。TA 那份撤除。 |
| `TA/docs/design.md` §3.1–3.4 / §5 · `error_codes.md` | provider 相關段落搬到 dp。`dp/cpp/README.md:31,170` 目前引用 `../docs/design.md`、`../docs/error_codes.md`，兩者在 dp 中**不存在，是斷鏈**。 |
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

`scripts/` 下與 dp 同名的 10 支刪除，改呼叫 submodule 內的：

```
_common.py  aggregate_parquet.py  audit_bar_cache.py  clear_bar_cache.py
dedupe_bars.py  dump_parquet.py  imputation_parquet.py  run_ingestion.py
simple_sub.py  verify_parquet.py
```

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
| **(a) TA 改用 dp 的 SinkPipeline**（建議） | `tick_writer`/`bar_writer` → `ParquetTickSink`/`ParquetBarSink`；`strategy`/`broker`/`risk` 移出到 TA 自己的 orchestrator，透過 `on_bar` 掛入 | runtime 真正共用一份。dp 維持 data-collection-only。改動最大 |
| (b) dp 開放 hook 供注入 | dp 的 `IngestionRuntime` 加泛型 bar hook 鏈 | dp 要接受策略導向的抽象，違反 §1.3 Non-Goals |
| (c) 只共用 domain/aggregation/storage/providers | `runtime/` 不進 submodule | 改動最小，但 runtime 仍是兩份會各自 drift 的程式碼 |

**建議 (a)。** dp 的 `sinks/parquet.py` docstring 已寫明其設計意圖：

> "These are thin adapters over `BarWriter` / `TickWriter` … They exist so the
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

### D3 — sequence number（dp 側已決議實作）

dp 將於 **wire v2** 加入 `seq`（per-symbol 單調遞增）與 `sid`（publisher session id），
`EL_DllVersion()` 同步升至 **7**。見 [`../architecture.md` §4.3](../architecture.md)。

TA 側需配合的事項：

- 部署的 `TS2Python.dll` 需為 ABI 7，否則 subscriber 降級為不偵測缺漏（不會失敗，
  但會記錄警告）。`scripts/deploy_dll.py` 的部署驗證應檢查 `EL_DllVersion()`。
- TA 的 observability 層可消費 dp 暴露的 `messages_lost` 指標。
- 既有錄製的 Parquet 資料為 v1 產出、無序號，回測時無法回溯判斷當時是否有缺漏。

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
