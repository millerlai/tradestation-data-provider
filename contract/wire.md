# wire — envelope（`proto` 1）

> 權威來源：`cpp/src/ts2python.cpp`（`EL_PublishTick` / `EL_PublishBar`）。
> 本文如與實作不符，以實作為準並修正本文。
>
> 本文自成一體。**沒有舊版本可以相容** —— 理由見〈為什麼版本欄位叫 `proto` 而不是 `v`〉
> 與〈新舊部署不相容時會發生什麼〉。

## Transport

| 項目 | 值 |
| --- | --- |
| Pattern | ZeroMQ **PUB / SUB** |
| Publisher | DLL 端 `bind`，預設 `tcp://127.0.0.1:5555` |
| Subscriber | `connect` 同一 endpoint |
| 送達保證 | **無**，但可偵測（`seq`） |
| 對應 DLL ABI | `EL_DllVersion() == 1` |

## Frame 結構

2 個 frame（`ZMQ_SNDMORE` 串接）：frame 1 是 UTF-8 symbol topic，frame 2 是 UTF-8
JSON payload。**逐一精確訂閱，並在收訊後以字串完全相等再過濾一次**
（ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`）—— 見 [`semantics.md`](semantics.md) §5。

## Payload — `kind: "tick"`

由 `EL_PublishTick` 產生（EasyLanguage 的 `BarType = 0`，tick 資料序列）。

```json
{"proto":1,"kind":"tick","seq":1,"sid":1784998823554057,
 "ts":1784998835.554057,"ts_str":"2026-04/18-13:31:00",
 "px":450.400000,
 "el_volume":300,"el_ticks":812,"el_upticks":300,"el_downticks":512,
 "el_open_interest":0,
 "bid":450.390000,"ask":450.410000}
```

## Payload — `kind: "bar"`

由 `EL_PublishBar` 產生（`BarType <> 0`）。

```json
{"proto":1,"kind":"bar","tf":"1m","seq":3,"sid":1784998804189929,
 "ts":1784998816.189929,"ts_str":"2026-04/18-13:30:45",
 "o":450.100000,"h":450.750000,"l":449.800000,"c":450.400000,
 "el_volume":6100,"el_ticks":12000,"el_upticks":6100,"el_downticks":5900,
 "el_open_interest":0}
