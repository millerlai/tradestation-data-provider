# semantics — schema 管不到、但 binding 必須一致的規則

> **本文比 JSON Schema 重要。** Schema 只驗證欄位存在與型別；binding 之間真正會產生
> 分歧的是語意。只讀 schema 就動手寫的 binding，會產出「通過驗證但與其他 binding
> 不一致」的資料。
>
> 每一條規則都應有對應的 conformance fixture。

---

## 1. 時間權威

wire 上有兩個時間戳，用途**不可互換**：

| 欄位 | 來源 | 正確用途 | 錯誤用途 |
| --- | --- | --- | --- |
| `ts_str` | EL 原始字串，逐字透傳 | **`bar_time` 的唯一權威來源**（每一種圖都是） | ❌ 當成有秒級解析度 |
| `ts` | DLL 收訊端 wall clock（UTC epoch 秒） | 逐字落地為 `ts` 欄；延遲量測；**tick 圖上同一分鐘內各 frame 的唯一排序依據**；`ts_str` 缺席時的最後手段 | ❌ `bar_time` 的來源（缺席除外） |

**兩個都必須落地。** frame 只有一種形狀，`ts_str` 只有分鐘解析度 —— tick 圖
（`bar_type` 0）一分鐘內的每一筆成交共用同一個 `bar_time`，把它們分開的資訊只在
`ts` 裡。丟掉 `ts` 的 binding 存下的 tick 資料無法在分鐘內排序，而那正是舊協定
tick 的事件時間。

### 1.1 規則

- **`bar_time` = `ts_str` 解析結果**（每一種圖、每一個 frame 都是同一條規則）：以 `yyyy-MM/dd-HH:mm:ss` 格式、
  **`America/New_York` 時區**解析，再轉 UTC。
  - 必須用 IANA tz database 的 `America/New_York`，**不可用系統本地時區**，也不可用
    固定 UTC 偏移。DLL 主機的系統時區與此無關。
  - 解析出來的就是 bar 的時間,**原樣使用**。EL 的 `Time` 是收盤時間,所以
    `bar_time` 也是收盤時間 —— 不減、不對齊。要左緣標籤的消費端自己換算(§2)。
- **`ts_str` 缺席與解析失敗是兩種不同的狀態，binding 必須分開處理。**

  | 狀態 | 行為 | 為什麼 |
  | --- | --- | --- |
  | 欄位不存在或為 `""` | **允許**退回 `ts`（收訊時鐘），但**必須記錄一次** | publisher 宣告它沒有這個資訊。退回是誠實的降級 |
  | 欄位有值但解析不了 | **必須拒收整個 frame 並記錄**，不得退回 `ts` | publisher 送了壞資料。這時退回 `ts` 產生的不是降級，是**錯誤且看不出來的**資料 |

  拒收那條是被實際事故逼出來的。`ts` 是收訊時鐘，而歷史回放時整個 session 的 bar 在
  同一瞬間送達 —— 全部 floor 到同一分鐘、塌成同一個 `bar_time`，runtime 的去重
  再把它們收成一根。一個交易日變成一根「每個數字都合理」的 bar，落在今天的分區裡。
  zh-TW 主機的 `FormatTime("tt")` 就讓這件事真的發生過。

  **publisher 不再驗證這個字串** —— 舊協定的 DLL 會順手把它解析成 `ts_utc`，等於做了
  一次格式檢查；那個欄位已移除，所以格式錯誤現在一路到 binding 才會被發現。binding
  是唯一還能發現它的一層，所以它必須真的發現，而不是吞掉。

> **為何是 `ts_str` 而不是一個由 publisher 算好的 UTC 值？** 那種值是 DLL 主機算出來的；
> 若該主機的 tz database 過期，DST 轉換日會算錯，而且錯得沒有痕跡。`ts_str` 是原始事實，
> 讓每個 binding 用自己的 tz database 解析，可驗證、可重算。
>
> 代價是失去一個偵測面：舊協定用 `ts_utc` 與 `ts` 的差距（> 5 秒就警告）來發現「兩端
> tz database 不一致」。這個取捨記在 [`wire.md`](wire.md)。

