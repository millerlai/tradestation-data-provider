# wire — envelope（`proto` 2）

> 權威來源：`cpp/src/ts2python.cpp`（`EL_Publish`）。
> 本文如與實作不符，以實作為準並修正本文。
>
> 本文自成一體。**沒有舊版本可以相容** —— 理由見〈為什麼版本欄位叫 `proto` 而不是 `v`〉
> 與〈新舊部署不相容時會發生什麼〉。

## Transport

| 項目 | 值 |
| --- | --- |
| Pattern | ZeroMQ **XPUB / SUB** |
| Publisher | DLL 端 `bind`，預設 `tcp://127.0.0.1:5555` |
| Subscriber | `connect` 同一 endpoint |
| 送達保證 | **無**，但可偵測（`seq`）；「有沒有人在聽」則可確知 |
| 對應 DLL ABI | `EL_DllVersion() == 3` |

publisher 端是 **XPUB 而不是 PUB**。送出語意完全相同；差別是訂閱事件會以可讀訊息回到
publisher，所以 DLL 有辦法回答「到底有沒有人在聽」。這是 `EL_Init` 能夠在無人訂閱時
回 `-7` 並拒絕發布的唯一依據 —— PUB 在沒有訂閱者時靜默丟棄一切且不回報任何東西。

consumer 端仍然是普通的 `SUB`，不需要任何改動。

## Frame 結構

