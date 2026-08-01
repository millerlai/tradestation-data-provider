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
| `ts` | DLL 收訊端 wall clock（UTC epoch 秒） | **Tick 的事件時間**；延遲量測 | ❌ Bar 邊界 |
| `ts_str` | EL 原始字串，逐字透傳 | **Bar `bucket_start` 的唯一權威來源** | ❌ Tick 時間 |

### 1.1 規則

- **Tick 的時間 = `ts`**（DLL 收訊端 UTC epoch）。
- **Bar 的 `bucket_start` = `ts_str` 解析結果**：以 `yyyy-MM/dd-HH:mm:ss` 格式、
  **`America/New_York` 時區**解析，再轉 UTC。
  - 必須用 IANA tz database 的 `America/New_York`，**不可用系統本地時區**，也不可用
    固定 UTC 偏移。DLL 主機的系統時區與此無關。
  - 解析出來的是 bar 的**收盤**時間（EL 的 `Time`），**還不是** `bucket_start`：
    必須再依 §2 減去一個 `tf` 換成左標籤，然後依 §2.2 對齊格線。只做到解析就寫入，
    整條序列會靜默偏移一格。
- `ts_str` 為 `""`（EL 未傳）或解析失敗時，binding 需有明確的降級行為並記錄。
  **publisher 不再驗證這個字串** —— 舊協定的 DLL 會順手把它解析成 `ts_utc`，等於做了
  一次格式檢查；那個欄位已移除，所以格式錯誤現在一路到 binding 才會被發現。

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

## 2. Bar 用左標籤

`bucket_start` 表示區間 **`[t, t+step)`**（半開）。

US RTH 09:30–16:00 的 1m session 產生 **390** 根 bar：

```
bucket_start:  09:30, 09:31, …, 15:58, 15:59
                 ↑                        ↑
              第一根                   最後一根，涵蓋 [15:59, 16:00)
```

**不是** 09:31 … 16:00。

> 這條是被真實 bug 逼出來的：`verify_parquet.py` 的 `_expected_bars()` 曾產生右標籤
> 序列，導致完整 session 被誤判為缺漏。左/右標籤是市場資料最典型的靜默錯誤 ——
> 兩邊都「看起來對」，只差一根。
>
> **conformance fixture 必須涵蓋 session 首尾兩根 bar，而且必須送 wire 的真實形狀
> ——右標籤的 `09:31` / `16:00`，不是 contract 自己的答案。** `session.jsonl` 一度
> 送 `09:30` / `15:59`，於是 fixture 因構造而與規格一致，這條規則等於沒被驗證，
> 右標籤的 bar 就這樣一路寫進了 Parquet。fixture 的職責是複現 publisher，不是
> 複述 spec。

### 2.1 `bucket_start` 必須向下取整到分鐘

解析 `ts_str` 得到 UTC 時間後，**秒與微秒一律歸零**。

1 分鐘 bar 的 bucket 依定義是 `[分鐘邊界, +1min)`。若原樣保留秒數，`17:30:45` 起算的
bucket 涵蓋 `[17:30:45, 17:31:45)`，那不是分鐘 bar，也無法與其他 bar 對齊。

EL 正常情況下送出的就是分鐘對齊的時間，所以取整通常是 no-op —— 但 **`test_harness`
的 `--mode smoke` 會把 tick 的 `13:30:45` 直接沿用給 `EL_PublishBar`**，
`smoke.jsonl` 因此正好涵蓋這個情境。

> 這條規則原本只存在於 reference binding 的實作裡，本文件漏寫。是 conformance 測試
> 比對手寫期望值時抓出來的 —— 照當時的規格實作，新 binding 會產出 `17:30:45Z`
> 而非 `17:30:00Z`，與 reference binding 不一致。

### 2.2 bucket 錨定於 session

**bucket 的格線錨在交易時段，不是 Unix epoch。** 但 intraday 與 daily 用**不同的
劃格空間**，理由見下。

| timeframe | 錨點 | 劃格空間 | 產生的邊界 |
| --- | --- | --- | --- |
| `1m` `5m` `15m` `30m` `1h` | **09:30 ET**（RTH 開盤） | **UTC**（原點 = 某個 09:30 ET 對應的 UTC 瞬間） | …09:30, 09:35 … / 09:30, 10:30 … |
| `1d` | **04:00 ET**（延長時段起點，見 §4） | **ET 牆鐘** | 04:00 → 次日 04:00 |

#### 為何不能用 epoch 錨定