```

**bar 不帶 `bid` / `ask`。** `InsideBid` / `InsideAsk` 是即時報價函式，不是 bar 的欄位；
一根 bar 上的報價其實只屬於它的最後一筆成交，掛在整根 bar 上是誤導。報價只出現在 tick。

## 欄位

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `proto` | int | 協定版本，固定 `1`。**缺席即非本協定** |
| `kind` | string | `"tick"`（有 `px`、`bid`、`ask`）或 `"bar"`（有 `o` `h` `l` `c`） |
| `tf` | string | **僅 `bar` 有**。`1m` `5m` `15m` `30m` `1h` `1d` |
| `seq` | uint64 | per-symbol 單調遞增，從 `1` 起算，tick 與 bar 共用。見 `semantics.md` §6 |
| `sid` | uint64 | publisher session id（init 當下的 UTC epoch **微秒**）。只需比較是否相等，**不得**解讀為時間戳 |
| `ts` | float | DLL 收訊端 wall clock，UTC epoch 秒。**Tick 事件時間的權威來源**；對 bar 而言僅供延遲量測 |
| `ts_str` | string | EL 原始字串 `yyyy-MM/dd-HH:mm:ss`，逐字透傳。**bar bucket 的權威來源**，但它是 bar 的**收盤**時間（EL 的 `Time`）—— binding 必須依 `semantics.md` §2 減一個 `tf` 才得到左標籤的 `bucket_start` |
| `px` | float | 成交價（僅 `tick`） |
| `o` `h` `l` `c` | float | OHLC（僅 `bar`） |
| `el_volume` | **int64** | EasyLanguage 的 `Volume`，**原樣** |
| `el_ticks` | **int64** | EasyLanguage 的 `Ticks`，**原樣** |
| `el_upticks` | **int64** | EasyLanguage 的 `UpTicks`，**原樣** |
| `el_downticks` | **int64** | EasyLanguage 的 `DownTicks`，**原樣** |
| `el_open_interest` | **int64** | EasyLanguage 的 `OpenInt`，**原樣**。股票 / ETF 恆為 0 |
| `bid` `ask` | float \| **null** | **僅 `tick`**。無報價時為 `null` —— 見 `semantics.md` §3.1 |

`seq` / `sid` 是真正的無號 64 位元整數，`proto` 是小整數，五個 `el_*` 是帶號 64 位元
整數，`ts` / `px` / OHLC / `bid` / `ask` 是 double。`seq` 可能超過 IEEE 754 double 能
精確表示的 2^53，預設把 JSON 數字解析成 double 的函式庫**必須**確保它以整數型別讀取。

### 五個 `el_*` 欄位：publisher 不做任何選擇

**欄位名就是 EasyLanguage 的保留字，值就是那個保留字當下的內容。** publisher 不判斷、
不換算、不依圖表型態挑選欄位。

這一點是本協定與前一代最大的差別。前一代的 wire 只有一個 `vol`，而 publisher 依
`BarType` 決定要把 EL 的 `Volume` 還是 `Ticks` 填進去 —— 因為 EasyLanguage 這兩個保留字
在 intraday 與 daily 上語意相反（見 [`semantics.md`](semantics.md) §3.4）。那個選擇發生在
wire 之外，數字看起來永遠合理，於是需要另一個版本欄位來宣告「這批數字是照哪一版規則
算的」。

**把五個保留字各給一欄，那個宣告就不需要存在了。** intraday 與 daily 的語意反轉仍然是
事實，但它現在是 consumer 查表就能解決的事，而不是 publisher 代為決定、事後無從追查的事。

`el_` 前綴是刻意的：看到 `el_volume` 的人會去查 EasyLanguage 的定義，看到 `volume` 的人
不會 —— 而在 intraday 上，EL 的 `Volume` **不是**成交量，是上漲 tick 的成交股數。

> **EasyLanguage 傳不了 int64。** `DefineDLLFunc` 沒有 64 位元整數型別，所以 EL → DLL 的
> 這五個參數仍是 `double`，由 DLL `static_cast<long long>` 後以 `%lld` 寫進 JSON。
> double 的 53-bit 尾數可精確表示到 9×10¹⁵ 股，遠超任何實際成交量。
> **不可改用 EL 的 `int`** —— 那是 32-bit，日成交量超過 21.4 億股的個股會溢位。

## `kind` 表形狀，`tf` 表區間

| 欄位 | 語意 | 取值 |
| --- | --- | --- |
| `kind` | **形狀** —— 決定有哪些欄位 | `tick`（有 `px` / `bid` / `ask`）/ `bar`（有 `o` `h` `l` `c`） |
| `tf` | **區間** —— 僅 `bar` 有 | `1m` `5m` `15m` `30m` `1h` `1d` |

`tf` 用的就是儲存層 `timeframe=` 分區的那組字串。**這是刻意的**：

> binding 若遇到無法對應的 `tf`，**必須拒收該 frame**，不得以預設值歸檔。
> 歸到預設分區會讓某個區間的 bar 混進另一個區間的分區，而下游無從分辨。

各區間的 bucket 對齊規則不同 —— 見 [`semantics.md`](semantics.md) §2.2。

## 區間映射由 DLL 負責

| BarType | BarInterval | `tf` |
| ---: | ---: | --- |
| 1（intraday） | 1 / 5 / 15 / 30 / 60 | `1m` `5m` `15m` `30m` `1h` |
| 2（daily） | **0 或 1** | `1d` |
| 其他 | — | **無**，`EL_PublishBar` 回傳 `-5` 且不送出 |

放在 C ABI 而非 EL，理由與報價正規化相同：只有一個實作，所有呼叫端自動一致。
**不猜測** —— `BarType = 1` 涵蓋所有 intraday 分鐘圖，猜錯就是把 5 分鐘 bar 歸進
1 分鐘分區，而下游偵測不到。

日線的 `BarInterval` 兩個值都收：**TradeStation 10 實測回報 `0`**（SPY 日線圖的
EL log：`bar_type=2.00 bar_interval=0.00`），而 `1` 是文件值 —— DLL 裝在本 repo 看不到的
機器上，兩個都得認。`2` 以上仍然拒絕：`BarType = 2` 的 interval 是「幾天一根」，收下去
就是把 2 日線混進 `1d` 分區，而它長得跟真的日線一模一樣。

## 為什麼版本欄位叫 `proto` 而不是 `v`

前一代的 wire 用 `"v"`，版本號一路走到 `4`。本協定是重寫，版本從 `1` 重新起算 ——
**如果沿用 `"v"`，`{"v":1}` 就會同時是本協定與前一代第一版的合法開頭。**

那個碰撞不會產生錯誤，會產生錯誤的資料：

- 前一代 v1 的 bar 用 `"kind":"bar_1m"`。新 binding 的版本閘門會**放行**（`v == 1`），
  然後在 `kind` 這一關判定為未知形狀 —— 依規約是「跳過並記錄」，於是**所有 bar 被靜默丟棄**。
- 前一代 v1 的 tick 用 `"kind":"tick"`，形狀相符，於是一路走到欄位讀取才發現沒有
  `el_volume`。若 binding 用「取不到就給 0」的寫法，磁碟上就會多出一批**全 0 的量值** ——
  一個完全合理、什麼都不會失敗的數字。

改一個欄位名就讓這整類問題在結構上不存在：**舊 payload 沒有 `proto` 這個 key**，所以
「版本相符」與「其實是舊資料」永遠不會同時成立。

### binding 的義務

- **`proto` 缺席或不等於 `1` → 拒收該 frame 並記錄。** 錯誤訊息必須點名可能的原因是
  「DLL 早於本協定」，並指出修法是同時更新 `TS2Python.dll` 與 `.ELD`。
- **五個 `el_*` 欄位一律以「必填」讀取。** 缺欄位必須拋錯，**不得**套用預設值 ——
  靜默寫 0 的成本遠高於解析失敗。
- **未知的 `kind` → 跳過並記錄，不得拋錯。** 形狀不認得不代表整條串流壞了。

## 兩個時間戳，不是三個

前一代的 wire 有第三個時間欄位 `ts_utc`：DLL 用 `std::chrono::zoned_time` 把 `ts_str`
解析成 UTC epoch 的結果。本協定移除它。**這是取捨，不是移除冗餘**，兩個代價要說清楚：

1. **失去一個偵測面。** binding 是用自己的時區資料庫解析 `ts_str` 的。`ts_utc` 與 `ts`
   的差距（前一代規定 > 5 秒就記錄警告）是唯一能發現「DLL 主機與 binding 主機的 tz
   database 不一致」的訊號。DST 轉換日的折疊時刻是這種不一致唯一會顯現的地方，一年兩天。
2. **`ts_str` 的可解析性不再於 publisher 端驗證。** 前一代的 DLL 解析失敗會送 `ts_utc: 0.0`，
   等於順手做了一次格式檢查。現在無效的時間字串會原樣送出，由 binding 發現 ——
   錯誤的發現點往後移了一層。**因為 binding 是唯一還能發現它的一層，
   [`semantics.md`](semantics.md) §1.1 規定它必須拒收該 frame，不得退回 `ts`。**

換到的是：wire 上只有一個權威時間來源（`ts_str`）與一個量測用時間（`ts`），沒有第三個
「存在但不得作權威用」的欄位需要每個 binding 各自記得別用。時間權威的完整規則見
[`semantics.md`](semantics.md) §1。

## 兩個 publish 匯出，一個 init 匯出，兩個墓碑

| 匯出 | 簽章 | 說明 |
| --- | --- | --- |
| `EL_Init3` | `(const char* endpoint)` | **唯一的 init** |
| `EL_Init` | `(const char* endpoint)` | **墓碑**，一律回 `-6` |
| `EL_Init2` | `(const char* endpoint, int)` | **墓碑**，一律回 `-6` |
| `EL_PublishTick` | 10 個參數 | tick |
| `EL_PublishBar` | 13 個參數 | bar |
| `EL_Shutdown` | `()` | |
| `EL_DllVersion` | `()` | 回 `1` |

`EL_PublishTick` 與 `EL_PublishBar` **沿用前一代的名字但簽章不同**。這兩個是
`__stdcall`（由被呼叫端清堆疊），所以簽章不符的呼叫**會損毀堆疊** —— 不是回傳錯誤碼，
是崩潰或隨機行為。

安全性由 init 保證，不是由名字保證：**EasyLanguage 端的每一個 publish 呼叫都在
「init 成功」的守衛之內**（`EL/TS2Python_Exporter.el`），init 失敗時 indicator 永遠不會
走到 publish。所以只要 init 攔得住，改過簽章的 publish 函式就一次都碰不到。

### 新舊部署不相容時會發生什麼

| 情境 | 攔截點 | 使用者看到什麼 |
| --- | --- | --- |
| 新 `.ELD` + 舊 DLL | 舊 DLL 沒有 `EL_Init3` 匯出 | `DefineDLLFunc` 解析失敗，TradeStation 在 verify 階段就報錯 |
| 新 `.ELD` + 版本不符的新 DLL | indicator 的 `EL_DllVersion()` latch | Print Log 出現版本不符訊息，indicator 停止發布。`EL_DllVersion` 是 0 參數，簽章永不變，呼叫它絕對安全 |
| 舊 `.ELD`（呼叫 `EL_Init`）+ 新 DLL | 墓碑回 `-6` | Print Log 出現 `EL_Init FAILED rc=-6`，indicator 因 init 未成功而不發布 |
| 舊 `.ELD`（呼叫 `EL_Init2`）+ 新 DLL | 墓碑回 `-6` | 同上 |

**四個方向都是可讀的失敗，沒有一個會走到堆疊損毀，也沒有一個會產出錯誤的資料。**
墓碑必須留在 `.def` 裡 —— 把匯出刪掉也能讓舊 `.ELD` 在解析階段失敗，但回 `-6` 能給
operator 一句看得懂的話。

## 邊界與限制

| 限制 | 值 |
| --- | --- |
| tick payload 緩衝區 | 640 bytes；超出回傳 `-4` 且不送出（序號已消耗） |
| bar payload 緩衝區 | 768 bytes；同上 |
| `ts_str` 未跳脫 | 直接插入 JSON 字串；binding **應對解析失敗有容錯** |

## 傳輸保證

ZeroMQ PUB/SUB 為 fire-and-forget，兩端都會靜默丟棄（`SNDHWM = 100000`、
`RCVHWM` 預設 1000）。`seq` 是 subscriber 唯一能察覺的途徑，且**序號在送出失敗時仍然
消耗** —— 那筆資料確實遺失，顯示為 gap 才誠實。

回傳碼見 [`error_codes.md`](error_codes.md)，schema 管不到的語意規則見
[`semantics.md`](semantics.md)。
