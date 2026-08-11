# `on_partial_bar`：發展中 bar 的即時回呼

日期：2026-08-11 · 範圍：`bindings/python/src/tradestation_data/runtime/ingestion.py`
基準：`b90e7a4`（`fix(cpp)!: stop the multi-chart init leak and restore the ABI gate`）

---

## Part 1 — 問題

使用 Python binding 的消費端目前只拿得到**已收盤**的 bar。一張 5 分鐘 K 線圖，
要等整整五分鐘才會收到第一個 event。

原因在 `IngestionRuntime._handle_provider_bar`（`ingestion.py:263`）：EL 的
「Update every tick」模式在一根 K 線內會重送同一個
`(symbol, bar_type, bar_interval, bar_time)` 數十次、每次 OHLC 更精細，而 runtime
只把最新一筆留在 `_current_direct_bars`，直到下列三者之一才呼叫 `_on_closed_bar`：

- 下一根的 `bar_time` 到達（`ingestion.py:342`）
- wall-clock 超過 `bar_time + 2s` grace（`ingestion.py:206`、`_DIRECT_BAR_CLOSE_GRACE`）
- shutdown drain（`ingestion.py:195`）

而 `_on_closed_bar`（`ingestion.py:352`）是 `MarketSnapshot`、`SinkPipeline`、
`on_bar` callback 三個出口的**唯一**餵食點。所以這三條路全都只看得到收盤 bar。

### 兩個左右設計的既有事實

**1. wire 上沒有「收盤」這個訊號。** DLL 每個 frame 都長一樣，`EL_Publish` 不區分
發展中與已完成。「partial vs closed」100% 是 binding 這一側的推論。因此 partial bar
不是一種新的**資料**，而是同一份資料的另一種**送出時機**。

**2. `Bar` 是 frozen dataclass，沒有任何欄位能分辨 partial 或 closed**
（`domain/bar.py:61-77`）。只要 partial 與 closed 走同一個出口，接收端就分不出來 ——
這正是 CLAUDE.md 點名的失敗模式（"a computed bar is indistinguishable from a
published one the moment it is persisted"）。設計的核心因此不是「怎麼送」，
而是**怎麼讓接收端不可能搞混**。

---

## Part 2 — 已定案的三個決策

| 決策 | 選擇 | 排除的選項與理由 |
|---|---|---|
| 送達範圍 | **只給 in-process callback** | 不進 `SinkPipeline`（要動 `Sink` Protocol，而 `sinks.yaml` 可指向任意第三方 `module:attr`）；不進 storage（與「binding 只收/標/存已發布的點」正面衝突） |
| 觸發頻率 | **每一幀都送，不節流不去重** | 與 tick chart 現行做法一致（`ingestion.py:283`："dedupe is the consumer's call"）。節流權留給消費端 |
| tick chart | **不觸發** | `bar_type == 0` 的每一筆本身就是完成的點，沒有 partial 可言。兩個 callback 語義互斥、不重疊 |

### 方案取捨

**A. 對稱的第二個 callback（採用）** — 新增 `on_partial_bar=` 建構參數與
`_on_partial_bar()` 方法，觸發點綁在 buffer 的賦值動作上。

**B. 在 `Bar` 上加 `is_partial` 欄位，共用 `on_bar`（否決）** — `Bar` 同時是
`BarWriter` 寫 Parquet 的 row 來源，加欄位就要面對「這欄要不要進 `BAR_SCHEMA`」，
兩個答案都有代價。更嚴重的是它**改變現有 `on_bar` 的語義**：所有既有消費端會
突然開始收到發展中的 bar，這是沉默的破壞性改動。

**C. 唯讀快照輪詢 `runtime.developing_bars()`（否決）** — 優點是使用者程式碼
不在 ingest 熱路徑上，慢的消費端不會回壓 ZMQ。但與「每一幀都送」直接衝突：
輪詢必然漏掉兩次輪詢之間的中間狀態。

選 A 的代價是明確的：**callback 寫得慢會拖垮 ingestion**。這一點寫進 docstring，
不靠使用者猜。

---

## Part 3 — 設計

### 3.1 公開介面

`ingestion.py:69-80` 的建構子加一個 keyword-only 參數，與現有 `on_bar=` 對稱：

```python
def __init__(
    self, provider, symbols, snapshot, sinks=None, *,
    on_bar: Callable[[Bar], None] | None = None,
    on_partial_bar: Callable[[Bar], None] | None = None,   # 新增
    heartbeat_interval: float = 60.0,
    ...
)
```