2 個 frame（`ZMQ_SNDMORE` 串接）：frame 1 是 UTF-8 topic，frame 2 是 UTF-8 JSON
payload。**逐一精確訂閱，並在收訊後以字串完全相等再過濾一次**
（ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`）—— 見 [`semantics.md`](semantics.md) §5。

topic 有兩種，**而 topic 本身就是 payload 形狀的鑑別子**：

| topic | payload | 產生者 |
| --- | --- | --- |
| symbol（`SPY`、`$VIX.X`…） | data point | `EL_Publish` |
| `__ts2py__`（固定） | chart 宣告（hello） | `EL_Init` |

沒有新增 `kind` 欄位，point frame 一個 byte 都沒變。

## Payload —— 只有一種

**一個 frame 形狀,不論來自什麼圖。** 沒有 `kind`,也沒有 `tf`。

```json
{
  "proto": 2,
  "seq": 1,
  "sid": 1785646054360588,
  "ts": 1785646062.364744,
  "ts_str": "2026-04/18-13:30:45",
  "bar_type": 0,
  "bar_interval": 1,
  "category": 2,
  "o": 450.0, "h": 450.0, "l": 450.0, "c": 450.0,
  "el_volume": 100, "el_ticks": 180,
  "el_upticks": 100, "el_downticks": 80, "el_open_interest": 80,
  "bid": 449.99, "ask": 450.01
}
```

| 欄位 | 型別 | 意義 |
| --- | --- | --- |
| `proto` | int | 協定版本。**目前只有 2**,缺這個鍵就不是這個協定 |
| `seq` | int | 每個 symbol 各自單調遞增。**每一個 frame 都必須有** |
| `sid` | int | publisher session id。DLL 重啟會變 —— 那是重置,不是遺漏 |
| `ts` | float | DLL 收訊端 wall clock（UTC epoch 秒）。量測延遲用,也是 `ts_str` 缺席時的最後手段 |
| `ts_str` | string | EL 的 `Date` + `Time`,`yyyy-MM/dd-HH:mm:ss`,ET 牆鐘,逐字。**`bar_time` 的權威來源**,原樣落地（`semantics.md` §2） |
| `bar_type` | int | EL 的 `BarType`,逐字。0 = tick 序列,1 = 盤中分鐘,2 = 日線 |
| `bar_interval` | int | EL 的 `BarInterval`,逐字。`bar_type` 為 1 時就是分鐘數 |
| `category` | int | EL 的 `Category`,逐字。0 期貨 / 2 股票 / 3 股票選擇權 / 4 指數 …（`semantics.md` §3.5） |
| `o` `h` `l` `c` | float | EL 的 `Open`/`High`/`Low`/`Close`。1-tick 序列上四者是同一筆成交 |
| `el_volume` `el_ticks` `el_upticks` `el_downticks` `el_open_interest` | int | EL 的五個保留字,逐字（`semantics.md` §3.4） |
| `bid` `ask` | float \| null | EL 的 `InsideBid` / `InsideAsk`,publisher 沒有報價時為 `null` |

### 為什麼不再分 tick 與 bar

wire 曾經有兩種形狀,用 `kind` 區分:tick 只送 `Close`、丟掉 `BarType`/`BarInterval`;
bar 送 OHLC、丟掉 `bid`/`ask`。兩邊都在**丟掉圖表已經提供的欄位**,依據是這個
publisher 自己對「哪些數字在哪種圖上有意義」的判斷 —— 而那個判斷發生在 wire 之外,
消費端看不出它做過。

TradeStation 對每一種圖都提供同一組保留字。1-tick 序列的 `Open = High = Low = Close`
是一個**事實**,值得落地,不是值得省略的冗餘。全部送出去也是唯一能撐過
「TradeStation 改變某個字的定義」的做法:這一層沒有會過時的意見。

### 為什麼 `bar_type` / `bar_interval` 不再映射成 `tf`

DLL 曾經把這一對映射成 `"5m"`、`"1d"` 之類的字串,並對**映射不出來的組合回 `-5`、
整根不送**。2 分鐘圖、週線圖、2 日圖因此完全不會出現在 wire 上。

現在原值直接上 wire,不映射也不拒收。落地的分區就是 `bartype={N}/interval={M}/`,
所以「這個 binding 沒有名字的間隔」不再等於「這筆資料不存在」。

## hello —— chart 宣告 frame

topic 固定為 **`__ts2py__`**。由 `EL_Init` 送出，每張圖一筆。

```json
{
  "proto": 2,
  "seq": 1,
  "sid": 1786079270794516,
  "ts": 1786079271.165191,
  "symbol": "$VIX.X",
  "category": 4,
  "bar_type": 1,
  "bar_interval": 1
}
```

| 欄位 | 型別 | 意義 |
| --- | --- | --- |
| `proto` | int | 恆為 `2`。point frame 沒有變，所以 wire 版本沒有變 |
| `seq` | int | 控制 topic 自己的序號，與各 symbol 的序號互不相干 |
| `sid` | int | publisher session id，與 point frame 同一個值 |
| `ts` | float | DLL 收到 `EL_Init` 呼叫的 UTC epoch 秒 |
| `symbol` | string | EL `GetSymbolName` |
| `category` | int | EL `Category`，逐字（§3.5 的表） |
| `bar_type` | int | EL `BarType`，逐字 |
| `bar_interval` | int | EL `BarInterval`，逐字 |

它**不是 data point**：沒有 OHLC、沒有五個量值、沒有報價。不要用 point 的 schema 去
驗它，也不要把它交給消費 point 的路徑。

### 為什麼是獨立 topic 而不是欄位

consumer 是**逐 symbol 訂閱**的，而且訂閱清單來自它自己的設定檔。所以一張掛在「設定檔
裡沒有的 symbol」上的圖，如果把 hello 發在該 symbol 的 topic 上，就**永遠不會被任何人
收到** —— 而那恰好正是 operator 最需要被告知的情況（圖開著、資料進不來、分區空著）。

固定 topic 解決這件事，而且順帶讓 point frame 完全不用動：鑑別子是 topic，不是新欄位。
`kind` 是這個 repo 已經移除過一次的東西，不會再加回來。

`__` 開頭是為了離開 TradeStation 的 symbol 空間 —— ZMQ 訂閱是 prefix match，一個可能
成為真實 symbol 前綴（或被真實 symbol 前綴）的 topic 會互相誤送。

### binding 的義務

1. **必須訂閱 `__ts2py__`，而且與 symbol 清單無關。** 這不是選配：publisher 在這個
   topic 上看不到訂閱者之前，`EL_Init` 回 `-7` 且**什麼都不發**。沒訂的 consumer 會讓
   每一張 TradeStation 圖無限期空轉。
2. **不得把 hello 當成 data point 交給下游。**
3. **收到 hello 時必須說話。** symbol 在訂閱清單內 → 記錄「開始接收」並附上四個欄位；
   不在清單內 → **警告**，並說明這張圖的資料收不到。兩者不可混為一談。
4. **一個壞掉的 hello 不得中斷串流。** 它不是市場資料，丟掉不損失任何一根 bar；讓例外
   逸出會殺掉 ingest 迴圈。
5. `symbol` 必須檢查是不是字串。JSON `null` 經 `str()` 會變成字串 `"None"`，然後以一個
   看起來像真 symbol 的名字被登記下來。

### 重播：consumer 重開不需要動 TradeStation

DLL 記住每一張呼叫過 `EL_Init` 的圖。**每收到一則涵蓋控制 topic 的訂閱訊息，就把所有
已知的圖重新宣告一次。**

這是必要的：`EL_Init` 一張圖只跑一次（在該圖的第一根 bar），TradeStation 不會為了一張
已經開著的圖再呼叫一次。沒有這個重播機制，consumer 重啟後就再也學不到工作區裡有什麼，
除非人工把每一張圖重新 Verify。

觸發條件是**訂閱訊息本身**，不是「訂閱者數由 0 變 1」。這一點是量出來的：consumer 重啟
時，新舊兩個訂閱者可能重疊幾毫秒，libzmq 因此從未看到該 topic 掉到零訂閱者 —— 用邊緣
判斷時，**重連的 consumer 一個 hello 都收不到**，而同一個測試隔六秒再連就兩個都收到。

DLL 因此設定 **`ZMQ_XPUB_VERBOSE`**。預設的 XPUB 每個 topic 只回報**第一個**訂閱者，
重疊的那一個會被當成重複訂閱吃掉。

代價是：一個 consumer 若同時訂閱兩個都涵蓋控制 topic 的 topic（例如 `""` 與
`__ts2py__`），會收到重複的 hello。重複的宣告在消費端是冪等的；漏掉的宣告則讓 consumer
對整個工作區一無所知。

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

- **`proto` 缺席或不等於 `2` → 拒收該 frame 並記錄。** 錯誤訊息必須點名可能的原因是
  「DLL 早於本協定」，並指出修法是同時更新 `TS2Python.dll` 與 `.ELD`。
- **五個 `el_*` 欄位一律以「必填」讀取。** 缺欄位必須拋錯，**不得**套用預設值 ——
  靜默寫 0 的成本遠高於解析失敗。
- **讀不了的 frame → 跳過並記錄，不得拋錯。** proto 2 沒有 `kind` 可以未知了；一個
  frame 現在只會因為缺必填欄位或型別不對而讀不了。壞一個 frame 不代表整條串流壞了 ——
  但它必須被算進 `frames_refused`，不能只是消失。

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

## 一個 publish 匯出,一個 init 匯出,兩個墓碑

| 匯出 | 簽章 | 用途 |
| --- | --- | --- |
| `EL_Init` | `(const char* endpoint, const char* symbol, int category, int bar_type, int bar_interval)` | **唯一的 init**;綁定 socket + 宣告這張圖 |
| `EL_Publish` | 16 個參數 | **唯一的 publish** |
| `EL_PublishTick` | 10 個參數 | **墓碑**,一律回 `-6` |
| `EL_PublishBar` | 13 個參數 | **墓碑**,一律回 `-6` |
| `EL_Shutdown` | `()` | |
| `EL_DllVersion` | `()` | 回 `3` |

`EL_Init2` 與 `EL_Init3` 已**刪除**。

### `EL_Init` 這個名字被收回重用 —— 這是一個已知且無法補救的風險

前一代的 `EL_Init` 是**單參數**的。現在是五參數。`DefineDLLFunc` 只按名字
`GetProcAddress`,所以舊 `.ELD` **解析得到這個匯出**;而 `__stdcall` 由被呼叫端清堆疊,
被呼叫端看不見呼叫端推了幾個參數 —— **堆疊直接損毀,TradeStation 崩潰或行為異常,
沒有任何回傳碼**。

以前擋住這件事的一直是 init:每一個 publish 呼叫都在「init 成功」的閘門後面
（`EL/TS2Python_Exporter.el`）,而 init 的名字每次改簽章時都跟著換
（`EL_Init` → `EL_Init2` → `EL_Init3`）,所以舊 `.ELD` 停在自己的 init。

**這一版放棄了那個保護。** 名字重用本身就是那個洞,程式碼補不起來。剩下的只有流程:
**DLL 與 `.ELD` 是同一個單位,換 DLL 就必須重新 Verify indicator。**
`cpp/install-to-tradestation.bat` 安裝完會這樣提醒。

`EL_PublishTick` / `EL_PublishBar` 的墓碑仍然留著,但要明白它們現在**擋不到任何東西**:
舊 `.ELD` 在 `EL_Init` 就已經把堆疊弄壞了,走不到這兩個名字。留著只是因為刪掉匯出只會
讓失敗更難讀。

**反方向（新 `.ELD` 打進舊 DLL）是安全的。** ABI-2 的 DLL 匯出的是 `EL_Init3`,沒有
五參數的 `EL_Init`,`DefineDLLFunc` 在 verify 階段解析失敗,什麼都不會跑 —— 可自行驗證:

```bash
git show 7faeabf:cpp/src/TS2Python.def   # ABI-1:有 EL_Init3,沒有 EL_Publish
git show 2a8033f:cpp/src/TS2Python.def   # ABI-2:EL_Init3 + EL_Publish
git show HEAD:cpp/src/TS2Python.def      # ABI-3:五參數 EL_Init,沒有 EL_Init3
```

### 新舊部署不相容時會發生什麼

| 情境 | 攔截點 | 使用者看到什麼 |
| --- | --- | --- |
| 新 `.ELD` + 舊（ABI-1/2）DLL | 舊 DLL 沒有五參數的 `EL_Init` 匯出 | `DefineDLLFunc` 解析 `EL_Init` 失敗，TradeStation 在 verify 階段就報錯 |
| 新 `.ELD` + 版本不符的新 DLL | indicator 的 `EL_DllVersion()` latch | Print Log 出現版本不符訊息，indicator 停止發布。`EL_DllVersion` 是 0 參數，簽章永不變，呼叫它絕對安全 |
| 舊 `.ELD`（呼叫 `EL_PublishTick`/`Bar`）+ 新 DLL | 墓碑回 `-6` | Print Log 出現 `rc=-6`，不發布 |
| **舊 `.ELD`（呼叫單參數 `EL_Init`）+ 新 DLL** | **無。名字解析得到，`__stdcall` 堆疊損毀** | **TradeStation 崩潰或行為異常。這是上面那節說的洞** |

前三個方向是可讀的失敗。**第四個不是**，而且它是這一版新開的 —— 唯一的防線是把 DLL 與
`.ELD` 一起換。

## 邊界與限制

| 限制 | 值 |
| --- | --- |
| tick payload 緩衝區 | 640 bytes；超出回傳 `-4` 且不送出（序號已消耗） |
| bar payload 緩衝區 | 768 bytes；同上 |
| hello payload 緩衝區 | 512 bytes；超出 `EL_Init` 回 `-4` |
| `ts_str` 未跳脫 | 直接插入 JSON 字串；binding **應對解析失敗有容錯** |
| hello 的 `symbol` 未跳脫 | 同上。TradeStation 的 symbol 不含 `"` 或 `\`，但 binding 不得假設 —— 壞掉的 hello 必須被拒收並計入 `frames_refused`，不得中斷串流 |

## 傳輸保證

ZeroMQ PUB/SUB 為 fire-and-forget，兩端都會靜默丟棄（`SNDHWM = 100000`、
`RCVHWM` 預設 1000）。`seq` 是 subscriber 唯一能察覺的途徑，且**序號在送出失敗時仍然
消耗** —— 那筆資料確實遺失，顯示為 gap 才誠實。

回傳碼見 [`error_codes.md`](error_codes.md)，schema 管不到的語意規則見
[`semantics.md`](semantics.md)。
