# semantics — schema 管不到、但 binding 必須一致的規則

> **本文比 JSON Schema 重要。** Schema 只驗證欄位存在與型別；binding 之間真正會產生
> 分歧的是語意。只讀 schema 就動手寫的 binding，會產出「通過驗證但與其他 binding
> 不一致」的資料。
>
> 每一條規則都應有對應的 conformance fixture。

---

## 1. 時間權威

wire 上有三個時間戳，用途**不可互換**：

| 欄位 | 來源 | 正確用途 | 錯誤用途 |
| --- | --- | --- | --- |
| `ts` | DLL 收訊端 wall clock（UTC epoch） | **Tick 的事件時間**；延遲量測 | ❌ Bar 邊界 |
| `ts_utc` | `ts_str` 經 `zoned_time`（America/New_York）轉出 | **僅作交叉稽核** | ❌ 任何權威用途 |
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
- `ts_utc` 僅用於交叉檢查：與 `ts` 差距 > **5 秒**時記錄警告，**不得拋錯或丟棄資料**。
  這種漂移幾乎都是 DST 表差異造成。
- `ts_str` 為 `""`（EL 未傳）或解析失敗時，`ts_utc` 為 `0.0`；binding 需有明確的
  降級行為並記錄。

> **為何 `ts_str` 而非 `ts_utc`？** `ts_utc` 是 DLL 主機算出來的；若該主機的 tz
> database 過期，DST 轉換日會算錯。`ts_str` 是原始事實，讓每個 binding 用自己的 tz
> database 解析，可驗證、可重算。

### 1.2 為何這條必須是契約級

若某個 binding 改用 `ts_utc` 作 bar 邊界，它與其他 binding 在 **DST 轉換日**會算出
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

`bar_1m` 的 bucket 依定義是 `[分鐘邊界, +1min)`。若原樣保留秒數，`17:30:45` 起算的
bucket 涵蓋 `[17:30:45, 17:31:45)`，那不是分鐘 bar，也無法與其他 bar 對齊。

EL 正常情況下送出的就是分鐘對齊的時間，所以取整通常是 no-op —— 但 **`test_harness`
的 `--mode smoke` 會把 tick 的 `13:30:45` 直接沿用給 `EL_PublishTickEx`**，
`smoke.jsonl` 因此正好涵蓋這個情境。

> 這條規則原本只存在於 reference binding 的實作裡（`_parse_el_str_as_et` 的
> `replace(second=0, microsecond=0)`），本文件漏寫。是 conformance 測試比對
> 手寫期望值時抓出來的 —— 照當時的規格實作，新 binding 會產出 `17:30:45Z`
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

#### native 的非 1m bar 由誰對齊 —— **binding**

wire 的 `ts_str` 是 EL 圖表自己的 `Date`/`Time`，**不保證落在上述格線上**
（日線圖尤其不會是 04:00 ET）。因此：

> **binding 必須把每一根 bar 的 `bucket_start` 向下取整到該 `tf` 的格線**，
> native 與 derived 一視同仁。

不這麼做，同一個交易日會在 `timeframe=1d` 分區裡出現兩列 —— 一列來自 EL 的時間戳、
一列來自重採樣的 04:00 ET 錨點 —— 違反 §2.3 rule 2，且任何 `bucket_start` 上的
join 都會重複計數。

> **已確認（2026-07-26，live TradeStation）**：EasyLanguage 的 `Time` 是 bar 的
> **收盤**時間，`TsStr` 又由它逐字組成，所以 wire 上的 `ts_str` 一律是**右標籤**。
>
> 這裡原本寫著「1m 不受影響（取整後兩者相同）」——**那句是錯的**。偏移是整整一格，
> 不是能被向下取整吸收的秒數差：一份 live 採集的 1m SPY 檔案落地成 09:31…16:00
> 共 390 根，恰好是 §2 明令禁止的那個序列。根數相同、數值合理，所以沒有任何一層
> 報錯。
>
> 因此 **binding 必須在對齊前先減去一個 `tf`**，native 與 derived 一視同仁。
> `1d` 這類 session 錨定的 interval 例外，而且必須例外：對齊本身已經丟棄時分秒
> 改用 04:00 ET，若先減一天，只會把 bar 記到前一個交易日。

