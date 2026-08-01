# wire v4 — envelope

> 權威來源：`cpp/src/ts2python.cpp`（`EL_PublishTick` / `EL_PublishBar`）。
> 本文如與實作不符，以實作為準並修正本文。
>
> v4 相對 [v3](../v3/envelope.md) 的差異：新增 `pv`，宣告 publisher 實作的是哪一版
> 語意慣例。其餘欄位、形狀、映射規則與 v3 完全相同。本文自成一體。

## Transport

| 項目 | 值 |
| --- | --- |
| Pattern | ZeroMQ **PUB / SUB** |
| Publisher | DLL 端 `bind`，預設 `tcp://127.0.0.1:5555` |
| Subscriber | `connect` 同一 endpoint |
| 送達保證 | **無**，但可偵測（`seq`） |
| 對應 DLL ABI | `EL_DllVersion() == 9` |

## Frame 結構

2 個 frame（`ZMQ_SNDMORE` 串接）：frame 1 是 UTF-8 symbol topic，frame 2 是 UTF-8
JSON payload。Topic 規則同 v3：**逐一精確訂閱，並在收訊後以字串完全相等再過濾一次**
（ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`）。

## Payload — `kind: "tick"`

由 `EL_PublishTick` 產生。

```json
{"v":4,"pv":1,"kind":"tick","seq":1,"sid":1784998823,
 "ts":1784998835.554057,"ts_utc":1776533460.000000,
 "ts_str":"2026-04/18-13:31:00","px":812.000000,"vol":0.000000,
 "bid":null,"ask":null,"tc":1}
```

## Payload — `kind: "bar"`

由 `EL_PublishBar` 產生。

```json
{"v":4,"pv":1,"kind":"bar","tf":"1m","seq":3,"sid":1784998804,
 "ts":1784998816.189929,"ts_utc":1776533445.000000,
 "ts_str":"2026-04/18-13:30:45","o":450.100000,"h":450.750000,
 "l":449.800000,"c":450.400000,"vol":12000.000000,
 "bid":450.390000,"ask":450.410000,"tc":140}