預設 `None`。不傳就完全沒有行為改變 —— 這是既有測試的回歸保護點。

**命名有一個必須避開的坑。** 既有寫法是參數 `on_bar` 存成屬性 `self._on_bar`，
而發射方法叫 `_on_closed_bar` —— 兩者不同名所以相安無事。partial 這邊若照抄，
參數 `on_partial_bar` 存成 `self._on_partial_bar` 會與同名的發射方法**互相覆蓋**。
因此屬性取名 `self._on_partial`：

```python
self._on_partial = on_partial_bar                 # 使用者的 callback
self._partial_callback_failure_logged = False     # §3.4 的 latch
```

### 3.2 觸發規則

一句話：**`_current_direct_bars[key]` 被設成這一幀時，觸發一次。**

`_handle_provider_bar` 裡剛好只有三個賦值點：

| 位置 | 情境 | 動作 |
|---|---|---|
| `ingestion.py:298` | 新桶的第一幀（`current is None`） | `_on_partial_bar(bar)` |
| `ingestion.py:338` | 桶內更新（`bar_time == current.bar_time`） | `_on_partial_bar(bar)` |
| `ingestion.py:343` | 換桶（`bar_time > current.bar_time`） | `_on_closed_bar(current)` **先**，`_on_partial_bar(bar)` **後** |

第三個的順序是刻意的：先送「上一根收了」，再送「這一根開始了」。反過來會讓
維護 bar 序列的消費端在收到收盤前就看到下一根，插錯位置。

**不觸發的四種情況**，全部靠現有邏輯自動排除，不需要新的判斷：

- tick chart —— `ingestion.py:285` 早已 `return`
- 歷史重播的舊桶 —— `ingestion.py:292` 的 `<= last_emitted` gate
- 亂序幀 —— `ingestion.py:349` 的分支不做賦值
- `_drain_direct_bars` / `_advance_direct_bars` —— 只從 buffer 移除並收盤，
  沒有新資料到達

這是選 A 的主要理由：partial 的邊界條件與 closed 的判斷共用同一套程式碼，
不會隨時間漂移。

**由此得到一個可測的不變量**：對 `bar_type != 0`，每一根收盤的 bar 必定至少
觸發過一次 partial。因為 bar 只能經由那三個賦值點進 buffer，而收盤只能從 buffer 出來。

### 3.3 partial 不碰什麼

- **`MarketSnapshot`** —— 不碰。snapshot 持有 session 歷史，
  `_evict_before_premarket_window` 這類邏輯對著同一根重複出現的 bar 運作會壞掉。
- **`SinkPipeline`** —— 不碰。型別上就到不了 Parquet。
- **`_counters.bars_out`** —— 不碰，那是收盤計數。

一個必須寫進 docstring 的後果：**同一根 bar 會先以 partial 出現 N 次、最後以
closed 出現一次，數值可能完全相同**。把同一個函式同時註冊到兩個 callback 會重複計算。

### 3.4 例外隔離

`_on_closed_bar` 現行寫法（`ingestion.py:356-360`）每次失敗都 `log.exception`。
一根 bar 一次沒問題；partial 一秒上百次，callback 一壞就把 log 淹掉。

同檔案已有先例：`_subminute_warned`（`ingestion.py:108-111`）為同一理由 latch，
註解寫得直白 —— "one line per chart is the signal — a line per print would bury it"。
照做：

```python
async def _on_partial_bar(self, bar: Bar) -> None:
    if self._on_partial is None:
        return
    self._counters.bars_partial_out += 1
    try:
        self._on_partial(bar)
    except Exception:
        self._counters.partial_callback_failed += 1
        # Latched: this fires per frame, not per bar. One traceback is the
        # signal; a traceback per print would bury every other log line.
        if not self._partial_callback_failure_logged:
            self._partial_callback_failure_logged = True
            log.exception(
                "on_partial_bar_callback_failed", extra={"symbol": bar.symbol}
            )
```

Latch 用單一 bool 而非 per-series —— 壞掉的是 callback 本身，不是某個 symbol。
**不**在連續失敗 N 次後停用 callback：那是替使用者決定他的程式該不該活著，
計數加一次 traceback 已足以讓問題可見。

`async def` 是為了與 `_on_closed_bar` 一致。內部沒有 `await`，所以不會讓出
event loop，行為與同步版完全相同。

### 3.5 Counters 與 heartbeat

`_Counters`（`ingestion.py:29-37`）加兩個欄位，`_emit_heartbeat`
（`ingestion.py:368-388`）的 `extra` 加兩個 key：