> 這條也是被實作與規格不一致逼出來的：reference binding 的 `Resampler` 原本用
> epoch 錨定，而 `1h` / `1d` 的行為從未被測試涵蓋；改成 ET 牆鐘之後又引入了上述
> DST 折疊錯誤，且錯誤的理由被寫進了本節。兩者現已修正並由
> `bindings/python/tests/test_timeframe_grid.py` 逐點比對 SQL 與程式碼兩套實作。

### 2.3 native 與 derived 的 bar 不可互換

同一個區間的 bar 有兩種來源：

| 來源 | 產生方式 | 特性 |
| --- | --- | --- |
| **native** | TradeStation 自己聚合，經 wire 送達 | 聚合規則不透明；**日線含交易所官方 OHLC 與除權息調整** |
| **derived** | binding 由 tick 或 1m bar 算出 | 規則在我們手上、可重現、可稽核 |

#### 規則

1. **binding 必須讓兩者在儲存上可辨識。** reference binding 把 derived bar 的
   provenance 欄位標為 `derived:<來源>`（例如 `derived:ticks`、`derived:1m`），
   native 則保留 provider id。
2. **同一個區間分區內不得混有兩種來源。**
3. **native 優先。** derived 的結果**不得覆寫** native bar。
4. **`1d` 不得由計算產生。** 不只是「不得覆寫」—— binding **不得**用 tick 或 1m
   推導出日線並持久化，即使該分區本來是空的。查不到就回空，由使用者去 TradeStation
   把日線圖掛上匯出。理由見下：一根算出來的日線在磁碟上跟真的一模一樣，下游沒有
   任何辦法分辨，所以唯一安全的規則是根本不產生它。

#### 為何日線特別重要

`1d` 是唯一「native 明確較優」的區間 —— TradeStation 的日線帶有**交易所官方收盤價
與除權息調整**，這兩樣都**無法**由 tick 加總還原。derived 的日線是個長得一模一樣的
近似值，換掉 native 之後看起來完全合理，卻是錯的。

日線的 `vol` 同樣是交易所的官方彙總量，與 intraday 加總分屬兩種口徑，**不可互相
驗證或回填**（§3.4）。發現兩者對不上時，那通常不是 bug。

> 注意規則 1 到 3 適用於**每一個**區間，不是只有 `1d`：5 分鐘圖送出的是 native 的
> 5m bar，跟 resampler 寫進同一個 `timeframe=5m/` 目錄。判斷依據永遠是 provenance
> 欄位，不是路徑。

> reference binding 的實作曾有這個洞：`Resampler` 用 `first(source)` 把來源原樣
> 複製，於是 derived 與 native 在磁碟上**完全無法區分**；而快取寫入是直接覆寫，
> 只要範圍內有一天缺資料，整段就會重算並把旁邊的 native 日線一併蓋掉。

intraday 則相反：由 tick 聚合是可重現、可驗證的，且只需掛一張圖。

### 2.4 讀取端的時區語意：對外一律 ET，UTC 只是內部絕對時間

儲存層兩份都留（`bucket_start` UTC + `bucket_start_et` ET，§3.6），但那是**儲存**的事。
對呼叫端而言，這是一個美股的 API：規則時段、假日、盤前盤後、`date=` 分區全都以
`America/New_York` 定義，沒有人在做美股時心裡換算 UTC。

1. **讀取 API 收到的 naive datetime 一律解讀為 `America/New_York`。**
   帶時區的輸入照它自己的時區處理 —— 那沒有歧義，不需要預設值。
2. **UTC 只用於內部：** 絕對時間的儲存、查詢引擎的決定性、以及 §2.2 的 bucket 格線。
   這些都不該外洩成呼叫端必須自己換算的負擔。
3. **以「日」為單位的參數（CLI 的 `--start-date` 之類）指的是 ET 日曆日**，不是 UTC 午夜。

> 這條規則是補寫的，因為 reference binding 原本把 naive 當 UTC —— 純粹是查詢引擎
> session 設成 `UTC` 的副作用洩漏到 API 上，沒有人決定過。後果是
> `load_bars(sym, datetime(2026,4,20,9,30), ...)` 看起來像在問開盤，實際問的是
> 05:30 ET。同一個洩漏也讓 `audit_bar_cache --start-date D` 實際從 `D-1 20:00 ET`
> 開始。查詢引擎要用什麼 session 時區是實作自由，**但不得因此改變 API 的語意**。

### 2.5 空區間的讀取語意

「這個 symbol 在這段時間沒有交易」是一個**正常的答案**，不是錯誤。休市日、盤中暫停、
冷門標的的盤前時段都會落在這裡。讀取面因此有兩條規則：