### 1.2 為何這條必須是契約級

若某個 binding 改用收訊時間作 bar 邊界，它與其他 binding 在 **DST 轉換日**會算出
不同的 bucket。平常看不出來，一年錯兩天。

---

## 2. Bar 的時間就是 publisher 給的時間

**`bar_time` = `ts_str` 逐字解析的結果,不做任何位移或對齊。**

```
EL 的 Time  →  ts_str  →  以 America/New_York 解析 → 轉 UTC → 秒歸零  →  bar_time
```

EasyLanguage 的 `Time` 是 bar 的**收盤**時間,所以 `bar_time` 也是收盤時間。想要左緣
標籤(`[t, t+step)`)的消費端自己減 —— 那是消費端的事,不是這條傳輸鏈的事。

### 2.0 為什麼不在這裡換成左標籤

本文件曾經要求 binding 把右標籤換成左標籤:先減一分鐘,再對齊一條錨在 09:30 ET 的
格線。**那條規則已經移除,而且不可以再加回來。**

理由是它會**吃掉資料**,而且不會有任何錯誤浮現:

TradeStation 的盤中格線在 **RTH 開盤與收盤各重啟一次**。一張 session 設為
06:00–20:00 的 60 分鐘圖,一天發出 **15 根**,其中兩根是殘根:

```
06:00–07:00  07:00–08:00  08:00–09:00  09:00–09:30 ←殘根(盤前段尾)
09:30–10:30  10:30–11:30  11:30–12:30  12:30–13:30  13:30–14:30  14:30–15:30
15:30–16:00 ←殘根(RTH 段尾)
16:00–17:00  17:00–18:00  18:00–19:00  19:00–20:00
```

> **實測(2026-08-02,live SPY 2026-07-31)**:收盤 `09:00`(完整的 08:00–09:00)與
> 收盤 `09:30`(09:00–09:30 殘根)減一分鐘後對齊小時格線,**雙雙落在 08:30**。
> reference binding 的 `_handle_provider_bar` 把後到的視為 intra-bar 更新,整根
> 08:00–09:00 被半小時的數字取代 —— **15 根發出、14 根落地,每天如此**。磁碟上
> `el_volume == 128573` 的列數是 0。

**沒有任何格線能修好它。** 區段長度是 `[session起點, 09:30)`、`[09:30, 16:00)`、
`[16:00, session結束)`,而 session 起點是使用者的 chart 設定 —— **wire 上沒有這個
資訊,binding 永遠推算不出來**。上例三段是 210 / 390 / 240 分鐘,只有 60 不整除前
兩段,所以 5m / 15m / 30m 全部倖存、只有 1h 壞掉,壞了很久沒人發現。

publisher 送出的時間本來就是對的。這一層的工作是**不要弄壞它**。

### 2.0.1 DST 折疊時刻：wire 無法表達，binding 必須說出來

`ts_str` 是不帶偏移、也不帶 fold 位元的本地時間字串，所以在 DST 轉換日它**不足以
指出唯一的一個瞬間**：

| 情況 | 日期（2026） | 本地時間 | 意義 |
| --- | --- | --- | --- |
| 重複的一小時 | 11-01 | 01:00–02:00 ET | 同一個字串對應**兩個**相差一小時的瞬間 |
| 不存在的一小時 | 03-08 | 02:00–03:00 ET | 這個字串對應的瞬間**從未發生** |

**規範：取 fold=0（第一次出現），並且必須記錄一次。** 取 fold=0 不是因為它更正確，
而是因為 frame 裡沒有能判斷的資訊 —— 猜另一邊同樣沒有根據。真正不可接受的是**安靜地**
做這件事：兩種情況都會產出一個看起來完全正常的時間戳。

美股的延長盤是 04:00–20:00 ET，重複的那一小時落在外面，所以正常 equity 資料碰不到。
但 binding 不會拒收 session 以外的 bar，而 TradeStation 也提供 24 小時的 session
template，所以這條規則不能省。

