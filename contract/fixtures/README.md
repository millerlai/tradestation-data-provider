# fixtures — conformance 樣本

每個 binding 都必須通過這裡的每一份 fixture。這是「多語言 subscriber」從口號變成
可驗證事實的地方。

## 檔案配對

**只有一組，沒有版本前綴。** 這個協定沒有需要相容的舊版本 —— 舊 DLL 送出的 payload
不帶 `proto` 欄位，binding 一律拒收，所以不存在「舊 fixture 仍是現役測試」的情形。
理由見 [`../wire.md`](../wire.md)。

| fixture | expected | 涵蓋 |
| --- | --- | --- |
| `smoke.jsonl` | `expected/smoke.json` | tick + bar · per-symbol `seq` · index symbol 的 bid/ask 無效化（§3.2）· bucket 向下取整到分鐘（§2.1）· 時間戳原樣落地（§2） |
| `noquote.jsonl` | `expected/noquote.json` | 無報價 → wire 上為 `null`（§3.1）。含 **非 index symbol**（SPY）的無報價 tick —— `$TICK` 單獨無法區分 §3.1 與 §3.2 |
| `bars.jsonl` | `expected/bars.json` | 每一個 `BarType`/`BarInterval` 組合逐字上 wire · **沒有任何組合被拒收** —— 2 分鐘圖(1/2)、週線(3/1)、2 日(2/2) 以前會被 DLL 回 `-5` 整根不送 · `bar_type=2` 與盤中同一條規則:時間戳原樣落地(§2)
| `session.jsonl` | `expected/session.json` | session 首尾兩根 bar（§2）。**wire 送 EL 的收盤時間 `09:31` / `16:00`，期望值就是 `09:31` / `16:00`** —— 釘住「publisher 給什麼就存什麼」 |

四份都涵蓋五個 `el_*` 量值原樣落地（§3.4）。harness 用的是內部一致的 intraday 形狀：
`el_volume == el_upticks`、`el_ticks == el_upticks + el_downticks` —— 那是真實 intraday
資料的關係，binding 不得「修正」它。

`*.jsonl` 每行一個 frame：

```json
{"topic": "SPY", "payload": "{\"proto\":2,\"seq\":1,\"bar_type\":0,...}"}
```

`payload` 是**收到當下的原文**，未經解析或正規化。無法以 UTF-8 解碼的 frame 改記在
`payload_invalid_utf8` 而非丟棄。

## 兩條規矩

### 1. fixture 必須錄製，不得手寫

用 [`../tools/record.py`](../tools/record.py) 搭配 `cpp` 的 `test_harness`（它不需要
TradeStation 就能驅動 DLL）：

```bash
# 同一個 shell 呼叫，一前一後 —— PUB 會丟棄還沒有訂閱者時送出的訊息
cpp/Release/TS2Python_TestHarness.exe --mode smoke --warmup-ms 8000 &
python contract/tools/record.py --count 6 --quiet --record contract/fixtures/smoke.jsonl
wait
```

各 fixture 對應的 harness mode 與 frame 數：

| fixture | `--mode` | `--count` |
| --- | --- | ---: |
| `smoke.jsonl` | `smoke` | 6 |
| `noquote.jsonl` | `noquote` | 3 |
| `bars.jsonl` | `bars` | 6 |
| `session.jsonl` | `session` | 2 |

> `bars` 的 6 是 **9 次呼叫**的結果：6 個可對應的間隔各送出一個 frame，另外 3 次
> （2 分鐘、週線、2 日線）回 `-5` 且不送出。

> TradeStation 執行中時它已經佔用預設的 `tcp://127.0.0.1:5555`，init 會回 `-3`。
> 不必關掉 TradeStation，兩邊都指定同一個別的 port 即可。

手寫的 fixture 只是把「我們以為 wire 長怎樣」寫第二遍，抓不到實作與規格的落差 ——
而那正是 fixture 存在的理由。

### 2. `expected/` 不得由任何 binding 產生

期望結果必須依 [`../semantics.md`](../semantics.md) 的規則**獨立推導**。用受測程式碼
產生期望值，只能證明它跟自己一致。

每份 `expected/*.json` 的 `derivation` 欄位須記錄推導方式。

## 新增 fixture

尚未涵蓋、但 `semantics.md` 已規範的情境：

- **DST 轉換日**的 `ts_str` → UTC（§1）—— 一年只錯兩天的那種 bug。
  bucket 那一側已由 `bindings/python/tests/test_timeframe_grid.py` 逐點比對兩套實作，
  但 wire 上的 `ts_str` 解析仍無 fixture。
- **缺漏**：`seq` 跳號後的偵測行為（§6）。harness 目前無法刻意跳號。
- **`sid` 變更**：同一次錄製內的 publisher 重啟（§6.3）。

前兩項需要 harness 支援指定時間戳與人為跳號；`--mode bars` / `--mode session` 已示範
如何加一個參數寫死的新 mode，照著擴充即可。