```python
bars_partial_out: int = 0          # on_partial_bar 實際送達次數
partial_callback_failed: int = 0   # 其中拋例外的次數
```

`bars_partial_out` 只在**有註冊 callback 時**累加 —— §3.4 的 early return 排在
累加之前。沒註冊時這個數字恆為 0，而不是「本來可以送幾次」。

**刻意不加 `partials_per_sec`。** `bars_per_sec` 需要 `last_report_bars` 那樣的
額外狀態，而累計值會不會往上跳已足以判斷這條通道是否活著；要量速率，
兩次 heartbeat 相減就有。

### 3.6 文件

- `IngestionRuntime` class docstring（`ingestion.py:41-67`）—— 補三條性質：
  不去重、不進 sink/storage、**在 ingest 熱路徑上同步跑**（callback 寫得慢會
  回壓 ZMQ recv 造成掉包，`messages_lost` 看得到）。這句話不能只活在本文件裡。
- `CLAUDE.md` 的 live ingest data-flow 圖 —— `_handle_provider_bar` 下面多一條
  partial 分支。
- **`contract/semantics.md` 不動。** wire 一個 byte 都沒變，DLL 也沒變。
  CLAUDE.md 說 "Rules that live only inside a binding are bugs"，此處的豁免理由是：
  `on_partial_bar` 不是 wire 語義，而是本 binding 對每個 binding 都已收到的資料
  所提供的投遞便利。另一個 binding 不實作它，語義不會有任何歧異，只是少個方便。
  **若**日後要求各 binding 的 partial 語義一致，那時才該寫進 contract。
- 待查：`bindings/python/README.md` / `README.zh-TW.md` 是否列出 callback 介面，
  有列就得補。

### 3.7 測試

全部走 `tests/test_ingestion_runtime.py` 現成的 `_handle_provider_bar` 直驅模式
（參見該檔 `:359` 一組），不需要新 harness：

1. `test_partial_fires_on_every_intra_bar_frame` —— 5 分鐘 chart 送 3 幀同
   `bar_time` 加 1 幀新 `bar_time`：partial 收到 4 次，closed 收到 1 次
2. `test_close_precedes_partial_on_rollover` —— 用單一 list 記錄兩個 callback
   的呼叫順序，斷言 `closed(old)` 排在 `partial(new)` 之前
   （原訂名 `test_partial_fires_before_close_on_rollover` 與它要斷言的順序相反，
   實作時改成現名）
3. `test_tick_chart_never_fires_partial` —— `bar_type=0` 送 3 幀：partial 0 次、
   closed 3 次
4. `test_partial_not_fired_for_stale_or_reordered_frames` —— 沿用該檔 `:377`
   既有的亂序案例，加斷言 partial 沒被呼叫
5. `test_every_closed_bar_was_partial_first` —— §3.2 的不變量，`bar_type != 0` 下
   `partial_count >= closed_count`
6. `test_partial_callback_exception_is_latched_and_swallowed` —— callback 每次都拋：
   ingestion 不中斷、`log.exception` 只出現一次、`partial_callback_failed` 等於幀數
7. `test_partial_never_reaches_sinks_or_snapshot` —— 註冊 `InMemorySink`，
   只送桶內更新不換桶，斷言 sink 與 snapshot 都是空的
8. 回歸：不傳 `on_partial_bar` 時，既有測試全數不變

第 7 個是最重要的那個 —— 它把「partial 不可能污染 storage」從設計意圖變成
被釘住的事實。

---

## Part 4 — 規模與風險

**規模。** `ingestion.py` 一個檔案：建構子 1 行、三個觸發點各 1 行、新方法約 12 行、
counters 2 行、heartbeat 2 行、docstring 若干。加上一個測試檔的 8 個測試，
與 CLAUDE.md 的圖。沒有新檔案、沒有新相依、沒有 Protocol 改動、沒有 wire 改動。

**風險。** 唯一的行為風險是使用者的 callback 在 ingest 熱路徑上執行：寫得慢會
回壓 ZMQ recv 造成掉包。這不是可以在 binding 內修掉的東西（見 Part 2 方案 C 的
取捨），只能靠 docstring 講清楚，並靠 `messages_lost` 讓後果可見。

**分支。** 本文件寫在 `fix/cpp-multichart-init-leak` 上，該分支的主題是 C++ 修復。
實作前應先切回 `main`、pull、另開分支（例如 `feat/partial-bar-callback`），
並把本文件一併帶過去。
