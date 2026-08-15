# fixtures — conformance 樣本

每個 binding 都必須通過這裡的每一份 fixture。這是「多語言 subscriber」從口號變成
可驗證事實的地方。

## 檔案配對

**只有一組，沒有版本前綴。** 這個協定沒有需要相容的舊版本 —— 舊 DLL 送出的 payload
不帶 `proto` 欄位，binding 一律拒收，所以不存在「舊 fixture 仍是現役測試」的情形。
理由見 [`../wire.md`](../wire.md)。

| fixture | expected | 涵蓋 |
| --- | --- | --- |
| `smoke.jsonl` | `expected/smoke.json` | tick + bar · per-symbol `seq` · **真實報價一律存活，不依 symbol 名稱丟棄**（§3.3；`VXX` 帶著真實 bid/ask 上 wire，期望值原樣保留）· bucket 向下取整到分鐘（§2.1）· 時間戳原樣落地（§2） |
| `noquote.jsonl` | `expected/noquote.json` | 無報價 → wire 上為 `null`（§3.1）。含 **非指數 symbol**（SPY）的無報價 tick —— 只有 `$TICK` 的話，無法分辨「publisher 沒有報價」與「binding 自己丟掉了報價」 |
| `bars.jsonl` | `expected/bars.json` | 每一個 `BarType`/`BarInterval` 組合逐字上 wire · **沒有任何組合被拒收** —— 2 分鐘圖(1/2)、週線(3/1)、2 日(2/2) 以前會被 DLL 回 `-5` 整根不送 · `bar_type=2` 與盤中同一條規則:時間戳原樣落地(§2)
| `session.jsonl` | `expected/session.json` | session 首尾兩根 bar（§2）。**wire 送 EL 的收盤時間 `09:31` / `16:00`，期望值就是 `09:31` / `16:00`** —— 釘住「publisher 給什麼就存什麼」 |
| `hello.jsonl` | `expected/hello.json` | **chart 宣告**（`EL_InitChart`）走固定的 `__ts2py__` topic：topic 就是鑑別子、沒有 `kind`；一個已訂閱與一個未訂閱的 chart，釘住「必須說話、而且兩者不可混為一談」；控制 topic 有自己的 `seq` |

前四份是 point frame，`hello.jsonl` 不是 —— 它一根 bar 都沒有，`expected/hello.json`
的 `events` 是空陣列。它驗的是 [`../wire.md`](../wire.md) 那節列的五條 binding 義務。

前四份都涵蓋五個 `el_*` 量值原樣落地（§3.4）。harness 用的是實測到的 intraday 形狀：
`el_volume == el_upticks`、`el_ticks == el_upticks + el_downticks`、
`el_open_interest == el_downticks` —— 三條都是真實 intraday 資料的關係，binding 不得
「修正」它們。日線則是另一個形狀：`el_downticks == 0`，而股票日線的
`el_ticks == el_volume`（§3.4）。

> **這三條恆等式同時代表 fixture 守不住那三欄。** 兩個實作，一個讀 `el_ticks`、一個用
> `up + down` 算，在忠實的 wire 上產生一模一樣的數字。fixture 曾經刻意打破恆等式來換取
> 鑑別力，換到的是零 —— 換掉的卻是忠實性。§3.4 的「fixture 抓得到什麼」表有完整說明。

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

**三個程序，而且順序是固定的：hub → recorder → harness。**

hub 是新加的，而且**不是可選的**：DLL 現在是 `connect` 側（一張圖一個 `orchart.exe`，
`bind` 獨佔會讓除了第一個以外的圖全部拿到 `-3`），所以 harness 與 recorder 兩邊都在
connect，中間必須有人 bind。少了 hub，兩端誰也連不上誰，而且不會有任何錯誤 ——
harness 等到 `--subscriber-timeout-ms` 逾時後以 `-7` 退出，recorder 錄出一份空檔案。

recorder 仍然要排在 harness 之前：`EL_InitChart` 在控制 topic 看不到訂閱者之前回 `-7`
且什麼都不發。舊寫法（harness 先跑、`--warmup-ms 8000` 硬等）早就不成立。

```bash
# hub 先起來，recorder 次之，harness 最後
tradestation-data-hub --frontend tcp://127.0.0.1:5599 \
    --backend tcp://127.0.0.1:5600 &
sleep 2
python contract/tools/record.py --endpoint tcp://127.0.0.1:5600 \
    --count 8 --quiet --record contract/fixtures/smoke.jsonl &
sleep 3
cpp/Release/TS2Python_TestHarness.exe --mode smoke --endpoint tcp://127.0.0.1:5599
wait
```

**注意兩個 port 不一樣**：harness 打 hub 的**前台**（XSUB，5599），recorder 連 hub 的
**後台**（XPUB，5600）。兩邊寫成同一個 port 是最容易犯的錯，而它的症狀是靜默。

`record.py` 一定會訂閱 `__ts2py__`，即使命令列有指定 symbol 過濾。少了它 DLL 永遠不會
開始發布，而錄出來的會是一份空 fixture，沒有任何地方會報錯。

各 fixture 對應的 harness mode 與 frame 數：

| fixture | `--mode` | `--count`（重錄用） | 其中 hello | point frame |
| --- | --- | ---: | ---: | ---: |
| `smoke.jsonl` | `smoke` | 8 | 2 | 6 |
| `noquote.jsonl` | `noquote` | 5 | 2 | 3 |
| `bars.jsonl` | `bars` | 11 | 2 | 9 |
| `session.jsonl` | `session` | 4 | 2 | 2 |
| `hello.jsonl` | `smoke` | 2 | 2 | 0 |

> **`--count` 已含開頭那兩個 hello frame。** 每一個 mode 在進入 mode 本身之前都會先用
> `EL_InitChart` 宣告兩張圖（`SPY` 1/1 與 `QQQ` 1/5），而 `record.py` 是逐 frame 計數、不分
> topic。目前簽入的前四份 `*.jsonl` 只有 point frame（行數 6/3/9/2），因為它們錄製於
> hello 機制存在之前；照舊值重錄會在 mode 送完之前就收滿而截斷。

> `hello.jsonl` 用 `smoke` 錄，但只取前 2 個 frame —— harness 啟動時會宣告兩張圖
> （`SPY` 1/1 與 `QQQ` 1/5），它們一定排在任何 point frame 之前。
>
> **前四份 fixture 裡沒有 hello frame，這是刻意的。** 它們錄製於這個機制存在之前，而
> point frame 一個 byte 都沒變，所以它們仍然完全有效。若日後重錄任何一份，錄出來就會
> 多出開頭那兩個 `__ts2py__` frame —— 屆時 `expected/` 必須跟著手工重推，不得沿用。

> `bars` 的 9 個 frame 對應 9 種 `BarType`/`BarInterval` 組合 —— 包含 2 分鐘(1/2)、
> 週線(3/1)與 2 日(2/2)。**沒有任何組合被拒收**;`-5` 的映射拒收已隨 `tf` 一起移除。

> **錄製時要換掉預設 port，但理由已經不是 `-3`。** DLL 改成 connect 之後不會再因為
> 「port 被佔用」失敗（見 `contract/error_codes.md` 的 `-3` 一節）。現在的理由是：
> TradeStation 正在用的那個 hub 綁著 5555/5556，錄製用自己的一組 port（例如
> 5599/5600）才不會讓錄製流量與正式流量混在一起 —— 兩邊會互相收到對方的 frame，
> 錄出來的 fixture 就多了不該有的東西。不必關掉 TradeStation。

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