> 這條是被真實 bug 逼出來的：`verify_parquet.py` 的 `_expected_bars()` 曾產生右標籤
> 序列，導致完整 session 被誤判為缺漏。左/右標籤是市場資料最典型的靜默錯誤 ——
> 兩邊都「看起來對」，只差一根。
>
> **conformance fixture 必須涵蓋 session 首尾兩根 bar，而且必須送 wire 的真實形狀
> ——右標籤的 `09:31` / `16:00`，不是 contract 自己的答案。** `session.jsonl` 一度
> 送 `09:30` / `15:59`，於是 fixture 因構造而與規格一致，這條規則等於沒被驗證，
> 右標籤的 bar 就這樣一路寫進了 Parquet。fixture 的職責是複現 publisher，不是
> 複述 spec。

### 2.1 `bar_time` 必須向下取整到分鐘

解析 `ts_str` 得到 UTC 時間後，**秒與微秒一律歸零**。

1 分鐘 bar 的 bucket 依定義是 `[分鐘邊界, +1min)`。若原樣保留秒數，`17:30:45` 起算的
bucket 涵蓋 `[17:30:45, 17:31:45)`，那不是分鐘 bar，也無法與其他 bar 對齊。

EL 正常情況下送出的就是分鐘對齊的時間，所以取整通常是 no-op —— 但 **`test_harness`
的 `--mode smoke` 會把 tick 的 `13:30:45` 直接沿用給 `EL_PublishBar`**，
`smoke.jsonl` 因此正好涵蓋這個情境。

> 這條規則原本只存在於 reference binding 的實作裡，本文件漏寫。是 conformance 測試
> 比對手寫期望值時抓出來的 —— 照當時的規格實作，新 binding 會產出 `17:30:45Z`
> 而非 `17:30:00Z`，與 reference binding 不一致。

### 2.3 讀取端的時區語意：對外一律 ET，UTC 只是內部絕對時間

儲存層兩份都留（`bar_time` UTC + `bar_time_et` ET），但那是**儲存**的事。
對呼叫端而言，這是一個美股的 API：規則時段、假日、盤前盤後、`date=` 分區全都以
`America/New_York` 定義，沒有人在做美股時心裡換算 UTC。

1. **讀取 API 收到的 naive datetime 一律解讀為 `America/New_York`。**
   帶時區的輸入照它自己的時區處理 —— 那沒有歧義，不需要預設值。
2. **UTC 只用於內部：** 絕對時間的儲存。這些都不該外洩成呼叫端
   必須自己換算的負擔。
3. **以「日」為單位的參數（CLI 的 `--start-date` 之類）指的是 ET 日曆日**，不是 UTC 午夜。

> 這條規則是補寫的，因為 reference binding 原本把 naive 當 UTC —— 純粹是查詢引擎
> session 設成 `UTC` 的副作用洩漏到 API 上，沒有人決定過。後果是
> `load_bars(sym, datetime(2026,4,20,9,30), ...)` 看起來像在問開盤，實際問的是
> 05:30 ET。查詢引擎要用什麼 session 時區是實作自由，**但不得因此改變 API 的語意**。

### 2.4 空區間的讀取語意

「這個 symbol 在這段時間沒有交易」是一個**正常的答案**，不是錯誤。休市日、盤中暫停、
冷門標的的盤前時段都會落在這裡。讀取面因此有兩條規則：

1. **空區間傳回 0 列，不得拋例外、不得傳回 0 欄位的空白 frame。**
   分區檔存不存在只能區分「從未錄過」，不能用來判斷區間內有沒有資料。

2. **同一個呼叫的空答案與非空答案，schema 必須完全一致**（欄位、順序、型別）。
   否則呼叫端跨 symbol 或跨日堆疊結果時，會在第一個沒交易的日子上炸開，而不是
   得到少一列的結果。注意各讀取路徑帶的 hive 欄位不同（單檔 `1d` 佈局帶
   `timeframe`，`date=` 佈局另帶 `date`），所以此處要求的是**與自己的非空答案一致**，
   而不是一份跨路徑的固定欄位表。

