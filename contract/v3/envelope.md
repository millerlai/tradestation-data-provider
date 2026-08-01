# wire v3 — envelope

> 權威來源：`cpp/src/ts2python.cpp`（`EL_PublishTick` / `EL_PublishBar`）。
> 本文如與實作不符，以實作為準並修正本文。
>
> v3 相對 [v2](../v2/envelope.md) 的差異：`kind` 收斂為 `tick` / `bar` 兩種**形狀**，
> 新增 `tf` 欄位表達**區間**。本文自成一體。

## Transport

| 項目 | 值 |
| --- | --- |
| Pattern | ZeroMQ **PUB / SUB** |
| Publisher | DLL 端 `bind`，預設 `tcp://127.0.0.1:5555` |
| Subscriber | `connect` 同一 endpoint |
| 送達保證 | **無**，但可偵測（`seq`） |
| 對應 DLL ABI | `EL_DllVersion() == 8` |

## Frame 結構

2 個 frame（`ZMQ_SNDMORE` 串接）：frame 1 是 UTF-8 symbol topic，frame 2 是 UTF-8
JSON payload。Topic 規則同 v2：**逐一精確訂閱，並在收訊後以字串完全相等再過濾一次**
（ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`）。

## Payload — `kind: "tick"`

由 `EL_PublishTick` 產生。

```json
{"v":3,"kind":"tick","seq":1,"sid":1784998823,
 "ts":1784998835.554057,"ts_utc":1776533460.000000,
 "ts_str":"2026-04/18-13:31:00","px":812.000000,"vol":0.000000,
 "bid":null,"ask":null,"tc":1}
```

## Payload — `kind: "bar"`

由 `EL_PublishBar` 產生。

```json
{"v":3,"kind":"bar","tf":"1m","seq":3,"sid":1784998804,
 "ts":1784998816.189929,"ts_utc":1776533445.000000,
 "ts_str":"2026-04/18-13:30:45","o":450.100000,"h":450.750000,
 "l":449.800000,"c":450.400000,"vol":12000.000000,
 "bid":450.390000,"ask":450.410000,"tc":140}
```

## `kind` 表形狀，`tf` 表區間

v1/v2 用 `kind: "bar_1m"` 把兩件事綁在同一個字串裡。v3 拆開：

| 欄位 | 語意 | 取值 |
| --- | --- | --- |
| `kind` | **形狀** —— 決定有哪些欄位 | `tick`（有 `px`）/ `bar`（有 `o` `h` `l` `c`） |
| `tf` | **區間** —— 僅 `bar` 有 | `1m` `5m` `15m` `30m` `1h` `1d` |

這樣新增區間不必動 binding 的分派邏輯，只是 `tf` 多一個值。

### `tf` 的字彙與儲存分區一致

`tf` 用的就是儲存層 `timeframe=` 分區的那組字串。**這是刻意的**：

> binding 若遇到無法對應的 `tf`，**必須拒收該 frame**，不得以預設值歸檔。
> 歸到預設分區會讓某個區間的 bar 混進另一個區間的分區，而下游無從分辨。

各區間的 bucket 對齊規則不同 —— 見 [`../semantics.md`](../semantics.md) §2.2。

## 欄位

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `v` | int | wire 版本，v3 固定為 `3` |
| `kind` | string | `"tick"` 或 `"bar"` |
| **`tf`** | string | **僅 `bar` 有**。`1m` `5m` `15m` `30m` `1h` `1d` |
| `seq` | uint64 | per-symbol 單調遞增，從 `1` 起算。見 `../semantics.md` §6 |
| `sid` | uint64 | publisher session id（`EL_Init` 當下的 UTC epoch **微秒**）。只需比較是否相等，不得解讀為時間戳 |
| `ts` | float | DLL 收訊端 wall clock。**僅用於延遲量測** |
| `ts_utc` | float | `ts_str` 經 `zoned_time` 轉出。解析失敗為 `0.0`。**僅作交叉稽核** |
| `ts_str` | string | EL 原始字串 `yyyy-MM/dd-HH:mm:ss`，逐字透傳。**bar bucket 的權威來源**，但它是 bar 的**收盤**時間（EL 的 `Time`）—— binding 必須依 `semantics.md` §2 減一個 `tf` 才得到左標籤的 `bucket_start` |
| `px` | float | 成交價（僅 `tick`） |
| `o` `h` `l` `c` | float | OHLC（僅 `bar`） |
| `vol` | float | **總**成交股數，每個 timeframe 皆然。**是 float 不是 int**。publisher 須依圖表型態選對 EL 保留字 —— 見 `../semantics.md` §3.4 |
| `bid` `ask` | float \| **null** | 無報價時為 `null` —— 見 `../semantics.md` §3.1 |
| `tc` | float | 以 `%.0f` 格式化。**只在 `1d` 上是筆數**，intraday 恆為 `0`，binding 不得當筆數讀 —— 見 `../semantics.md` §3.4 |

`seq` / `sid` 是真正的無號 64 位元整數，其餘數值欄位皆為 double。`seq` 可能超過
IEEE 754 double 能精確表示的 2^53，預設把 JSON 數字解析成 double 的函式庫**必須**
確保它以整數型別讀取。

## 區間映射由 DLL 負責

EasyLanguage 傳的是 `BarType` 與 `BarInterval` 兩個數字，`tf` 字串由 DLL 的
`wire_timeframe()` 映射：

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

## 邊界與限制

| 限制 | 值 |
| --- | --- |
| tick payload 緩衝區 | 544 bytes；超出回傳 `-4` 且不送出（序號已消耗） |
| bar payload 緩衝區 | 640 bytes；同上 |
| `ts_str` 未跳脫 | 直接插入 JSON 字串；binding **應對解析失敗有容錯** |

## 傳輸保證

同 v2：ZeroMQ PUB/SUB 為 fire-and-forget，兩端都會靜默丟棄（`SNDHWM = 100000`、
`RCVHWM` 預設 1000）。`seq` 是 subscriber 唯一能察覺的途徑，且**序號在送出失敗時
仍然消耗** —— 那筆資料確實遺失，顯示為 gap 才誠實。

binding 端的相容義務見 [`../compat.md`](../compat.md)。
