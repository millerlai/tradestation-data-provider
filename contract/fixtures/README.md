# fixtures — conformance 樣本

每個 binding 都必須通過這裡的每一份 fixture。這是「多語言 subscriber」從口號變成
可驗證事實的地方。

## 檔案配對

| fixture | expected | 涵蓋 |
| --- | --- | --- |
| `smoke.jsonl` | `expected/smoke.json` | wire v2 · tick + bar_1m · per-symbol `seq` · index symbol 的 bid/ask 無效化 |
| `noquote.jsonl` | `expected/noquote.json` | 無報價情境 —— 歷史回放與 breadth symbol，wire 上為 `null` |
| `v1_legacy.jsonl` | `expected/v1_legacy.json` | wire v1 向下相容 · 無 `seq` 時的降級行為 · v1 用 `0` 表示無報價 |

`*.jsonl` 每行一個 frame：

```json
{"topic": "SPY", "payload": "{\"v\":2,\"kind\":\"tick\",...}"}
```

`payload` 是**收到當下的原文**，未經解析或正規化。無法以 UTF-8 解碼的 frame 改記在
`payload_invalid_utf8` 而非丟棄。

## 兩條規矩

### 1. fixture 必須錄製，不得手寫

用 [`../tools/record.py`](../tools/record.py) 搭配 `cpp` 的 `test_harness`（它不需要
TradeStation 就能驅動 DLL）：

```bash
# 終端機 A —— 留足夠暖機時間讓 subscriber 接上
cpp/build/x86-release/Release/TS2Python_TestHarness.exe --mode smoke --warmup-ms 8000

# 終端機 B
python contract/tools/record.py --count 6 --quiet --record contract/fixtures/smoke.jsonl
```

手寫的 fixture 只是把「我們以為 wire 長怎樣」寫第二遍，抓不到實作與規格的落差 ——
而那正是 fixture 存在的理由。

> `v1_legacy.jsonl` 也是照此原則：v1 的 DLL 原始碼從 git 取出（`67c5618^`）另行建置後
> 錄製，而非依 v1 規格手寫。

### 2. `expected/` 不得由任何 binding 產生

期望結果必須依 [`../semantics.md`](../semantics.md) 的規則**獨立推導**。用受測程式碼
產生期望值，只能證明它跟自己一致。

每份 `expected/*.json` 的 `derivation` 欄位須記錄推導方式。

## 新增 fixture

尚未涵蓋、但 `semantics.md` 已規範的情境：

- **DST 轉換日**的 `ts_str` → UTC（§1）—— 一年只錯兩天的那種 bug
- **session 首尾兩根 bar**，驗證左標籤（§2）
- **缺漏**：`seq` 跳號後的偵測行為（§6）

這幾項需要 harness 支援指定 symbol 與時間戳；目前 `--mode smoke` 的參數是寫死的。
