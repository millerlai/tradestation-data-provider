# wire — envelope（`proto` 2）

> 權威來源：`cpp/src/ts2python.cpp`（`EL_Publish`）。
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
| 對應 DLL ABI | `EL_DllVersion() == 2` |

## Frame 結構

2 個 frame（`ZMQ_SNDMORE` 串接）：frame 1 是 UTF-8 symbol topic，frame 2 是 UTF-8
JSON payload。**逐一精確訂閱，並在收訊後以字串完全相等再過濾一次**
（ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`）—— 見 [`semantics.md`](semantics.md) §5。

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
  "el_volume": 100, "el_ticks": 195,
  "el_upticks": 100, "el_downticks": 80, "el_open_interest": 0,
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

## 一個 publish 匯出,一個 init 匯出,四個墓碑

| 匯出 | 簽章 | 用途 |
| --- | --- | --- |
| `EL_Init3` | `(const char* endpoint)` | **唯一的 init** |
| `EL_Publish` | 16 個參數 | **唯一的 publish** |
| `EL_Init` | `(const char* endpoint)` | **墓碑**,一律回 `-6` |
| `EL_Init2` | `(const char* endpoint, int)` | **墓碑**,一律回 `-6` |
| `EL_PublishTick` | 10 個參數 | **墓碑**,一律回 `-6` |
| `EL_PublishBar` | 13 個參數 | **墓碑**,一律回 `-6` |
| `EL_Shutdown` | `()` | |
| `EL_DllVersion` | `()` | 回 `2` |

`EL_PublishTick` 與 `EL_PublishBar` 曾經在改簽章時沿用名字。它們是 `__stdcall`
（由被呼叫端清堆疊）,所以簽章不符的呼叫**會弄壞堆疊** —— 不是回傳錯誤碼,是後續
無法預期的行為。這一次它們沒有再沿用:新的 publish 叫 `EL_Publish`,舊的兩個名字
留在 `.def` 裡當墓碑,舊 `.ELD` 打進來會拿到一個可讀的 `-6`,而不是崩潰。

**舊 `.ELD` 打進新 DLL** 這個方向由 init 擋住,而不是由名字擋住:EasyLanguage 端的每一個
publish 呼叫都在「init 成功」的閘門後面（`EL/TS2Python_Exporter.el`）,init 失敗時
indicator 永遠不會執行 publish。所以只要 init 的墓碑回負值,改過簽章的 publish 函式就
一次都碰不到。

**反方向（新 `.ELD` 打進舊 DLL）不是 init 擋的。** 前一代的 ABI-1 DLL **確實匯出了
`EL_Init3`**,而且簽章相同 —— 可自行驗證:

```bash
git show 7faeabf:cpp/src/TS2Python.def   # ABI-1:有 EL_Init3,沒有 EL_Publish
git show HEAD:cpp/src/TS2Python.def      # ABI-2:多了 EL_Publish
```

所以光靠 init 完全攔不到這個方向。ABI-1 缺的是 **`EL_Publish`**（`572b436` 才加入）,
`DefineDLLFunc` 會在解析 `EL_Publish` 時失敗;`EL_DllVersion()` latch 則負責涵蓋
「兩邊匯出都齊全但版本不符」的情況。

### 新舊部署不相容時會發生什麼

| 情境 | 攔截點 | 使用者看到什麼 |
| --- | --- | --- |
| 新 `.ELD` + 舊（ABI-1）DLL | 舊 DLL 沒有 **`EL_Publish`** 匯出（`EL_Init3` 反而解析得到） | `DefineDLLFunc` 解析 `EL_Publish` 失敗，TradeStation 在 verify 階段就報錯 |
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