> reference binding 曾在這裡踩過兩次，兩次都是「空結果走了另一條程式路徑」：一次是
> 查詢引擎對空結果回傳無 schema 的 reader 而直接拋例外，回測問到一個沒動靜的日子就
> 中斷整個 run；修掉之後變成空答案比非空答案少一個欄位，崩潰的位置換了，呼叫端一樣
> 沒辦法統一處理。

---

## 3. bid / ask 何時無效

報價有**兩種**無效情形，來源不同，處理方式也不同。**兩者都只適用於 tick** ——
bar 不帶報價（見 [`wire.md`](wire.md)）。

### 3.1 沒有報價可報 → wire 上是 `null`（publisher 負責）

EL 傳的是 `InsideBid` / `InsideAsk`，那是**即時報價函式**。以下情況它們回傳 `0`：

| 情況 | 說明 |
| --- | --- |
| **歷史回放** | 圖表載入、任何非即時 bar。TradeStation 不在 live mode 時就沒有報價 |
| **本身無報價的 symbol** | breadth 指數（`$TICK`、`$ADD` …）從來就沒有買賣盤 |

**DLL 會把非正值報價正規化為 JSON `null`**（`format_quote()`，`!(v > 0.0)` 因此也涵蓋
NaN）。wire 自己說出「沒有報價」，binding 不需要記得「`0` 代表無效」這種只活在文件裡、
遲早被漏掉的規則。

正規化刻意放在 C++ 而非 EL：C ABI 只有一個實作，所有 EL 呼叫端自動一致；
放在 EL 則每支 script 都要各自記得。

### 3.2 有報價但不該採信 → binding 負責

即使 live mode 下報價非零，以下 symbol 的 `bid` / `ask` 仍**不具意義**，
binding 必須視為無效：

```
$TICK   $ADD   $VOLD   $TRIN   $PCVA   VXX
```

此清單為**預設值**，binding 應允許呼叫端覆寫（reference binding 的
`TradeStationELProvider(index_symbols=...)`）。

DLL 不做這一層，是因為它不持有 symbol 分類知識（那是 binding 的設定，
例如 `bindings/python/config/symbols.yaml`）。代價就是**這條規則必須寫在契約裡**。

### 3.3 綜合判定

binding 應把 `bid` / `ask` 視為無效，若**任一**成立：

1. 值為 `null`（publisher 已判定沒有報價）
2. 值 `<= 0`（任何未來的異常值）
3. symbol 在 index / breadth 清單中（§3.2）

### 3.4 五個 `el_*` 量值欄位

wire 帶五個量值，**每一個都是 EasyLanguage 同名保留字的原值**：`el_volume`、
`el_ticks`、`el_upticks`、`el_downticks`、`el_open_interest`。

#### publisher 不做任何選擇 —— 這是刻意的

TradeStation 官方定義（股票商品，[EL 保留字文件][elvol]）：

| EL 保留字 | **intraday**（分鐘 / tick / volume bar） | **daily 以上** |
| --- | --- | --- |
| `Volume` | **只有上漲 tick 的成交股數／口數** | 總成交股數／口數 |
| `Ticks` | **總成交股數／口數** | 總 tick 數 **或「Volume 與 Open Interest 之和」** |
| `UpTicks` | 上漲 tick 的成交股數／口數 | 總成交股數／口數 |
| `DownTicks` | 下跌 tick 的成交股數／口數 | **0**（股票／外匯）／ **Open Interest**（期貨） |
| `OpenInt` | **0**（股票／外匯）／ **下跌 tick 的量**（期貨） | **Open Interest**（期貨）／ 其餘為 0 |

以上五列逐字取自 TradeStation 官方頁面 [EasyLanguage words related to Ticks, Volume &
Open Interest][elvol]。**不要再去別處推測這五個字的語意,也不要重問** —— 那一頁是唯一
權威,而它已經被抄進這張表。