1. **空區間傳回 0 列，不得拋例外、不得傳回 0 欄位的空白 frame。**
   分區檔存不存在只能區分「從未錄過」，不能用來判斷區間內有沒有資料。

2. **同一個呼叫的空答案與非空答案，schema 必須完全一致**（欄位、順序、型別）。
   否則呼叫端跨 symbol 或跨日堆疊結果時，會在第一個沒交易的日子上炸開，而不是
   得到少一列的結果。注意各讀取路徑帶的 hive 欄位不同（單檔 `1d` 佈局帶
   `timeframe`，`date=` 佈局另帶 `date`），所以此處要求的是**與自己的非空答案一致**，
   而不是一份跨路徑的固定欄位表。

> reference binding 曾在這裡踩過兩次。第一次是 DuckDB 1.5 把 `.arrow()` 的回傳改成
> `RecordBatchReader`，空結果的 reader 既無 batch 也無 schema，`pl.from_arrow` 直接
> 拋 `ValueError`；回測問到一個沒動靜的日子就會中斷整個 run。第二次是修掉它之後，
> 空答案與非空答案的欄位數不同（少了 `bucket_start_et`），`pl.concat` 換成拋
> `ShapeError` —— 崩潰的位置換了，呼叫端一樣沒辦法統一處理。

### 2.6 分區是一整個 ET 交易日，重算視窗不得吃掉同日其他資料

Tier-3 的一個 `date=` 分區代表**一整個 ET 交易日**，但一次重算只涵蓋被查詢的視窗。
寫入若是整檔覆寫，先建好盤前、之後再重算 RTH，就會把盤前那幾根一併刪掉：磁碟列數
下降、沒有任何錯誤，而且 Tier-1 的 tick 一旦被清掉就再也救不回來。

因此重算既有 derived 分區時**必須與檔內既有資料合併**：重算視窗內的 bucket 以新算
結果為準，視窗外的既有 bucket 必須保留。native 分區不適用 —— 它在更前面就已經被
§2.3 規則 3 擋掉，根本不會走到合併。

### 2.7 快取覆蓋率必須另外記錄，不能用「檔案存不存在」推論

Tier-3 的快取需要回答一個問題：**這一天算過了嗎？** 直覺的答案是看
`date=<D>/bars.parquet` 在不在，但那是錯的，而且錯得很安靜。

同一條路徑有**多個寫入者**，各自的語意不同：

| 寫入者 | 寫出來的東西 |
|---|---|
| lazy resampler | 被查詢視窗涵蓋的 bucket，**不是整天** |
| `BarWriter`（live ingest） | 當天到目前為止的 bar，盤中持續長大 |
| 批次聚合工具 | 該工具自己的欄位集（可能沒有 `bucket_start_et`） |
| 舊版本的 binding | 以當時規則寫的任何子集 |

所以「檔案存在」只代表**有人寫過東西**，不代表**這一天完整**。把它當成完整性訊號，
盤中那天會在第一次查詢後凍結、既有快取裡每一個部分分區會被永久當成完整、而任何
事後補進的資料都不會被撿起來 —— 全部沒有錯誤、沒有 log。

> reference binding 實作過這條錯路並實測到後果：盤中先查早盤、下午再查同一天，
> 下午的視窗回 0 列，整天查詢只回早上那 6 根。

#### 規則

1. **覆蓋率記錄必須由「建置者」獨佔寫入**，與 bar 檔本身分開存放。其他寫入者不碰它，
   於是「有記錄」就確實代表「這個建置者算完過這一天」。

2. **每筆記錄必須帶上當時來源的指紋**（Tier-1 該日 tick 分區、或索引類 symbol 的 1m
   bar 分區）。讀取時比對指紋：
   - 相同 → 命中，直接讀 bar 檔，不重掃來源。
   - 不同或來源已出現／消失 → 記錄作廢，重建整天。

   指紋是這條規則的重點。有了它，「盤中長大」「事後補資料」「當天原本沒資料後來有了」
   三種情形都會自動失效重建，**不需要**任何「假日 vs 未 ingest」的猜測 —— 而那種猜測
   是猜不準的：市場休市與 ingestion 中斷在資料上長得一模一樣。

3. **沒有資料的一天也要能被記錄**，而且**不得**用 0 列的 bar 檔充當記錄。理由是 0 列檔
   會落進 native tier 的目錄（`1m`），而清快取工具依設計不會碰 native tier，等於留下
   一個清不掉的錯誤答案。空的那天記在覆蓋率記錄裡即可。

