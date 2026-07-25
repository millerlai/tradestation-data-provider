# semantics — schema 管不到、但 binding 必須一致的規則

> **本文比 JSON Schema 重要。** Schema 只驗證欄位存在與型別；binding 之間真正會產生
> 分歧的是語意。只讀 schema 就動手寫的 binding，會產出「通過驗證但與其他 binding
> 不一致」的資料。
>
> 每一條規則都應有對應的 conformance fixture。

---

## 1. 時間權威

wire 上有三個時間戳，用途**不可互換**：

| 欄位 | 來源 | 正確用途 | 錯誤用途 |
| --- | --- | --- | --- |
| `ts` | DLL 收訊端 wall clock（UTC epoch） | **Tick 的事件時間**；延遲量測 | ❌ Bar 邊界 |
| `ts_utc` | `ts_str` 經 `zoned_time`（America/New_York）轉出 | **僅作交叉稽核** | ❌ 任何權威用途 |
| `ts_str` | EL 原始字串，逐字透傳 | **Bar `bucket_start` 的唯一權威來源** | ❌ Tick 時間 |

### 1.1 規則

- **Tick 的時間 = `ts`**（DLL 收訊端 UTC epoch）。
- **Bar 的 `bucket_start` = `ts_str` 解析結果**：以 `yyyy-MM/dd-HH:mm:ss` 格式、
  **`America/New_York` 時區**解析，再轉 UTC。
  - 必須用 IANA tz database 的 `America/New_York`，**不可用系統本地時區**，也不可用
    固定 UTC 偏移。DLL 主機的系統時區與此無關。
- `ts_utc` 僅用於交叉檢查：與 `ts` 差距 > **5 秒**時記錄警告，**不得拋錯或丟棄資料**。
  這種漂移幾乎都是 DST 表差異造成。
- `ts_str` 為 `""`（EL 未傳）或解析失敗時，`ts_utc` 為 `0.0`；binding 需有明確的
  降級行為並記錄。

> **為何 `ts_str` 而非 `ts_utc`？** `ts_utc` 是 DLL 主機算出來的；若該主機的 tz
> database 過期，DST 轉換日會算錯。`ts_str` 是原始事實，讓每個 binding 用自己的 tz
> database 解析，可驗證、可重算。

### 1.2 為何這條必須是契約級

若某個 binding 改用 `ts_utc` 作 bar 邊界，它與其他 binding 在 **DST 轉換日**會算出
不同的 bucket。平常看不出來，一年錯兩天。

---

## 2. Bar 用左標籤

`bucket_start` 表示區間 **`[t, t+step)`**（半開）。

US RTH 09:30–16:00 的 1m session 產生 **390** 根 bar：

```
bucket_start:  09:30, 09:31, …, 15:58, 15:59
                 ↑                        ↑
              第一根                   最後一根，涵蓋 [15:59, 16:00)
```

**不是** 09:31 … 16:00。

> 這條是被真實 bug 逼出來的：`verify_parquet.py` 的 `_expected_bars()` 曾產生右標籤
> 序列，導致完整 session 被誤判為缺漏。左/右標籤是市場資料最典型的靜默錯誤 ——
> 兩邊都「看起來對」，只差一根。
>
> **conformance fixture 必須涵蓋 session 首尾兩根 bar。**

### 2.1 `bucket_start` 必須向下取整到分鐘

解析 `ts_str` 得到 UTC 時間後，**秒與微秒一律歸零**。

`bar_1m` 的 bucket 依定義是 `[分鐘邊界, +1min)`。若原樣保留秒數，`17:30:45` 起算的
bucket 涵蓋 `[17:30:45, 17:31:45)`，那不是分鐘 bar，也無法與其他 bar 對齊。

EL 正常情況下送出的就是分鐘對齊的時間，所以取整通常是 no-op —— 但 **`test_harness`
的 `--mode smoke` 會把 tick 的 `13:30:45` 直接沿用給 `EL_PublishTickEx`**，
`smoke.jsonl` 因此正好涵蓋這個情境。

> 這條規則原本只存在於 reference binding 的實作裡（`_parse_el_str_as_et` 的
> `replace(second=0, microsecond=0)`），本文件漏寫。是 conformance 測試比對
> 手寫期望值時抓出來的 —— 照當時的規格實作，新 binding 會產出 `17:30:45Z`
> 而非 `17:30:00Z`，與 reference binding 不一致。

---

## 3. Index / breadth symbol 的 bid、ask 無效

> ⚠️ **這是目前最容易被新 binding 漏掉的規則。**

### 3.1 wire 上看不出來

`bid` / `ask` 在 wire 上**永遠是 float，從不為 `null`** —— payload 由 `snprintf`
以 `%.6f` 組出。對於 index / breadth symbol，DLL 會照樣送出 EL 傳進來的值
（通常是 `0.0` 或殘值），**它們不具意義**。

### 3.2 規則

以下 symbol 的 `bid` / `ask` 必須由 binding 視為**無效**（轉為該語言的 null / None /
`nil`），不可原樣傳遞給下游：

