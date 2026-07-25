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

DLL 不持有 symbol 分類知識（那在 `config/symbols.yaml`），且保持 wire 為原始透傳
可讓 binding 自行決定分類。代價就是**這條規則必須寫在契約裡**，否則只會活在某一個
binding 的原始碼中。

> 現況即為如此：reference binding 把清單硬編在
> `providers/tradestation_el.py` 的 `DEFAULT_INDEX_SYMBOLS`。本文件將其升格為契約。

---

## 4. Session 規則

| 規則 | 值 |
| --- | --- |
| US equity RTH | 09:30–16:00 **ET** |
| `session_open_utc` | 固定 09:30 ET，**不受 chart session 設定影響** |
| Session 歸屬 | 04:00 ET 之前的 bar 屬於**前一個** session |

### 4.1 Per-symbol 保留政策

由 `config/symbols.yaml` 的 `category` 決定預設，可逐 symbol 覆寫：

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

## 6. 規則新增準則

往本文件加規則的判準：**「換一種語言重寫 binding 時，會不會有人猜錯？」**

若答案是「會」，它就屬於這裡，而不是某個 binding 的原始碼註解。

每條新規則都必須：

1. 在 `fixtures/` 有對應情境
2. 在 `fixtures/expected/` 有語言中立的期望結果
3. 被至少一個 binding 的 conformance 測試實際消費