直接對 UTC 時間做 epoch 錨定的分桶（多數時序函式的預設），在短週期上**恰好正確**
—— ET 偏移是整小時，而 09:30 又是 30 分的倍數。但兩個長週期會錯：

| timeframe | epoch 錨定的後果 |
| --- | --- |
| `1h` | 產生 09:00 ET 桶，RTH 首根只涵蓋 09:30–10:00 —— **半根 bar 掛著整根的時間戳**，下游無從分辨 |
| `1d` | 依 UTC 午夜切分，而 UTC 午夜正是 **20:00 ET，延長時段的結束點**，盤後成交會被推到隔天 |

#### 為何 intraday 用 UTC 原點而非 ET 牆鐘

錨點是 09:30 ET，但**格線本身必須在 UTC 上鋪**。原因是 DST 折疊區：

- 回撥日（例：2025-11-02），`01:15 EDT` 與 `01:15 EST` 相差整整一小時，
  牆鐘讀數卻一模一樣。在 ET 空間分桶會把兩者併進同一個桶，
  產出一根**橫跨兩個真實小時的「1h」bar**。
- 前撥日（例：2025-03-09），`03:15 EDT` 在 ET 空間會被標到 `03:30`，
  **`bucket_start` 比它所含的資料還晚**，直接違反 §2 的 `[t, t+step)`。

而 UTC 原點不會讓格線相對交易時段偏移：`1m`/`5m`/`15m`/`30m`/`1h` **都整除 DST 的
一小時位移**，所以同一條 UTC 格線在 EST 與 EDT 都恰好落在 09:30 ET。
（這也是為什麼新增 timeframe 時，「是否整除一小時」是必須檢查的條件；
`4h` 之類不整除的區間需要另外定義規則。）

#### 為何 `1d` 反而必須用 ET 牆鐘

日曆日一年有兩天是 23 或 25 小時，固定的 UTC 位移表達不了。
`1d` 因此在 ET 空間分桶；這是安全的，因為 **04:00 ET 落在折疊區之外**
（轉換發生於 02:00 ET），邊界轉回 UTC 時不存在模糊。

#### bar 的對齊由 binding 負責

wire 的 `ts_str` 是 EL 圖表自己的 `Date`/`Time`，**不保證落在上述格線上**
（日線圖尤其不會是 04:00 ET）。因此：

> **binding 必須把每一根 bar 的 `bucket_start` 向下取整到該 `tf` 的格線。**

不這麼做，同一個交易日可能在 `timeframe=1d` 分區裡出現兩列不同時間戳的同一根日線，
而任何 `bucket_start` 上的 join 都會重複計數。

> **已確認（2026-07-26，live TradeStation）**：EasyLanguage 的 `Time` 是 bar 的
> **收盤**時間，`ts_str` 又由它逐字組成，所以 wire 上的 `ts_str` 一律是**右標籤**。
>
> 偏移是整整一格，不是能被向下取整吸收的秒數差：一份 live 採集的 1m SPY 檔案落地成
> 09:31…16:00 共 390 根，恰好是 §2 明令禁止的那個序列。根數相同、數值合理，所以沒有
> 任何一層報錯。
>
> 因此 **binding 必須在對齊前先減去一個 `tf`**。`1d` 這類 session 錨定的 interval
> 例外，而且必須例外：對齊本身已經丟棄時分秒改用 04:00 ET，若先減一天，只會把 bar
> 記到前一個交易日。

### 2.3 讀取端的時區語意：對外一律 ET，UTC 只是內部絕對時間

儲存層兩份都留（`bucket_start` UTC + `bucket_start_et` ET），但那是**儲存**的事。
對呼叫端而言，這是一個美股的 API：規則時段、假日、盤前盤後、`date=` 分區全都以
`America/New_York` 定義，沒有人在做美股時心裡換算 UTC。

1. **讀取 API 收到的 naive datetime 一律解讀為 `America/New_York`。**
   帶時區的輸入照它自己的時區處理 —— 那沒有歧義，不需要預設值。
2. **UTC 只用於內部：** 絕對時間的儲存與 §2.2 的 bucket 格線。這些都不該外洩成呼叫端
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
| `Volume` | **只有上漲 tick 的成交股數** | 總成交股數 |
| `Ticks` | **總成交股數** | 總 tick 數 |
| `UpTicks` | 上漲 tick 成交股數 | 總成交股數 |
| `DownTicks` | 下跌 tick 成交股數 | 0 |

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