```
$TICK   $ADD   $VOLD   $TRIN   $PCVA   VXX
```

此清單為**預設值**，binding 應允許呼叫端覆寫（reference binding 的
`TradeStationELProvider(index_symbols=...)`）。

同理，`vol` 對這些 symbol 亦不具價格量意義；當 `vol == 0` 時衍生的 VWAP 應為 null
而非除以零。

### 3.3 為何不在 DLL 端處理

DLL 不持有 symbol 分類知識（那是 binding 的設定，例如 reference binding 的 `bindings/python/config/symbols.yaml`），且保持 wire 為原始透傳
可讓 binding 自行決定分類。代價就是**這條規則必須寫在契約裡**，否則只會活在某一個
binding 的原始碼中。

> 現況即為如此：reference binding 把清單硬編在
> `bindings/python/src/tradestation_data/wire/el_subscriber.py` 的
> `DEFAULT_INDEX_SYMBOLS`。本文件將其升格為契約。

---

## 4. Session 規則

| 規則 | 值 |
| --- | --- |
| US equity RTH | 09:30–16:00 **ET** |
| `session_open_utc` | 固定 09:30 ET，**不受 chart session 設定影響** |
| Session 歸屬 | 04:00 ET 之前的 bar 屬於**前一個** session |

### 4.1 Per-symbol 保留政策

由 binding 設定檔的 `category` 決定預設（reference binding 為
`bindings/python/config/symbols.yaml`），可逐 symbol 覆寫：

| category | session 重置 | 盤前保留 |
| --- | --- | --- |
| `breadth` | **每日重置**（09:30 ET 清空） | 無 |
| 其他（`etf` / `volatility` / `mega_cap`） | 不重置 | 預設 60 分鐘 |

> 這些是**市場規則**，不是某語言的實作細節。任何 binding 若自行詮釋，
> 產出的 session 邊界會與其他 binding 不一致。

---

## 5. Symbol 前綴衝突

ZMQ 的 `SUBSCRIBE` 是 **prefix match**：訂閱 `SPY` 也會收到 `SPYG` 的訊息。

binding 在收訊後**必須**以 topic 字串完全相等再過濾一次，不可假設訂閱本身已精確。

---

## 6. 序號與缺漏偵測（wire v2 起）

`seq` 只有在 binding 正確詮釋下才有意義。以下各條皆為**強制行為**。

### 6.1 per-symbol，且 tick 與 bar 共用

`seq` 的計數單位是 **symbol**，不是 (symbol, kind)。同一個 symbol 的 `tick` 與
`bar_1m` 交錯在同一條 topic 串流上，共用一個計數器才能偵測該串流的遺漏。

實測範例（`test_harness --mode smoke`，5 筆 tick 輪流三個 symbol + 1 根 SPY bar）：

| symbol | seq |
| --- | --- |
| SPY | 1, 2, **3**（第 3 筆是 `bar_1m`） |
| QQQ | 1, 2 |
| VXX | 1 |

之所以不用全域序號：subscriber 可能只訂閱一個 topic，全域序號的跳號會與它從未
訂閱的流量混淆，無法判斷自己是否漏收。

### 6.2 首次見到某 symbol → 建立基準，不得報告遺漏

中途加入的 subscriber 第一筆看到 `seq=21`，**不代表它遺失了 20 筆** —— 那些訊息
發送時它根本沒在聽。第一筆只用來建立期望值。

### 6.3 `sid` 變更 → 重置，不是遺漏

`sid` 不同代表 publisher 重啟、所有計數器歸零。此時必須清空狀態，否則會把
「重啟」誤報成數十億筆遺漏。

`EL_Init` 的冪等路徑（重複呼叫回傳 `1`）**不會**更新 `sid`，所以在 TradeStation
重新 Verify indicator 不會被誤判成重啟。

### 6.4 `seq < expected` → 不得回退期望值

TCP 保證單一 publisher 的順序，所以較小的序號是重複或重播，不是亂序。記錄它，
但**期望值必須維持不變** —— 否則下一筆正常訊息會被誤判成 gap。

### 6.5 序號在送出失敗時仍然消耗

publisher 在組裝 payload 前取號；後續截斷或送出失敗時該號不會上線。
**這是刻意的** —— 那筆資料確實遺失，顯示為 gap 才誠實。

### 6.6 `messages_lost` 為 0 的兩種含義

對 v1 publisher（無 `seq`），計數恆為 0。binding 必須讓使用者能區分：

- **「沒有遺失」**（v2，有偵測能力）
- **「無從得知」**（v1，沒有偵測能力）

這個差別在用該數字判斷某天的資料可不可信時是關鍵。

---

## 7. 規則新增準則

往本文件加規則的判準：**「換一種語言重寫 binding 時，會不會有人猜錯？」**

若答案是「會」，它就屬於這裡，而不是某個 binding 的原始碼註解。

每條新規則都必須：

1. 在 `fixtures/` 有對應情境
2. 在 `fixtures/expected/` 有語言中立的期望結果
3. 被至少一個 binding 的 conformance 測試實際消費