4. **記錄是可丟棄的**。刪掉它只會讓下一次查詢重算，不得改變任何答案的正確性。不認得
   這份記錄的 binding 必須仍然能正確讀取整個資料目錄 —— 它只是拿不到快取效益。

5. **`NATIVE_ONLY_TIMEFRAMES`（`1d`）不適用**：那一層從不由本地計算產生，沒有覆蓋率
   的概念。

---

## 3. bid / ask 何時無效

報價有**兩種**無效情形，來源不同，處理方式也不同。

### 3.1 沒有報價可報 → wire 上是 `null`（publisher 負責）

EL 傳的是 `InsideBid` / `InsideAsk`，那是**即時報價函式**。以下情況它們回傳 `0`：

| 情況 | 說明 |
| --- | --- |
| **歷史回放** | 圖表載入、任何非即時 bar。TradeStation 不在 live mode 時就沒有報價 |
| **本身無報價的 symbol** | breadth 指數（`$TICK`、`$ADD` …）從來就沒有買賣盤 |

wire v2 起，**DLL 會把非正值報價正規化為 JSON `null`**（`format_quote()`，
`!(v > 0.0)` 因此也涵蓋 NaN）。wire 自己說出「沒有報價」，binding 不需要記得
「`0` 代表無效」這種只活在文件裡、遲早被漏掉的規則。

> **wire v1 沒有這個保護。** v1 的 payload 一律是 `%.6f`，歷史回放會送出
> `"bid":0.000000`。讀 v1 的 binding **必須**自行把 `<= 0` 視為無效，否則會把
> `$0.00` 當成真實報價。`v1_legacy.jsonl` 就是為此存在。

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
例如 `bindings/python/config/symbols.yaml`）。代價就是**這條規則必須寫在契約裡** ——
現況即為如此：reference binding 把清單硬編在
`bindings/python/src/tradestation_data/wire/el_subscriber.py` 的
`DEFAULT_INDEX_SYMBOLS`，本文件將其升格為契約。

### 3.3 綜合判定

binding 應把 `bid` / `ask` 視為無效，若**任一**成立：

1. 值為 `null`（v2 publisher 已判定沒有報價）
2. 值 `<= 0`（v1 相容，或任何未來的異常值）
3. symbol 在 index / breadth 清單中（§3.2）

### 3.4 成交量

`vol` 對 index / breadth symbol 同樣不具意義。`vol == 0` 時衍生的 VWAP 應為 null
而非除以零。

#### `1d` 的 volume 與 intraday 加總對不上，而且本來就不該相等

把一天的 1m（或 5m）bar 的 `vol` 加總，**不會**等於同一天 `1d` bar 的 `vol`。兩者
是不同口徑的兩份事實，不是任一方算錯：

| 來源 | 口徑 |
| --- | --- |
| `1d` | 交易所結算後發布的**官方彙總成交量**（consolidated daily volume）|
| intraday | 盤中即時串流（SIP tick data）當下組出來的連續撮合量 |

差異來自四個層面：

1. **盤後延遲申報與大宗交易** —— block trades、OTC、dark pool 撮合、late prints
   （Form T）。這些不會落進盤中任何一根 intraday bar，有些在收盤數小時後才申報，
   但全部計入官方日線總量。
2. **資料源涵蓋範圍** —— 日線彙整全美所有交易所（consolidated tape：NYSE、NASDAQ、
   ARCA、BATS…）；盤中串流可能受訂閱層級或過濾規則限制，涵蓋較窄，加總自然偏小。
3. **收盤集合競價**（closing cross / MOC）—— 16:00 的集合競價量體很大，日線必然
   包含；intraday 若以 `[15:55, 16:00)` 為最後一根，那一刻的成交可能落在邊界外，
   或被單獨記成一筆 16:00 的 tick 而未計入。
4. **Session 設定** —— intraday 圖表的 session template 由使用者決定，涵蓋範圍
   隨設定改變；`1d` 由 TradeStation 的日線伺服器獨立提供，**完全不受本地圖表設定
   影響**。

因此：**不要用 intraday 加總去「驗證」`1d`，也不要反過來用 `1d` 回填 intraday。**
這是 §2.3 規定 `1d` 只能取 native、不得由 rollup 產生的另一個理由。