**互換有兩組,不是一組。** `Volume` / `Ticks` 是眾所周知的那一組;`DownTicks` /
`OpenInt` 是第二組,而且方向相反 —— intraday 時 `OpenInt` 借用 `DownTicks` 的意義
(僅期貨),daily 時 `DownTicks` 借用 `OpenInt` 的意義(僅期貨)。**本文件曾經只寫
第一組**,第二組是 2026-08-02 對照原頁補上的。

> **實測(2026-08-02,live SPY / @ES / VXX / SPY 選擇權,盤中圖)**
>
> | 商品 | `Category` | `openint` | `downticks` | 與上表 |
> | --- | --- | --- | --- | --- |
> | `@ES` 期貨 | 0 | 1,370 | 1,370 | **相符** —— 期貨盤中 `OpenInt` 官方定義就是下跌 tick 的量 |
> | `SPY` 股票 | 2 | 33,109 | 33,109 | **不符** —— 表上寫 0,實際回傳 `DownTicks` |
> | `VXX` 股票 | 2 | 582,719 | 582,719 | 同上 |
> | `SPY 選擇權` | 3 | 158 | 158 | 同上 |
>
> 亦即 **`OpenInt` 在盤中圖上一律回傳 `DownTicks` 的值,與商品類別無關**。期貨那一列
> 是文件行為;股票那三列是文件沒說、但實際如此。publisher 與 binding 都逐字轉發,
> 所以落地的 `el_open_interest` 在盤中圖上就是 `el_downticks` 的複本。
>
> 這也解釋了本文件先前標為「存疑」的 `1d` `el_ticks`:**股票日線的 `Ticks` 是
> 「Volume 與 Open Interest 之和」**,而股票的 OI 為 0,所以它恆等於 `el_volume`
> ——SPY 499 個日線列全部成立。**它從來不是筆數**,不可當成交筆數使用。

**期貨日線的 `el_downticks` 是未平倉量。** 任何對 `el_downticks` 做加總的消費端,
在期貨日線上加總到的是 OI,不是成交量。危險性與 Volume/Ticks 那一組完全相同。

**wire 上必須有 `category`,消費端才查得動這張表。** 見 §3.5。

[elvol]: https://help.tradestation.com/10_00/eng/tsdevhelp/elword/el_definitions/easylanguage_words_related_to_ticks,_volume_&_open_interest.htm

**`Volume` 與 `Ticks` 在 intraday 與 daily 之間是對調的。** 直覺的「`Volume` 是量、
`Ticks` 是筆數」只在日線成立；在 intraday 上 `Volume` 少算了下跌 tick 的成交，而真正的
總量在 `Ticks`。

前一代的 wire 只有一個 `vol` 欄位，由 publisher 依 `BarType` 決定要填 `Volume` 還是
`Ticks`。那個選擇發生在 wire 之外，產出的數字**永遠看起來合理**，所以需要額外一個版本
欄位來宣告「這批數字是照哪一版規則算的」—— 而那個宣告本身又是另一個會不同步的東西
（indicator 裝在使用者的 TradeStation 上，不隨 DLL 或 binding 更新）。

**五個保留字各給一欄，整個問題就消失了。** 語意反轉仍然是事實，但它現在是 consumer
查上表就能解決的事。

#### 規則

0. **前提：圖表的 Volume 屬性必須設為 Trade Volume。** 上表是該設定下的定義；設成
   Tick Count 時這些保留字改吐 tick 筆數而非股數，於是欄位裡會裝進一個數量級完全
   不同、卻同樣合理的數字。`BarType` 看不見這個設定，publisher 無從偵測，所以它是
   **操作者的責任**：掛上指標前先確認 symbol 的 Volume 設定。
1. **publisher 原樣透傳，不得選擇、換算或修正。**
2. **binding 原樣落地，不得選擇、換算或修正。** 想要「總成交量」的 consumer 自己依
   上表取用：intraday 取 `el_ticks`，daily 取 `el_volume`。
3. **欄位名的 `el_` 前綴是規範的一部分，不得省略。** 看到 `el_volume` 的人會去查
   EasyLanguage 的定義；看到 `volume` 的人不會 —— 而在 intraday 上它並不是成交量。
   這個坑本 repo 踩過一次，代價是一份系統性低估約一半的成交量資料。
