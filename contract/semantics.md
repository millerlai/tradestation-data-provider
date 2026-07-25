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

### 2.2 bucket 錨定於 session，且在 ET 時鐘上劃格

**bucket 的格線以 `America/New_York` 牆鐘時間鋪設，錨點是交易時段而非 Unix epoch。**

| timeframe | 錨點（ET） | 產生的邊界 |
| --- | --- | --- |
| `1m` `5m` `15m` `30m` `1h` | **09:30**（RTH 開盤） | …09:30, 09:35 … / 09:30, 10:30 … |
| `1d` | **04:00**（延長時段起點，見 §4） | 04:00 → 次日 04:00 |

#### 為何不能用 epoch 錨定

直接對 UTC 時間做 epoch 錨定的分桶（多數時序函式的預設），在短週期上**恰好正確**
—— ET 偏移是整小時，而 09:30 又是 30 分的倍數。但兩個長週期會錯：

| timeframe | epoch 錨定的後果 |
| --- | --- |
| `1h` | 產生 09:00 ET 桶，RTH 首根只涵蓋 09:30–10:00 —— **半根 bar 掛著整根的時間戳**，下游無從分辨 |
| `1d` | 依 UTC 午夜切分，而 UTC 午夜正是 **20:00 ET，延長時段的結束點**，盤後成交會被推到隔天 |

#### 為何在 ET 牆鐘上劃格，而非用固定 UTC 原點

若用固定的 UTC 原點，每年兩次 DST 轉換會讓格線相對交易時段**偏移一小時**。
在本地時間空間分桶後再轉回 UTC，格線就恆定錨在 09:30 / 04:00 ET。

兩個錨點都落在 DST 折疊區之外（轉換發生於 02:00 ET），所以邊界轉回 UTC 時
不存在時間模糊。

> 這條也是被實作與規格不一致逼出來的：reference binding 的 `Resampler` 原本用
> epoch 錨定，而 `1h` / `1d` 的行為從未被測試涵蓋。同時 `aggregation/session.py`
> 的 04:00 規則與重採樣的日界切分是**兩套不一致的時間概念** —— 現已統一。

---

## 3. bid / ask 何時無效

報價有**兩種**無效情形，來源不同，處理方式也不同。

### 3.1 沒有報價可報 → wire 上是 `null`（publisher 負責）

EL 傳的是 `InsideBid` / `InsideAsk`，那是**即時報價函式**。以下情況它們回傳 `0`：

| 情況 | 說明 |
| --- | --- |
| **歷史回放** | 圖表載入、任何非即時 bar。TradeStation 不在 live mode 時就沒有報價 |
| **本身無報價的 symbol** | breadth 指數（`$TICK`、`$ADD` …）從來就沒有買賣盤 |

wire v2 起，**DLL 會把非正值報價正規化為 JSON `null`**（`format_quote()`，
`!(v > 0.0)` 因此也涵蓋 NaN）。wire 自己說出「沒有報價」，binding 不需要記得
「`0` 代表無效」這種只活在文件裡、遲早被漏掉的規則。

> **wire v1 沒有這個保護。** v1 的 payload 一律是 `%.6f`，歷史回放會送出
> `"bid":0.000000`。讀 v1 的 binding **必須**自行把 `<= 0` 視為無效，否則會把
> `$0.00` 當成真實報價。`v1_legacy.jsonl` 就是為此存在。

正規化刻意放在 C++ 而非 EL：C ABI 只有一個實作，所有 EL 呼叫端自動一致；
放在 EL 則每支 script 都要各自記得。

### 3.2 有報價但不該採信 → binding 負責

即使 live mode 下報價非零，以下 symbol 的 `bid` / `ask` 仍**不具意義**，
binding 必須視為無效：

```
$TICK   $ADD   $VOLD   $TRIN   $PCVA   VXX
```

此清單為**預設值**，binding 應允許呼叫端覆寫（reference binding 的
`TradeStationELProvider(index_symbols=...)`）。

DLL 不做這一層，是因為它不持有 symbol 分類知識（那是 binding 的設定，
例如 `bindings/python/config/symbols.yaml`）。代價就是**這條規則必須寫在契約裡** ——
現況即為如此：reference binding 把清單硬編在
`bindings/python/src/tradestation_data/wire/el_subscriber.py` 的
`DEFAULT_INDEX_SYMBOLS`，本文件將其升格為契約。

### 3.3 綜合判定

binding 應把 `bid` / `ask` 視為無效，若**任一**成立：

1. 值為 `null`（v2 publisher 已判定沒有報價）
2. 值 `<= 0`（v1 相容，或任何未來的異常值）
3. symbol 在 index / breadth 清單中（§3.2）

### 3.4 成交量

`vol` 對 index / breadth symbol 同樣不具意義。`vol == 0` 時衍生的 VWAP 應為 null
而非除以零。

### 3.5 `Bar` 是否保留 bid / ask 由 binding 決定

wire 的 `bar_1m` 帶有 bar 收盤當下的 `bid` / `ask`。reference Python binding 的
`Bar` 型別**不保留**這兩個欄位，資料在此丟棄。

這是建模選擇而非解析錯誤 —— 保留的 binding 不算違規，丟棄的也不算。但 fixture 的
`expected/*.json` 一律記錄 wire 上的原值，讓選擇保留的 binding 有東西可比對。

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