> **本 repo 實測（SPY 2026-07-23）**：`1d` 的 OHLC 是 **RTH 口徑**。`high` / `low`
> 與 09:30–15:55 的 intraday 極值**完全相同**，而當天最高價其實出現在盤前
> （盤前 746.21，日線 742.56）—— 日線並未納入盤前盤後的價格極值。`open` / `close`
> 差 0.02–0.03，是官方開收盤價與「該分鐘第一／最後一筆成交」之間的正常差距。
>
> **但 volume 的落差遠超過上述四個原因所能解釋的範圍。** 即使把盤前盤後全部加進來
> （06:00–19:55，168 根 5m bar），intraday 的 `vol` 合計仍只有日線的三分之一：
> 18,505,973 對 55,437,545。late prints 與 closing cross 一般是個位數到十幾個
> 百分比，不是三倍。
>
> **所以這一節不足以解釋任意大的差距。** 看到三倍量級時，先懷疑 `vol` / `tc` 兩個
> 欄位裝的到底是什麼，而不是急著用上面四點合理化 —— 見下節。

#### EasyLanguage 的 `Volume` / `Ticks` 在 intraday 與 daily 上語意相反

這是 wire 上 `vol` / `tc` 的來源，也是本節前面那個「落差過大」的主因。

TradeStation 官方定義（股票商品，[EL 保留字文件][elvol]）：

| EL 保留字 | **intraday**（分鐘 / tick / volume bar） | **daily 以上** |
| --- | --- | --- |
| `Volume` | **只有上漲 tick 的成交股數** | 總成交股數 |
| `Ticks` | **總成交股數** | 總 tick 數 |
| `UpTicks` | 上漲 tick 成交股數 | 總成交股數 |
| `DownTicks` | 下跌 tick 成交股數 | 0 |

[elvol]: https://help.tradestation.com/10_00/eng/tsdevhelp/elword/el_definitions/easylanguage_words_related_to_ticks,_volume_&_open_interest.htm

**兩者在 intraday 與 daily 之間是對調的。** 直覺的「`Volume` 是量、`Ticks` 是筆數」
只在日線成立；在 intraday 上 `Volume` 少算了下跌 tick 的成交，而真正的總量在 `Ticks`。

#### 規則

1. **wire 的 `vol` 一律是總成交股數。** publisher 必須依圖表型態取值：intraday 取
   `Ticks`，daily 取 `Volume`。取錯的後果不是精度問題，是**系統性低估**。
2. **wire 的 `tc` 只在 daily 上是成交筆數。** intraday 拿不到筆數 —— EL 沒有任何
   保留字提供它 —— 所以 intraday 的 `tc` **不具意義**，binding 不得將它當筆數使用。
3. **binding 不得自行推導或修正。** 這個對調發生在 publisher 那一側，wire 上看不出
   來；若 publisher 送錯，binding 無從分辨，這正是它必須寫在契約裡的原因。

> **本 repo 實測（SPY 100-tick 圖，2026-07-24 收盤前後）**：`Ticks` 與 `Volume`
> 同時輸出，`Ticks` 恆大於 `Volume`，比值散在 1.01–7.60、中位數約 1.6 —— 與「總量
> vs 上漲量」相符（上漲量通常占總量四到六成）。最有力的一筆是 16:00:00 的收盤集合
> 競價：`Ticks=760951` / `Volume=753328`，比值 1.01。單一大額成交整筆被歸為上漲
> tick，於是上漲量幾乎等於總量。文件與實測互證。
>
> **這解釋了前面那個落差的絕大部分。** 以本 repo 已收的 SPY 2026-07-23 實測：
>
> | | 值 | 對日線 55,437,545 的落差 |
> | --- | --- | --- |
> | intraday `vol` 加總（取自 `Volume`，即上漲量） | 18,505,973 | **3.00×** |
> | intraday `tc` 加總（取自 `Ticks`，即總量） | 41,552,075 | **1.33×** |
>
> 也就是說那個「遠超過四個原因所能解釋」的 3 倍，主要是讀錯欄位。改用正確欄位後
> 剩下 1.33 倍，落在 late prints、consolidated tape 涵蓋範圍、收盤集合競價與
> session 設定這四項的合理範圍內。**兩者本來就不該相等，但也不該差三倍。**
>
> 已收資料的 intraday `tc`/`vol` 比值穩定在 **1.85–2.25**（1m 與 5m、五個交易日），
> 與「上漲量約占總量一半」相符。

#### `tc` 沒有 provenance，同一欄混著三種來源 —— 已知限制

