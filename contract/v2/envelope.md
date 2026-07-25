# wire v2 — envelope

> 權威來源：`cpp/src/ts2python.cpp`（`EL_PublishTick` / `EL_PublishTickEx`）。
> 本文如與實作不符，以實作為準並修正本文。
>
> v2 相對 v1 的唯一差異是新增 `seq` 與 `sid` 兩個欄位。本文自成一體，實作 v2 不需
> 回頭讀 [v1](../v1/envelope.md)。

## Transport

| 項目 | 值 |
| --- | --- |
| Pattern | ZeroMQ **PUB / SUB** |
| Publisher | DLL 端 `bind`，預設 `tcp://127.0.0.1:5555` |
| Subscriber | `connect` 同一 endpoint |
| 送達保證 | **無**，但 v2 起**可偵測**。見 §傳輸保證 |
| 對應 DLL ABI | `EL_DllVersion() == 7` |

## Frame 結構

每次 publish 送出 **2 個 frame**（`ZMQ_SNDMORE` 串接）：

| Frame | 內容 | 編碼 |
| --- | --- | --- |
| 1 | Topic = symbol 原字串 | UTF-8 bytes，無終止符 |
| 2 | Payload | UTF-8 JSON |

### Topic 規則

- Topic **就是 symbol 原字串**，含 `$` 前綴（`SPY`、`VXX`、`$TICK`、`$ADD`）。
- Subscriber 應以 `setsockopt(ZMQ_SUBSCRIBE, <symbol>)` **逐一精確訂閱**。
- **不要用 `SUBSCRIBE ""` 收全部。** ZMQ 訂閱是 prefix match，訂 `SPY` 也會收到
  `SPYG`。binding 必須在收訊後以 topic 字串**完全相等**再過濾一次。

## Payload — `kind: "tick"`

由 `EL_PublishTick` 產生。單筆成交/報價。

```json
{"v":2,"kind":"tick","seq":1,"sid":1784993521,
 "ts":1784993528.356531,"ts_utc":1776533445.000000,
 "ts_str":"2026-04/18-13:30:45","px":450.000000,"vol":100.000000,
 "bid":449.990000,"ask":450.010000,"tc":1}
```

## Payload — `kind: "bar_1m"`

由 `EL_PublishTickEx` 產生。已成形的分鐘 OHLC bar。

```json
{"v":2,"kind":"bar_1m","seq":3,"sid":1784993521,
 "ts":1784993528.356739,"ts_utc":1776533445.000000,
 "ts_str":"2026-04/18-13:30:45","o":450.100000,"h":450.750000,
 "l":449.800000,"c":450.400000,"vol":12000.000000,
 "bid":450.390000,"ask":450.410000,"tc":140}
```

## 欄位

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `v` | int | wire 版本。v2 固定為 `2` |
| `kind` | string | `"tick"` 或 `"bar_1m"` |
| **`seq`** | uint64 | **per-symbol** 單調遞增序號，從 `1` 起算。見 `../semantics.md` §6 |
| **`sid`** | uint64 | publisher session id —— `EL_Init` 當下的 UTC epoch 秒 |
| `ts` | float | DLL 收訊端 wall clock（UTC epoch，含亞秒）。**僅用於延遲量測** |
| `ts_utc` | float | `ts_str` 經 `std::chrono::zoned_time`（America/New_York）轉出的 UTC epoch。解析失敗為 `0.0` |
| `ts_str` | string | EL 原始字串 `yyyy-MM/dd-HH:mm:ss`（24 小時制），逐字透傳。EL 未傳時為 `""` |
| `px` | float | 成交價（僅 `tick`） |
| `o` `h` `l` `c` | float | OHLC（僅 `bar_1m`） |
| `vol` | float | 成交量。**注意是 float 不是 int** |
| `bid` `ask` | float | 買賣報價。**永遠是 float，從不為 `null`** —— 見 `../semantics.md` §3 |
| `tc` | float | tick count，以 `%.0f` 格式化（無小數點，但語意上是整數） |

### 所有數值欄位都是 double，`seq` / `sid` 除外

payload 由 `snprintf` 以 `%.6f`（`tc` 為 `%.0f`）組出。`seq` 與 `sid` 以 `%llu`
輸出，是**真正的無號整數**，不是浮點數。

JSON 沒有整數型別的概念，而 `seq` 可能超過 IEEE 754 double 能精確表示的
2^53。使用預設會把數字解析成 double 的 JSON 函式庫時（JavaScript、部分 Go
設定），binding **必須**確保 `seq` 以整數型別讀取，否則長時間執行後序號比較會失準。

## 邊界與限制

| 限制 | 值 | 後果 |
| --- | --- | --- |
| tick payload 緩衝區 | 544 bytes | 超出時 `EL_PublishTick` 回傳 `-4`，**不送出**（但序號已消耗，見下） |
| bar payload 緩衝區 | 608 bytes | 同上 |
| `ts_str` 未跳脫 | — | `ts_str` 直接插入 JSON 字串。binding **應對解析失敗有容錯**，不可假設 payload 必為合法 JSON |

## 傳輸保證

**v2 仍不保證送達，但遺漏變得可偵測。**

ZeroMQ PUB/SUB 是 fire-and-forget：

- Publisher 端 `SNDHWM = 100000`，超過即**靜默丟棄**（PUB 永不阻塞 publisher）。
- Subscriber 端預設 `RCVHWM = 1000`，超過即**靜默丟棄**。

publisher 不會、也無法回報這些丟棄。`seq` 是 subscriber 唯一能察覺的途徑。

### 序號在送出失敗時仍然消耗

publisher 在組裝 payload **之前**就取號。若後續 `snprintf` 截斷（回傳 `-4`）或
`zmq_send` 失敗（回傳 `-2`），該號碼不會出現在線上。

這是刻意的：**那筆資料確實遺失了**，讓它在 subscriber 端顯示為 gap 才誠實。
只在成功時遞增會讓真實遺漏被連續的序號掩蓋。

binding 端的相容義務見 [`../compat.md`](../compat.md)。