4. **index / breadth symbol 的量值不具意義。** 這類 symbol 根本沒有成交量，五個欄位
   都是 0。衍生的 VWAP 之類應回 null 而非除以零。

**適用範圍：股票商品。** 上表取自 TradeStation 對股票的定義，本 repo 也只在股票上實測過。
**期貨等其他商品未驗證** —— publisher 原樣透傳，所以在這些商品上不會出錯，但上表的
intraday/daily 對照是否成立需要用 `LogPublish` 自行實測。

> **本 repo 實測（SPY 100-tick 圖，2026-07-24 收盤前後）**：`Ticks` 恆大於 `Volume`，
> 比值散在 1.01–7.60、中位數約 1.6 —— 與「總量 vs 上漲量」相符（上漲量通常占總量四到
> 六成）。最有力的一筆是 16:00:00 的收盤集合競價：`Ticks=760951` / `Volume=753328`，
> 比值 1.01：單一大額成交整筆被歸為上漲 tick，於是上漲量幾乎等於總量。文件與實測互證。
>
> 已收資料的 `Ticks`/`Volume` 比值穩定在 **1.85–2.25**（1m 與 5m、五個交易日）。

> **`1d` 的 `el_ticks` 存疑。** 依上表它應是筆數、不該等於 `el_volume`，但本 repo 的
> SPY 日線 499 筆中兩者逐位元組相同。推測是 TradeStation 的日線來源未提供 tick count
> 而以總量填充，尚未證實。在證實之前，`1d` 的 `el_ticks` 不應被當成筆數使用。

#### fixture 抓得到什麼、抓不到什麼

規則 2（原樣落地）在 conformance 上只有一部分可驗證，這件事必須寫下來，否則下一個
binding 會以為「fixture 全過」等於「五個欄位都對」：

| 錯誤實作 | fixture 抓得到嗎 |
| --- | --- |
| 用 `el_upticks + el_downticks` 算出 `el_ticks` | ✅ 抓得到 —— 價格不變的成交兩邊都不算，所以 `Ticks` 嚴格大於兩者之和；fixture 刻意讓它不相等 |
| `el_ticks` 與 `el_downticks` 互換 | ✅ 抓得到 |
| **`el_volume` 與 `el_upticks` 互換** | ❌ **抓不到，而且永遠抓不到** |

最後一列不是 fixture 的疏漏。上表裡 TradeStation 在 **intraday 與 daily 兩種régime**
都把 `Volume` 和 `UpTicks` 定義成同一個數字，所以真實資料本身就無法區分這兩欄的互換 ——
沒有任何錄製得出來的 frame 能證明實作讀對了欄位。這一欄只能靠讀 code 保證。

### 3.5 `category` —— 查 §3.4 那張表的鑰匙

§3.4 的每一列都分「股票／外匯」與「期貨」,§3.2 的報價無效化只對指數成立,而 **wire
上必須說得出一根 frame 來自哪一類商品**,否則消費端無從查表,binding 只能靠硬編碼的
代碼清單猜——而猜錯不會有任何錯誤浮現。

EasyLanguage 的 `Category` 保留字提供這個事實。官方值(取值前必須先指派給數值變數,
`Value1 = Category;`):

| 值 | 意義 | 值 | 意義 | 值 | 意義 |
| --- | --- | --- | --- | --- | --- |
| 0 | Future | 8 | Index Option | 14 | Composite |
| 1 | Future Option | 9 | Cash | 18 | Stock CFD |
| 2 | Stock | 10 | Bond | 19 | Forex CFD |
| 3 | Stock Option | 11 | Spread | 20 | Index CFD |
| 4 | Index | 12 | Forex | 21 | Future CFD |
| 5 | Currency Option | 13 | CPC Symbol | | |
| 6 | Mutual Fund | 7 | Money Market Fund | | |