修正上述對調之後，`tc` 在同一個 `timeframe=` 目錄裡可能來自三個不同的地方，而
**欄位本身沒有任何東西能區分它們**：

| bar 從哪來 | `tc` 的值 | 是筆數嗎 |
| --- | --- | --- |
| native intraday（EL 直接送） | `0` | 否 —— intraday 拿不到筆數 |
| native `1d` | `Ticks` | 存疑（見下） |
| derived from ticks（binding 自己數 `count(*)`） | 該 bucket 的 tick 列數 | **是**，而且可信 |
| derived from 1m bars（`sum(tc)`） | 上游是 0 → `0` | 否 |

所以讀到 `tc = 0` 的呼叫端無法判斷那是「沒有筆數可給」還是「真的沒有成交」，讀到
`tc = 12` 也無法判斷那是 binding 自己數的、還是 publisher 給的。

這不是新問題 —— 修正前 native 給的是股數、derived 給的是列數，一樣不同源 —— 但
修正後從「兩者都錯」變成「一者為 0、一者可信」，混在一起反而更容易被誤讀。

> 正解是讓 `tc` 像 `source` 一樣帶 provenance，或乾脆拆成兩個欄位（publisher 給的
> 筆數 vs binding 數出來的筆數）。兩者都要升 wire 版本，尚未進行。
>
> **在那之前**：binding 不得依賴 `tc` 做任何跨來源的比較或聚合。它在 derived-
> from-ticks 的 bar 上是可信的成交筆數，其餘一律視為無資訊。

> **仍未決**：依上表，daily 的 `Ticks` 應是筆數、不該等於 `Volume`，但本 repo 的
> SPY 日線 499 筆中兩者逐位元組相同。推測是 TradeStation 的日線來源未提供 tick
> count 而以總量填充，尚未證實。在證實之前，`1d` 的 `tc` 同樣不應被當成筆數。

### 3.5 `Bar` 是否保留 bid / ask 由 binding 決定

wire 的 `bar_1m` 帶有 bar 收盤當下的 `bid` / `ask`。reference Python binding 的
`Bar` 型別**不保留**這兩個欄位，資料在此丟棄。

這是建模選擇而非解析錯誤 —— 保留的 binding 不算違規，丟棄的也不算。但 fixture 的
`expected/*.json` 一律記錄 wire 上的原值，讓選擇保留的 binding 有東西可比對。

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

## 6. 序號與缺漏偵測（wire v2 起）

`seq` 只有在 binding 正確詮釋下才有意義。以下各條皆為**強制行為**。

### 6.1 per-symbol，且 tick 與 bar 共用

`seq` 的計數單位是 **symbol**，不是 (symbol, kind)。同一個 symbol 的 `tick` 與
`bar_1m` 交錯在同一條 topic 串流上，共用一個計數器才能偵測該串流的遺漏。

實測範例（`test_harness --mode smoke`，5 筆 tick 輪流三個 symbol + 1 根 SPY bar）：

| symbol | seq |
| --- | --- |
| SPY | 1, 2, **3**（第 3 筆是 `bar_1m`） |
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

`EL_Init` 的冪等路徑（重複呼叫回傳 `1`）**不會**更新 `sid`，所以在 TradeStation
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

### 6.6 `messages_lost` 為 0 的兩種含義

對 v1 publisher（無 `seq`），計數恆為 0。binding 必須讓使用者能區分：

- **「沒有遺失」**（v2，有偵測能力）
- **「無從得知」**（v1，沒有偵測能力）

這個差別在用該數字判斷某天的資料可不可信時是關鍵。

**必須是公開 API 上的差別**，不能只是一行 log 或某個私有屬性 ——
消費端要能在程式裡判斷，否則「讀到 0」與「偵測從未啟動」在程式眼中一模一樣。
reference binding 的作法：`messages_lost` 回傳 `int | None`（`None` = 無從得知），
另外提供布林的 `gap_detection_available`。任何等效的判別面都可以，
但**不得只提供一個永遠是整數的計數器**。

---

## 7. 規則新增準則

往本文件加規則的判準：**「換一種語言重寫 binding 時，會不會有人猜錯？」**

若答案是「會」，它就屬於這裡，而不是某個 binding 的原始碼註解。

每條新規則都必須：

1. 在 `fixtures/` 有對應情境
2. 在 `fixtures/expected/` 有語言中立的期望結果
3. 被至少一個 binding 的 conformance 測試實際消費
