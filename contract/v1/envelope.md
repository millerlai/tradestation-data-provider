# wire v1 — envelope

> 權威來源：`cpp/src/ts2python.cpp`（`EL_PublishTick` / `EL_PublishTickEx`）。
> 本文如與實作不符，以實作為準並修正本文。

## Transport

| 項目 | 值 |
| --- | --- |
| Pattern | ZeroMQ **PUB / SUB** |
| Publisher | DLL 端 `bind`，預設 `tcp://127.0.0.1:5555` |
| Subscriber | `connect` 同一 endpoint |
| 送達保證 | **無。** 見下方「傳輸保證」 |

## Frame 結構

每次 publish 送出 **2 個 frame**（`ZMQ_SNDMORE` 串接）：

| Frame | 內容 | 編碼 |
| --- | --- | --- |
| 1 | Topic = symbol 原字串 | UTF-8 bytes，無終止符 |
| 2 | Payload | UTF-8 JSON |

Topic 獨立成 frame，是為了讓 subscriber 的訂閱過濾發生在 topic 上，payload 擴充
schema 時不影響訂閱行為。

### Topic 規則

- Topic **就是 symbol 原字串**，含 `$` 前綴（`SPY`、`VXX`、`$TICK`、`$ADD`）。
- Subscriber 應以 `setsockopt(ZMQ_SUBSCRIBE, <symbol>)` **逐一精確訂閱**。
- **不要用 `SUBSCRIBE ""` 收全部。** ZMQ 的訂閱是 prefix match，訂 `""` 會收到所有
  symbol；訂 `SPY` 也會收到 `SPYG`。若 symbol universe 中存在互為前綴的代號，
  binding 必須在收訊後以 topic 字串**完全相等**再過濾一次。
  （診斷工具如 `tools/record.py` 訂 `""` 是刻意為之，屬例外。）

## Payload — `kind: "tick"`

由 `EL_PublishTick` 產生。單筆成交/報價。

```json
{"v":1,"kind":"tick","ts":1747700000.123000,"ts_utc":1747700000.000000,
 "ts_str":"2026-04/18-13:30:45","px":450.000000,"vol":100.000000,
 "bid":449.990000,"ask":450.010000,"tc":1}
```

## Payload — `kind: "bar_1m"`

由 `EL_PublishTickEx` 產生。已成形的分鐘 OHLC bar。

```json
{"v":1,"kind":"bar_1m","ts":1747700060.123000,"ts_utc":1747700060.000000,
 "ts_str":"2026-04/18-13:31:00","o":450.100000,"h":450.750000,
 "l":449.800000,"c":450.400000,"vol":12000.000000,
 "bid":450.390000,"ask":450.410000,"tc":140}
```

## 欄位

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `v` | int | wire 版本。v1 固定為 `1` |
| `kind` | string | `"tick"` 或 `"bar_1m"` |
| `ts` | float | DLL 收訊端 wall clock（UTC epoch，含亞秒）。**僅用於延遲量測** |
| `ts_utc` | float | `ts_str` 經 `std::chrono::zoned_time`（America/New_York）轉出的 UTC epoch。解析失敗為 `0.0` |
| `ts_str` | string | EL 原始字串 `yyyy-MM/dd-HH:mm:ss`（24 小時制），逐字透傳。EL 未傳時為 `""` |
| `px` | float | 成交價（僅 `tick`） |
| `o` `h` `l` `c` | float | OHLC（僅 `bar_1m`） |
| `vol` | float | 成交量。**注意是 float 不是 int** |
| `bid` `ask` | float | 買賣報價。**永遠是 float，從不為 `null`** —— 見 `../semantics.md` |
| `tc` | float | tick count，以 `%.0f` 格式化（無小數點，但語意上是整數） |

### 所有數值欄位都是 double

payload 由 `snprintf` 以 `%.6f`（`tc` 為 `%.0f`）組出，**沒有整數型別**。
`vol` 與 `tc` 看起來像整數，但 binding 應以浮點數解析後再轉型，不可假設 JSON
parser 會給出整數。

## 邊界與限制

| 限制 | 值 | 後果 |
| --- | --- | --- |
| tick payload 緩衝區 | 448 bytes | 超出時 `EL_PublishTick` 回傳 `-4`，**不送出** |
| bar payload 緩衝區 | 512 bytes | 同上 |
| `ts_str` 未跳脫 | — | `ts_str` 直接插入 JSON 字串。若 EL 傳入含 `"` 或 `\` 的值會產生無效 JSON。實務上該值由 `FormatDate`/`FormatTime` 產生故不會發生，但 binding **應對解析失敗有容錯**，不可假設 payload 必為合法 JSON |

## 傳輸保證

**wire v1 不保證送達，且無法偵測缺漏。**

ZeroMQ PUB/SUB 是 fire-and-forget：

- Publisher 端 `SNDHWM = 100000`，超過即**靜默丟棄**（PUB 永不阻塞 publisher）。
- Subscriber 端預設 `RCVHWM = 1000`，超過即**靜默丟棄**；reference binding 調高至
  `1_000_000` 以降低機率，但無法消除。

payload 沒有序號，因此 **subscriber 無法知道自己漏收了資料**。

> wire v2 加入 `seq`（per-symbol 單調遞增）與 `sid`（publisher session id）以提供
> 缺漏偵測。見 `../compat.md`。