**publisher 不得用 `Category` 做任何分支。** 它和其他保留字一樣逐字轉發。若指標依它
決定「送 `OpenInt` 還是送 0」,落地的 0 就分不出是 TradeStation 說的還是指標填的
——那正是已被移除的 `pv` / `publisher_version` 的成因(見 `EL/TS2Python_Exporter.el`
量值註解)。判斷屬於消費端與本文件。

> **實測(2026-08-02,live)**:`SPY`=2、`@ES`=0、`VXX`=**2**、`SPY 260803C745`=3、
> `$TICK` / `$VIX.X` / `$ADD` / `$VOLD` / `$TRIN` 全部 =4 且五個量值皆為 0。
>
> `VXX` 是 `2 (Stock)` 而非 Index —— 它是可交易的 ETN,實測單根 bar 有 567,776 的
> 成交量。reference binding 的 `DEFAULT_INDEX_SYMBOLS` 把它列為指數,因而**正在丟棄
> VXX 真實的 bid/ask**。這正是硬編碼清單必然出的錯:清單是猜的,`category` 不是。

#### `1d` 的量與 intraday 加總對不上，而且本來就不該相等

把一天的 intraday bar 加總，**不會**等於同一天 `1d` bar 的量。兩者是不同口徑的兩份
事實，不是任一方算錯：

| 來源 | 口徑 |
| --- | --- |
| `1d` | 交易所結算後發布的**官方彙總成交量**（consolidated daily volume）|
| intraday | 盤中即時串流（SIP tick data）當下組出來的連續撮合量 |

差異來自四個層面：

1. **盤後延遲申報與大宗交易** —— block trades、OTC、dark pool 撮合、late prints
   （Form T）。這些不會落進盤中任何一根 intraday bar，有些在收盤數小時後才申報，
   但全部計入官方日線總量。
2. **資料源涵蓋範圍** —— 日線彙整全美所有交易所（consolidated tape）；盤中串流可能受
   訂閱層級或過濾規則限制，涵蓋較窄，加總自然偏小。
3. **收盤集合競價**（closing cross / MOC）—— 16:00 的集合競價量體很大，日線必然包含；
   intraday 那一刻的成交可能落在最後一根 bar 的邊界外。
4. **Session 設定** —— intraday 圖表的 session template 由使用者決定；`1d` 由
   TradeStation 的日線伺服器獨立提供，**完全不受本地圖表設定影響**。

**不要用 intraday 加總去「驗證」`1d`，也不要反過來用 `1d` 回填 intraday。**

> **本 repo 實測（SPY 2026-07-23）**：`1d` 的 OHLC 是 **RTH 口徑** —— `high`/`low`
> 與 09:30–15:55 的 intraday 極值完全相同，而當天最高價其實出現在盤前（盤前 746.21，
> 日線 742.56）。量的部分，即使把盤前盤後全部加進來（06:00–19:55，168 根 5m bar）：
>
> | | 值 | 對日線 55,437,545 的落差 |
> | --- | --- | --- |
> | intraday `Volume` 加總（上漲量） | 18,505,973 | **3.00×** |
> | intraday `Ticks` 加總（總量） | 41,552,075 | **1.33×** |
>
> 取錯欄位會得到 3 倍落差，取對欄位剩 1.33 倍 —— 後者落在上述四項的合理範圍內。
> **兩者本來就不該相等，但也不該差三倍。** 看到三倍量級時，先確認自己取的是哪個欄位。

---

## 4. Session 規則

| 規則 | 值 |
| --- | --- |
| US equity RTH | 09:30–16:00 **ET** |
| `session_open_utc` | 固定 09:30 ET，**不受 chart session 設定影響** |
| Session 歸屬 | 04:00 ET 之前的 bar 屬於**前一個** session |

### 4.1 Per-symbol 保留政策

由 binding 設定檔的 `category` 決定預設（reference binding 為
`bindings/python/config/symbols.yaml`），可逐 symbol 覆寫：

| category | session 重置 | 盤前保留 |
| --- | --- | --- |
| `breadth` | **每日重置**（09:30 ET 清空） | 無 |
| 其他（`etf` / `volatility` / `mega_cap`） | 不重置 | 預設 60 分鐘 |