```

## `pv` —— publisher 的語意慣例版本

`v` 說的是**這個信封長什麼樣**，`pv` 說的是**填進去的數字是依哪一版規則算出來的**。
兩者必須分開，因為它們由不同的東西決定、也不同步更新：

| | 由誰決定 | 何時改變 |
| --- | --- | --- |
| `v` | `cpp/` 的 DLL | 使用者換掉 `TS2Python.dll` |
| `pv` | `EL/` 的 indicator | 使用者重新匯入 `.ELD` |

**這不是假設性的區分。** DLL 與 indicator 都裝在使用者的 TradeStation 上，兩者可以
各自停留在不同版本，而 binding 對此無能為力。

### 取值

| `pv` | 意義 |
| ---: | --- |
| `0` | **未宣告。** publisher 走的是 `EL_Init`，代表 indicator 早於本欄位存在。依 [`../semantics.md`](../semantics.md) §3.4 的修正前慣例解讀：intraday 的 `vol` 是**上漲 tick 的成交股數**，約為實際成交量的一半 |
| `1` | intraday / daily 的 `Volume`–`Ticks` 對調已修正。`vol` 在每個 timeframe 都是總成交股數，intraday 的 `tc` 恆為 `0` —— 即 §3.4 的規則 1 與 2 |

`0` 不是錯誤碼，它是對「沒有宣告過的呼叫端」唯一為真的陳述。DLL 看不到慣例 ——
慣例是在 EasyLanguage 裡、由 indicator 讀哪個保留字決定的 —— 所以只能由呼叫端自報。

### 為什麼非得放在 wire 上

修正前後的 `vol` 差異是**一個看起來完全合理、只是持續偏小的數字**。它不會讓任何解析
失敗、不會觸發任何檢查，寫進 Parquet 之後與正確的值長得一模一樣。`pv` 是唯一能在
事後把兩者分開的東西。

reference binding 把它**落到磁碟**：`BAR_SCHEMA` / `TICK_SCHEMA` 都有一個 nullable 的
`publisher_version` 欄。三種值要分開讀：

| 磁碟上的值 | 意思 |
| --- | --- |
| `1` | wire 說了 `pv=1`，`volume` 是總成交股數 |
| `0` | wire 說了「未宣告」（v4 + 舊 indicator），`volume` 可能是上漲量 |
| `NULL` | 這一列寫在本欄位存在之前，或由 imputation 之類的工具生成 —— **無從得知** |

`0` 與 `NULL` 是兩件事，不可合併：前者是 publisher 明確表態，後者是根本沒人可以表態。

> **只修往後。** 既有分區一律 `NULL`，包含 §3.4 修正前後那一批 —— 它們當初就沒有任何
> 東西記錄自己是哪一種慣例，加欄位救不回來。這一欄保證的是「從此不再新增分不出來的
> 資料」。

### binding 的義務

- **必須讀 `pv`，且必須把 `0` 與 `1` 分開處理。** 把兩者一視同仁等於宣稱那兩批數字
  可以混用，而它們不可以。
- **`pv` 缺席（v1/v2/v3 的 payload）等同 `0`。** 那些版本的 publisher 一定早於本欄位。
- **不得因為 `pv` 未知就拒收整筆訊息。** 未知的高 `pv` 代表數值語意可能又變了，
  binding 應**記錄並照 `pv` 未知處理**，而不是丟棄資料 —— 資料本身沒有壞。
  這與未知的 `v`（必須拒收）不同：`v` 未知時連欄位怎麼擺都不知道。

## `kind` 表形狀，`tf` 表區間

同 v3，未變動：

| 欄位 | 語意 | 取值 |
| --- | --- | --- |
| `kind` | **形狀** —— 決定有哪些欄位 | `tick`（有 `px`）/ `bar`（有 `o` `h` `l` `c`） |
| `tf` | **區間** —— 僅 `bar` 有 | `1m` `5m` `15m` `30m` `1h` `1d` |

### `tf` 的字彙與儲存分區一致

`tf` 用的就是儲存層 `timeframe=` 分區的那組字串。**這是刻意的**：

> binding 若遇到無法對應的 `tf`，**必須拒收該 frame**，不得以預設值歸檔。
> 歸到預設分區會讓某個區間的 bar 混進另一個區間的分區，而下游無從分辨。

各區間的 bucket 對齊規則不同 —— 見 [`../semantics.md`](../semantics.md) §2.2。

## 欄位

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `v` | int | wire 版本，v4 固定為 `4` |
| **`pv`** | int | publisher 語意慣例版本。**恆存在**（未宣告時為 `0`）—— 見上節 |
| `kind` | string | `"tick"` 或 `"bar"` |
| `tf` | string | **僅 `bar` 有**。`1m` `5m` `15m` `30m` `1h` `1d` |
| `seq` | uint64 | per-symbol 單調遞增，從 `1` 起算。見 `../semantics.md` §6 |
| `sid` | uint64 | publisher session id（init 當下的 UTC epoch **微秒**）。只需比較是否相等，不得解讀為時間戳 |
| `ts` | float | DLL 收訊端 wall clock。**僅用於延遲量測** |
| `ts_utc` | float | `ts_str` 經 `zoned_time` 轉出。解析失敗為 `0.0`。**僅作交叉稽核** |
| `ts_str` | string | EL 原始字串 `yyyy-MM/dd-HH:mm:ss`，逐字透傳。**bar bucket 的權威來源**，但它是 bar 的**收盤**時間（EL 的 `Time`）—— binding 必須依 `semantics.md` §2 減一個 `tf` 才得到左標籤的 `bucket_start` |
| `px` | float | 成交價（僅 `tick`） |
| `o` `h` `l` `c` | float | OHLC（僅 `bar`） |
| `vol` | float | **總**成交股數，每個 timeframe 皆然 —— **前提是 `pv >= 1`**。**是 float 不是 int**。見 `../semantics.md` §3.4 |
| `bid` `ask` | float \| **null** | 無報價時為 `null` —— 見 `../semantics.md` §3.1 |
| `tc` | float | 以 `%.0f` 格式化。**只在 `1d` 上可能是筆數**（且存疑），intraday 恆為 `0`，binding 不得當筆數讀 —— 見 `../semantics.md` §3.4 |

`seq` / `sid` 是真正的無號 64 位元整數，`v` / `pv` 是小整數，其餘數值欄位皆為 double。
`seq` 可能超過 IEEE 754 double 能精確表示的 2^53，預設把 JSON 數字解析成 double 的
函式庫**必須**確保它以整數型別讀取。

## 區間映射由 DLL 負責

同 v3，未變動：

| BarType | BarInterval | `tf` |
| ---: | ---: | --- |
| 1（intraday） | 1 / 5 / 15 / 30 / 60 | `1m` `5m` `15m` `30m` `1h` |
| 2（daily） | **0 或 1** | `1d` |
| 其他 | — | **無**，`EL_PublishBar` 回傳 `-5` 且不送出 |

放在 C ABI 而非 EL，理由與報價正規化相同：只有一個實作，所有呼叫端自動一致。
**不猜測**——`BarType = 1` 涵蓋所有 intraday 分鐘圖，猜錯就是把 5 分鐘 bar 歸進
1 分鐘分區，而下游偵測不到。

日線的 `BarInterval` 兩個值都收：**TradeStation 10 實測回報 `0`**（SPY 日線圖的
EL log：`bar_type=2.00 bar_interval=0.00`），而 `1` 是 ABI 8 出廠時寫進本表的值 ——
DLL 裝在本 repo 看不到的機器上，兩個都得認。`2` 以上仍然拒絕：`BarType = 2` 的
interval 是「幾天一根」，收下去就是把 2 日線混進 `1d` 分區，而它長得跟真的日線
一模一樣。

## 兩個 init 匯出

| 匯出 | 簽章 | `pv` |
| --- | --- | ---: |
| `EL_Init` | `(const char* endpoint)` | `0` |
| `EL_Init2` | `(const char* endpoint, int publisher_version)` | 呼叫端指定 |

分成兩個匯出而非替 `EL_Init` 加參數：它們是 `__stdcall`，由被呼叫端清堆疊，所以舊
indicator 呼叫加寬後的 `EL_Init` 會破壞堆疊。兩者共用同一個初始化守衛，重複呼叫
（含交叉呼叫）一律回傳 `1`。

## 邊界與限制

| 限制 | 值 |
| --- | --- |
| tick payload 緩衝區 | 576 bytes；超出回傳 `-4` 且不送出（序號已消耗） |
| bar payload 緩衝區 | 672 bytes；同上 |
| `ts_str` 未跳脫 | 直接插入 JSON 字串；binding **應對解析失敗有容錯** |

## 傳輸保證

同 v3：ZeroMQ PUB/SUB 為 fire-and-forget，兩端都會靜默丟棄（`SNDHWM = 100000`、
`RCVHWM` 預設 1000）。`seq` 是 subscriber 唯一能察覺的途徑，且**序號在送出失敗時
仍然消耗** —— 那筆資料確實遺失，顯示為 gap 才誠實。

binding 端的相容義務見 [`../compat.md`](../compat.md)。