> 這些是**市場規則**，不是某語言的實作細節。任何 binding 若自行詮釋，
> 產出的 session 邊界會與其他 binding 不一致。

---

## 5. Symbol 前綴衝突

ZMQ 的 `SUBSCRIBE` 是 **prefix match**：訂閱 `SPY` 也會收到 `SPYG` 的訊息。

binding 在收訊後**必須**以 topic 字串完全相等再過濾一次，不可假設訂閱本身已精確。

---

## 6. 序號與缺漏偵測

`seq` 只有在 binding 正確詮釋下才有意義。以下各條皆為**強制行為**。

### 6.1 per-symbol，且 tick 與 bar 共用

`seq` 的計數單位是 **symbol**，不是 (symbol, kind)。同一個 symbol 的 `tick` 與 `bar`
交錯在同一條 topic 串流上，共用一個計數器才能偵測該串流的遺漏。

實測範例（`test_harness --mode smoke`，5 筆 tick 輪流三個 symbol + 1 根 SPY bar）：

| symbol | seq |
| --- | --- |
| SPY | 1, 2, **3**（第 3 筆是 `bar`） |
| QQQ | 1, 2 |
| VXX | 1 |

之所以不用全域序號：subscriber 可能只訂閱一個 topic，全域序號的跳號會與它從未
訂閱的流量混淆，無法判斷自己是否漏收。

### 6.2 首次見到某 symbol → 建立基準，不得報告遺漏

中途加入的 subscriber 第一筆看到 `seq=21`，**不代表它遺失了 20 筆** —— 那些訊息
發送時它根本沒在聽。第一筆只用來建立期望值。

### 6.3 `sid` 變更 → 重置，不是遺漏

`sid` 不同代表 publisher 重啟、所有計數器歸零。此時必須清空狀態，否則會把
「重啟」誤報成數十億筆遺漏。

init 的冪等路徑（重複呼叫回傳 `1`）**不會**更新 `sid`，所以在 TradeStation
重新 Verify indicator 不會被誤判成重啟。

`sid` 的解析度必須細到足以分辨**連續兩次重啟**。publisher 端若只取到秒，
同一個 wall-clock 秒內重啟的兩個 session 會共用同一個 `sid`，subscriber 於是走
§6.4 的「`seq < expected` 不回退」分支，把新 session 的前幾筆全當成重播 ——
新 session 真正掉的訊息完全不可見，而 `messages_lost` 仍讀 0。
本 repo 的 DLL 用**微秒**（同時仍在 2^53 之內，以 double 解析 JSON 的 binding 也精確）。

### 6.4 `seq < expected` → 不得回退期望值

TCP 保證單一 publisher 的順序，所以較小的序號是重複或重播，不是亂序。記錄它，
但**期望值必須維持不變** —— 否則下一筆正常訊息會被誤判成 gap。

### 6.5 序號在送出失敗時仍然消耗

publisher 在組裝 payload 前取號；後續截斷或送出失敗時該號不會上線。
**這是刻意的** —— 那筆資料確實遺失，顯示為 gap 才誠實。

### 6.6 `messages_lost` 的語意

本協定的每一筆 payload 都帶 `seq`，所以 `messages_lost == 0` 就是「沒有遺失」，
沒有第二種含義。

唯一的例外是 §6.2：在某個 symbol 的第一筆訊息到達之前，該 symbol 沒有任何判斷依據。
binding 應讓呼叫端能看出「這條流已經開始計數了嗎」，而不是把「還沒開始」與「已確認
乾淨」都顯示成 0。

---

## 7. 規則新增準則

往本文件加規則的判準：**「換一種語言重寫 binding 時，會不會有人猜錯？」**

若答案是「會」，它就屬於這裡，而不是某個 binding 的原始碼註解。

每條新規則都必須：

1. 在 `fixtures/` 有對應情境
2. 在 `fixtures/expected/` 有語言中立的期望結果
3. 被至少一個 binding 的 conformance 測試實際消費
