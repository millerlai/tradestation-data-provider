# Code review findings — `1eda23b` (wire v3 / ABI 8 / multi-timeframe bars)

來源：`/code-review max`（6 個 finder、54 個獨立 verifier，74 個候選 → 69 通過驗證 → 去重後 24 項）。
每一項的行號以 review 當時的 `1eda23b` 為準，修正時請以實際檔案為準。

狀態圖例：`[ ]` 未修 · `[x]` 已修 · `[~]` 部分修 · `[-]` 判定不修（附理由）

> **全部 34 項已處理。** 驗證：`uv run pytest` 349 passed（新增 40 項回歸測試）、
> `ruff check` / `ruff format --check` / `mypy` 全綠（binding 與 `contract/` 兩處）、
> C++ 重建（ABI 8）並重新錄製 6 份 conformance fixture。
>
> 兩處刻意保留的不確定性，已寫進 contract 而非留在原始碼註解：
>
> 1. **`semantics.md` §2.2 的「未決」方框** —— TradeStation 的 intraday bar 究竟用開盤
>    還是收盤時間標示，需要 live TradeStation 才能確認。若為收盤，native 的
>    5m/15m/30m/1h 會比 derived 晚一格（1m 與 1d 不受影響）。確認方式已寫在該處。
> 2. **H-03 改用行為判斷，不用 `BarType_ext`** —— 該常數各 TS 版本取值不同且無法在此
>    repo 驗證。改以「連續兩根 intraday bar 的 `Date`+`Time` 相同 ⇒ 秒級圖表」latch，
>    不依賴任何版本相關常數。若要釘出 `BarType_ext` 的值，方式寫在 `EL/README.md`。

---

## A. 多 timeframe 只做了一半（wire 有 `tf`，consumer 沒跟上）

### [x] A-01 · `_advance_direct_bars` 寫死 1 分鐘，非 1m bar 被截斷
`bindings/python/src/tradestation_data/runtime/ingestion.py:184`

`_advance_direct_bars` 用 `bar.bucket_start + _ONE_MINUTE + _DIRECT_BAR_CLOSE_GRACE <= now` 判斷收盤，
完全不看 `bar.timeframe`。wire v3 現在會送 5m/15m/30m/1h/1d。

**失效情境**：5 分鐘 SPY 圖 + EL「update every tick」。`tf="5m"` bucket 09:30 反覆送出，
但 09:31:05 就被強制收盤 —— sink 收到的是只涵蓋第一分鐘的 OHLC/volume，
之後四次帶著真實 H/C/volume 的更新被 `bucket_start <= _last_emitted_direct_bucket` 判為重複丟棄
（計入 `bars_duplicate_dropped`），全程沒有任何錯誤 log。
`data/bars/timeframe=5m/` 裡是「宣稱五分鐘、實際一分鐘」的 bar。

**修法**：close deadline 改成 `bucket_start + timeframe_to_timedelta(bar.timeframe) + grace`。

---

### [x] A-02 · direct-bar buffer 只用 symbol 當 key，不同 timeframe 互相踢掉
`bindings/python/src/tradestation_data/runtime/ingestion.py:252-262`

`_current_direct_bars: dict[str, Bar]` / `_last_emitted_direct_bucket: dict[str, datetime]`
都以 `bar.symbol` 為 key，但同一個 ZMQ topic 現在可以同時承載同一 symbol 的不同區間。

**失效情境**：同時開 SPY 1 分鐘圖與 5 分鐘圖（同一顆 DLL、同一個 PUB socket）。
5m bucket 09:30 被 buffer 住 → 1m bucket 09:31 進來，`bucket_start` 較晚，直接把它擠出去並提前 emit
（close 只反映第一次更新）→ `_last_emitted_direct_bucket["SPY"]=09:30` 讓真正的 5m 更新被當重複丟棄。
1m 分區完全正常，所以 operator 在他會看的地方看不出任何異狀。

**修法**：key 改為 `(symbol, timeframe)`。`_advance_direct_bars` / `_shutdown` 的 drain 一併調整。

---

### [x] A-03 · `_parse_bar` 一律 floor 到分鐘，非 1m bar 沒有對齊 grid
`bindings/python/src/tradestation_data/wire/el_subscriber.py:361`

`bucket_start` 由 `ts_str` floor 到分鐘產生，不分 timeframe。
`contract/semantics.md` §2.2 定義 intraday 錨在 09:30 ET、daily 錨在 04:00 ET。

**失效情境**：native 1d bar 的 `ts_str` 來自 EL 的 `Date`/`Time`，不會是 04:00 ET；
`Resampler` 產生的 derived 1d 則錨在 04:00 ET。兩者落在同一個
`bars/timeframe=1d/symbol=SPY/date=*/` 命名空間但 `bucket_start` 不同，
`load_bars(..., "1d")` 於是對同一個交易日回兩列 —— 違反 §2.3 rule 2，
也讓任何下游 join / bar count 檢查失效。

> 註：EL daily bar 的 `Time` 實際值無法在此 repo 驗證（需要 live TradeStation）。
> 修正時應同時在 contract 說清楚 native 非 1m bar 的 `bucket_start` 由誰負責對齊。

---

### [x] A-04 · v3 缺 `tf` 時默默當成 1m，contract 明文禁止
`bindings/python/src/tradestation_data/wire/el_subscriber.py:290`

`tf = str(data.get("tf", "1m"))` 不分 wire 版本套用預設值。
`contract/compat.md:48-54`：「`tf` 未知時 —— 必須拒收，不得歸入預設……
若以預設值歸檔，該 bar 會進到錯誤的 `timeframe=` 分區……下游再也分不出來」，
且 `contract/v3/bar.schema.json` 把 `tf` 列在 `required`。

**修法**：預設值只在 `version < 3` 時套用；v3 缺 `tf` 直接拒收。

---

### [x] A-05 · `BarWriter` 的 `timeframe` 建構參數已成死碼，但文件還在推銷
`bindings/python/src/tradestation_data/storage/bar_writer.py:91`

`bar.timeframe or self._default_timeframe` 的右運算元永遠到不了 ——
`Bar.timeframe` 預設是非空字串 `"1m"`，且 `_parse_bar` 會擋掉空值 / 未知值。
但 `ParquetBarSink` 仍暴露這個參數，`config/sinks.yaml` 與 `README.md:151` 仍寫著它。

**失效情境**：使用者照文件把 `ParquetBarSink` 設成 `timeframe: 5m`，
設定被靜默忽略，aggregator 產出的 bar 全部寫進 `bars/timeframe=1m/` ——
正好是 `tf` 這套機制要保持乾淨的 native 1 分鐘分區。無 warning、無測試涵蓋。

---

## B. `derived:` provenance 只做了一半

### [x] B-01 · `BarAggregator` 沒有蓋 `derived:` 戳記
`bindings/python/src/tradestation_data/aggregation/bar_aggregator.py:144`（另見 `:158`）

本 PR 讓 `derived:<origin>` 成為區分「算出來的」與「wire 來的」的唯一權威，
但只在 `Resampler` 蓋章。`_new_builder` 仍直接複製 `tick.source`（即 `"tradestation_el"`），
`_empty_bar` 用 `"empty"` —— `is_derived()` 兩者都讀成 native。

**失效情境**：一張 SPY tick 圖（BarType=0 → `BarAggregator` → 1m bar）與一張 SPY 1 分鐘圖
（`EL_PublishBar` → native 1m bar）同時跑，兩條路都經 `ParquetBarSink` 寫進同一個
`bars/timeframe=1m/symbol=SPY/date=*/bars.parquet`，`source` 值一模一樣。
違反 §2.3 rule 2，而且 `_partition_holds_native()` 會把 aggregator 的近似值當成不可覆寫的 native 資料。
**這是寫入量最大的路徑。**

---

### [x] B-02 · `aggregate_parquet.py` 同樣沒蓋戳記
`bindings/python/scripts/aggregate_parquet.py:32,136`

批次 Tier-3 producer 用 `first(source)` 原封不動帶過來源欄位，
與 B-01 同一個洞，但發生在明確標榜「產生 derived 資料」的工具上。

---

### [x] B-03 · `rebuild_bar_cache` 先刪再寫，直接繞過 `_partition_holds_native`
`bindings/python/src/tradestation_data/storage/history_store.py:85,172`

`rebuild_bar_cache` 先呼叫 `_delete_cache` 才呼叫 `_persist_cache`，
所以本 PR 新加的守衛看到的是空目錄。

**失效情境**：`bars/timeframe=1d/symbol=SPY/` 已有 native 1d bar，
呼叫 `rebuild_bar_cache("SPY", start, end, "1d")` 會先 unlink 全部 `bars.parquet`，
守衛通過，tick 加總出來的 daily bar 就地取代 native —— 正是 §2.3 rule 3 禁止的覆寫。
更糟的是 `tests/test_history_store.py:260` 把這個結果寫成**預期行為**
（「rebuild_bar_cache deletes first, so the native bar is gone from disk and the derived one lands」），
等於用一個會過的測試把 regression 鎖住。

---

### [x] B-04 · cache 分區日期用 UTC，`BarWriter` 用 ET —— 守衛看錯檔案
`bindings/python/src/tradestation_data/storage/history_store.py:140`

`_persist_cache` 由 `bucket_start`（UTC）取 `date=`；`BarWriter.write` 由 `bucket_start_et.date()` 取。
EST 期間 19:00 ET 之後的 bar 兩者就分岔。

**失效情境**：2026-01-05 19:00–19:59 ET 的 native 1m bar 由 BarWriter 寫入 `date=2026-01-05/`，
但 cache miss 後 `_persist_cache` 針對的是 `date=2026-01-06/`。
若該檔已有 native → 守衛跳過整天，那些沒有 native 副本的晚間分鐘被丟掉；
若沒有 → 寫入第二份 derived 副本，而 `_load_cached_bars` glob `date=*`，
`load_bars` 於是把那些分鐘回傳兩次。兩種結果都讓守衛的宣稱失效。

> verdict: PLAUSIBLE（其他項為 CONFIRMED）—— 需先確認 EST 晚間資料的實際路徑。

---

### [x] B-05 · `clear_bar_cache` 仍把「非 1m」等同「可重建」，會刪掉 native 日 K
`bindings/python/src/tradestation_data/tools/clear_bar_cache.py:34-35,106`

`TIER3_TIMEFRAMES` / `PROTECTED_TIMEFRAMES = {"1m"}` 以**目錄名**判斷是否為 derived cache，
但本 PR 讓 `BarWriter` 依 `bar.timeframe` 分區，native wire bar（含 `1d`）現在就住在那些目錄裡。

**失效情境**：日線圖 → `EL_PublishBar(bar_type=2, bar_interval=1)` → `tf="1d"` → native bar 落在
`data/bars/timeframe=1d/symbol=SPY/`。之後照文件跑 `python scripts/clear_bar_cache.py --confirm`
（預設涵蓋 5m/15m/30m/1h/1d），`shutil.rmtree` 永久刪除 native 日 K，
下次 `load_bars(..., "1d")` 靜默改用 tick 加總的 `derived:ticks` 近似值 ——
而 §2.3 明說那無法重現交易所官方收盤價與除權息調整。

**修法**：判斷依據改為 provenance（`source` 欄位），不是路徑。

---

### [x] B-06 · `audit_bar_cache` 拿 `Resampler` 跟自己比，而且會污染 native 分區
`bindings/python/src/tradestation_data/tools/audit_bar_cache.py:131`

`_audit_one` 透過會自我修復的 `HistoryStore.load_bars` 讀「現況」那一側，而不是直接讀 Tier-2 parquet。

**失效情境**：稽核某天，而該天的 `bars/timeframe=1m/.../bars.parquet` 遺失或為空
（正是這個工具存在的目的）。`load_bars` miss → `_miss_build_and_return` → `_resampler.resample(...)`，
跟 `_audit_one` 下一行的 `rebuilt` 是同一個呼叫，於是 `_compare_dataframes` 比到兩個相同的 frame，
稽核回報 clean、exit 0，真正的資料遺失沒被回報。
本 PR 之後更糟：`_persist_cache` 會把那些列以 `source="derived:ticks"` 寫進
`data/bars/timeframe=1m/`，一個唯讀稽核工具默默用計算值回填了 **native** tier，
之後 `_partition_holds_native` 就會把那天歸類為可覆寫的 derived 資料。

---

## C. 時間軸 / 對齊

### [x] C-01 · DST 宣稱有誤：derived 的 :30 grid 落在 DST fold 之內
`bindings/python/src/tradestation_data/storage/resampler.py:117`（以及 `contract/semantics.md` §2.2）

註解宣稱 session 錨點落在 DST fold 之外所以來回轉換不會有歧義 —— 但只有**錨點**如此，
往後推導出來的 :30 grid 會掉進 fold 裡。

**已用實機 DuckDB 驗證**：
- 2025-11-02（回撥）：05:15Z（01:15 EDT）與 06:15Z（01:15 EST）相差一小時，
  卻都 bucket 到 `2025-11-02 04:30:00+00` —— 一根「1h」bar 靜默橫跨兩個真實小時。
- 2025-03-09（前撥）：07:15Z（03:15 EDT）bucket 到 `2025-03-09 07:30:00+00` ——
  `bucket_start` 比它所含的 tick 還晚 15 分鐘，破壞 `[t, t+step)` 保證。

任何在轉換日 01:00–03:00 ET 之間有 tick 的 symbol（24 小時 session 設定、或匯入/回補資料）
做 1h/30m/15m/5m resample 都會產出錯誤 OHLC。而這個錯誤宣稱現在已是每個未來 binding 都會照抄的規範文字。

---

### [x] C-02 · `aggregate_parquet.py` 的 bucket 與 `Resampler` 不同格
`bindings/python/scripts/aggregate_parquet.py:25,99`

本 PR 把「左標籤、session 錨定」升格為 contract 規則
（`semantics.md` §2.2、`resampler._bucket_expr`、`verify_parquet._expected_bars` 都改了），
卻沒動 CLAUDE.md 明列的另一個 Tier-3 producer。`_chunk_label` 仍是 epoch 對齊的右標籤。

**失效情境**：`aggregate_parquet.py --timeframe 1h` 寫出 10:00、11:00…（epoch 對齊、右標籤），
`load_bars(..., "1h")` cache miss 時寫出 09:30、10:30…，**兩者進同一個目錄**。
兩個 grid 既不相等也不是固定 offset，任何 `bucket_start` 上的 join 都會靜默錯位。
5m 的情況：`verify_parquet.py --timeframe 5m` 對一份正確建好的批次 cache 會**每天都報 INCOMPLETE**
（09:30 缺、16:00 多），改動前是 OK。

---

## D. Contract 規則未實作

### [x] D-01 · 沒有做 topic 完全相等再過濾，訂 SPY 會吃到 SPYG
`bindings/python/src/tradestation_data/wire/el_subscriber.py:244`

`events()` 解碼 topic 後直接當 symbol 用，沒有與 `self._subscribed` 做字串完全相等比對。
`contract/semantics.md` §5 與 `contract/v3/envelope.md` 都把這條列為 **MUST**
（「ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`」「收訊後以字串完全相等再過濾一次」）。

**失效情境**：`symbols.yaml` 只有 SPY，但同一個 TradeStation 也開著 SPYG 圖。
prefix match 把 SPYG 的每一格都送進 SPY 訂閱，binding 解碼成 symbol `"SPYG"` 並照樣 yield，
於是 `IngestionRuntime` 幫一個從未被訂閱的 symbol 建立 `MarketSnapshot`、
寫出 `data/ticks/symbol=SPYG/` 與 `data/bars/timeframe=1m/symbol=SPYG/`，
`_SequenceTracker` 也開始追一條使用者沒要的流。且無任何 conformance fixture 涵蓋（§7 要求要有）。

---

### [x] D-02 · 無法在公開 API 上分辨「沒掉包」與「無從判斷」
`bindings/python/src/tradestation_data/wire/el_subscriber.py:190`

`semantics.md` §6.6 與 `compat.md` 都寫明 binding **必須**讓呼叫端分辨這兩者，
但參考 binding 只暴露 `messages_lost: int` 加一行 log；
唯一的程式化判別方式是私有屬性 `provider._seq._sid is None`
（conformance test `test_v1_cannot_report_loss_and_must_not_claim_none` 就是這樣伸手進去的）。

**失效情境**：接在仍在線的 ABI-6 / wire-v1 DLL 上的消費端讀到 `messages_lost == 0`，
把整個交易日記錄為「已驗證完整」，實際上 `seq` 根本不存在、gap 偵測整個沒開。

**修法**：公開一個 `sequence_tracking_available` / `messages_lost: int | None` 之類的判別面。

---

### [x] D-03 · 版本檢查在 `observe()` 之前 raise，被拒收的 frame 被算成掉包
`bindings/python/src/tradestation_data/wire/el_subscriber.py:267`

`_parse_payload` 在不支援的 wire 版本上先 raise，`self._seq.observe(...)` 永遠到不了。

**失效情境**：使用者把 TradeStation 裡的 DLL 升到未來的 wire v4（CLAUDE.md 已註明 DLL 不會跟 binding 同步更新）。
每一格都 `ValueError: Unsupported payload version: 4`，`events()` log 完丟棄，
但 `_expected[symbol]` 從未推進。之後第一格被接受的訊息就會報出一個捏造的 `lost` 數字
並灌大 `provider.messages_lost` —— operator 的 gap 儀表板顯示一條其實沒掉任何東西的鏈路在掉包。

---

## E. 文件與實作漂移（CLAUDE.md 點名過的歷史失誤，又發生了）

### [x] E-01 · 多處文件仍寫 ABI 7 / wire v2，同一個 commit 卻出 ABI 8 / wire v3
- `cpp/include/ts2python.h:100-102` — 本 commit **新加**的註解寫「Current pairing is ABI 7 <-> wire 2」，
  而同 commit 設 `kDllVersion = 8`、兩個 publisher 都送 `"v":3`
- `CLAUDE.md:24` — 「Wire v2 / DLL ABI 7 is current」
- `cpp/README.md:170`（與 zh-TW 版 `:171`）— `EL_DllVersion() == 6`，落後兩個 ABI
- `EL/README.md:32-39` — 記載 pre-`tf` 行為：非 1 分鐘 intraday 圖「閒置」、
  「wire 目前寫死 `bar_1m`」、「多 timeframe 將透過 wire 的 `tf` 支援」（未來式），
  但同 commit 的 `.el` 已經在發 1/5/15/30/60m 與日線
- `README.md:25` — 首頁 Mermaid 圖標示 ABI 7 / wire v2
- `docs/architecture.md:296` — 描述 v1 為現行、gap 偵測為「尚未實作的提案」
- `docs/architecture.md:445` — conformance 段落仍指名 `scripts/simple_sub.py`，
  並把 `--record` 說成「待辦」
- `contract/fixtures/README.md:10` — 說 `smoke.jsonl` 是 wire v2 / `bar_1m`，
  但 committed 的檔案是 wire v3 / `kind:"bar"` + `tf`
- `contract/tools/record.py:50` — 自述輸出符合 `contract/v2/envelope.md`
- `bindings/python/src/tradestation_data/wire/el_subscriber.py:123` — class docstring 仍描述 v1 形狀，
  且指向不存在的 `docs/design.md`

唯一寫對的是 `contract/compat.md`（ABI 8 / wire 3 標為「目前」）。
**同一個 diff 內對「現行 wire 版本」給出兩個不同答案。**

---

### [x] E-02 · `contract/error_codes.md` 的 -1 / -2 列漏掉 `EL_PublishBar`
`contract/error_codes.md:13`

`EL_PublishBar` 委派給 `publish_bar_impl`，`g_sock` 為 null 回 -1、`zmq::error_t` / send 失敗回 -2，
但表格「由誰回傳」欄只列了 `EL_PublishTick` / `EL_PublishTickEx`；只有 -4、-5 兩列被更新。
該文件自己的結語規定表格與 `ts2python.h` 必須與程式碼同 commit 更新。

---

## F. Repo 重構的殘留

### [x] F-01 · README 記載的 `pip install git+https://...` 已失效
`bindings/python/README.md:80-82`（zh-TW 版 `:79-84` 同樣）

本 PR 把 `pyproject.toml` 從 repo 根目錄移到 `bindings/python/`，
但安裝說明仍是 `pip install "git+https://github.com/millerlai/tradestation-data-provider.git"`，
現在會以「neither 'setup.py' nor 'pyproject.toml' found」失敗（需要 `#subdirectory=bindings/python`）。
`uv add` / `poetry add` 變體同樣失敗。

下面兩行提供的 `...git@v0.1.0` 逃生口確實能裝 —— 但只因為 v0.1.0 這個 tag 早於重構，
等於靜默安裝一個落後兩版、完全沒有 wire v2/v3 支援的套件。

順帶（同段落）：
- `README.md:110` / `README.zh-TW.md:107` 寫套件版本 0.1.0，`pyproject.toml:7` 是 0.2.0
- `README.md:93` 宣稱「272 tests」，實際 307

---

### [x] F-02 · CI 的 `cache-dependency-glob` 指向已不存在的根目錄檔案
`.github/workflows/ci.yml:37`（兩個 build job 都有）

`astral-sh/setup-uv` 的 `cache-dependency-glob` 相對於 checkout 根目錄解析，
不吃 job 層的 `defaults.run.working-directory` —— 這正是本 PR 兩步之後對 `upload-artifact`
明確寫下的理由（ci.yml:102、release.yml:57）。
重構後根目錄已無 `pyproject.toml` / `uv.lock`，glob 匹配到零個檔案，
cache key 不再包含相依雜湊。Dependabot 更新 `bindings/python/uv.lock` 後 key 不變，
`actions/cache` 拒絕覆寫既有 key，之後每次跑都還原自更新前相依集合建立的 cache。

**修法**：`bindings/python/pyproject.toml` / `bindings/python/uv.lock`（或 `**/pyproject.toml`）。

---

### [x] F-03 · sdist 收了 `tests/` 卻沒收 `contract/`，conformance 必然全掛
`bindings/python/pyproject.toml:81-89`

`tests/conformance/conftest.py:11` 用 `Path(__file__).resolve().parents[4] / "contract"` 定位 fixtures ——
在工作樹正確，但在 sdist 裡版面是 `tradestation_data_provider-0.2.0/tests/conformance/`，
`parents[4]` 會落到解壓目錄的上上層。
`pip download --no-binary :all:` 後解壓跑 `uv run pytest` 會得到 10 個
`FileNotFoundError: contract/fixtures/smoke.jsonl`。

**修法**：把 fixtures 納入 sdist，或在 `CONTRACT_DIR` 不存在時 skip conformance 套件。

---

### [x] F-04 · `contract/tools/record.py` 已不在 CI 的 lint 範圍內
`contract/tools/record.py:1`

CI 的 `uv run ruff check .` / `ruff format --check .` 都帶 `working-directory: bindings/python`，
ruff 不會走到 `contract/`。檔案自己留著 `# noqa: E731`（line 153）就是它以前被 lint 過的證據。
本 PR 新增的 repo 根 `.ruff.toml` 從未被 CI 呼叫。
結果：contract 指定的「唯一與 binding 無關的權威 fixture 錄製工具」失去所有風格 / 型別把關。

---

### [x] F-05 · 被 git 追蹤的 `.claude/settings.local.json` 自動核准 `git push`
`.claude/settings.local.json:20`

`git check-ignore` 回 1、`git ls-files .claude/` 列得到它 —— 這個檔案會散佈給每一位協作者。
本 PR 加入 `"Bash(git push *)"`、`"Bash(git commit -m ' *)"`、`"Bash(uv build *)"`。
任何人 clone 後開 Claude Code 就繼承一份「推遠端不需確認」的專案層允許清單，
與 workflow 規則「Never commit or push unless I explicitly ask」直接衝突。

**修法**：`.local.json` 本來就該是個人的、被 gitignore 的覆蓋層；
真正要共用的搬到 `.claude/settings.json` 並逐條審視。

---

## G. Contract 覆蓋率缺口（本 PR 的主角完全沒被測到）

### [x] G-01 · 沒有任何 harness mode 呼叫 `EL_PublishBar`，也沒有非 1m 的 fixture
`cpp/src/test_harness.cpp:110`

`run_smoke` 與 `run_noquote` 都還在用 legacy 的 `EL_PublishTickEx`，所以每份錄下來的 fixture 都是 `"tf":"1m"`。
`EL_PublishBar`、`wire_timeframe(1,5)→"5m"`、`wire_timeframe(2,1)→"1d"`、`-5` 拒收路徑，
全部零端對端覆蓋，`contract/fixtures/` 裡也沒有任何非 1m 的 bar 樣本。

`contract/semantics.md:298-302` 要求每條新規則都要有 fixture + expected + 消費它的 binding 測試；
CLAUDE.md 寫「Anything a second binding would have to guess belongs in `contract/semantics.md`, with a fixture」。
現在寫 Go binding 的人沒有任何東西可以對照，C++ 端的 mapping 出 regression（例如把 BarInterval 60 對成 `"60m"`）CI 也抓不到。

---

### [x] G-02 · `v1_legacy.jsonl` 沒有半個 0 報價，§3.1 的 MUST 零覆蓋
`contract/semantics.md:161`

§3.1 宣稱「`v1_legacy.jsonl` 就是為此存在」（釘住 v1 的 `bid:0.000000` 規則），
但該 fixture 六格全部帶 `"bid":449.99,"ask":450.01`，`expected/v1_legacy.json:36-37` 也記著同樣的正數。
一個省略了 `<= 0 → absent` 正規化的第二 binding 會通過全部 10 個 conformance 測試，
然後把 v1 歷史回放讀成一長串 $0.00 的買價。

---

### [x] G-03 · `run_noquote` 用的是 index symbol，分不出 §3.1 與 §3.2
`cpp/src/test_harness.cpp:120`

noquote mode 只發兩格：`$TICK` 上的 tick 與 SPY 上的 bar。
`$TICK` 屬於 `DEFAULT_INDEX_SYMBOLS`，`el_subscriber.py:317-318` 會在 `_quote_or_none` 之前就強制
`bid=ask=None`；SPY 那格是 bar，而 `test_wire_conformance.py:80-88` 刻意不對 bar 的 bid/ask 做任何斷言。
結果：一個完全無視 wire 上 `null`、對非 index symbol 直接回 `0.0` 的 binding 照樣通過 `noquote`。

**修法**：fixture 需要一個**非 index symbol 的 tick 且報價為 null**
（例如用 `EL_PublishTick` 發 SPY，bid=ask=0）。

---

### [x] G-04 · semantics.md 宣告為必要的 session 邊界 fixture 不存在
`contract/semantics.md:62`

該行寫「**conformance fixture 必須涵蓋 session 首尾兩根 bar。**」，
但 `contract/fixtures/` 只有 smoke / noquote / v1_legacy，三者唯一的 bar 都在 13:30 ET，
既不是 09:30 也不是 15:59。§2.2、§2.3、§5、§6.3–6.5 同樣沒有任何 fixture。
`contract/fixtures/README.md:52-56` 自己承認這個缺口並歸咎於 harness 參數寫死。

也就是說：該文件稱為「市場資料最典型的靜默錯誤」的左/右標籤問題
—— 本 PR 才剛在 `verify_parquet.py` 再修一次 —— 仍然沒有任何 fixture 擋得住。

---

### [x] G-05 · `contract/v1..v3` 的 JSON Schema 從未被執行
`contract/v3/tick.schema.json`（以及 v1/v2 全部，約 570 行）

全 repo（`.venv` 外）grep 不到 `jsonschema` 或 `schema.json` 的使用；
`tests/conformance/` 只讀 `fixtures/*.jsonl` 與 `fixtures/expected/*.json`。
v3 每個 schema 都設了 `additionalProperties: false` 與 `required`，
所以 DLL 一旦新增或改名欄位，schema 就變成錯的而沒人會發現 ——
正是 `contract/README.md:50` 承諾要防的漂移
（「契約與實作放在同一個 repo、且被 conformance fixtures 綁住，就是為了讓這種漂移在 CI 就爆掉」）。

---

### [x] G-06 · conformance 直接打私有 `_parse_payload`，公開接收路徑從未被驗
`bindings/python/tests/conformance/test_wire_conformance.py:32`

`_decode_all` 建 `TradeStationELProvider()` 後逐格呼叫 `provider._parse_payload(topic, payload)`，
完全繞過 `events()`。後果：
- §5 的 topic 完全相等再過濾沒有地方可以被檢查（而且它確實沒實作，見 D-01）
- `compat.md` 的「`kind` 未知時……跳過該訊息並記錄，不得拋錯」只由 `events()` 的 except 子句滿足，
  而測試從不跑到那裡，所以 `_parse_payload` 直接 raise 與符合規範的 skip 在測試上無從區分
- module docstring 自稱是「未來 Go / Rust binding 要滿足的樣板」，
  照著讀的人會以為契約止於 payload 解碼

---

## H. C++ / EL 端

### [x] H-01 · `g_sid` 只有秒級精度，同一秒內重啟無法被偵測
`cpp/src/ts2python.cpp:238`

`g_sid = recv_unix_seconds()` 截到整秒，同一個 wall-clock 秒內啟動的兩個 publisher session 無法區分。

**已對 `_SequenceTracker` 驗證**：SPY 已跑到 seq=5 的 session 若在同一秒內重啟
（test harness 重跑，或腳本裡 `EL_Shutdown` + `EL_Init`），`sid` 相同，
`observe()` 對新 session 的 seq 1/2/3 走 `seq < expected` 分支，把 `_expected['SPY']` 釘在 6。
新 session 前五格中真正掉的訊息完全不可見 —— `messages_lost` 仍讀 0，
而 §6.6 說呼叫端可以把它讀成「已驗證乾淨」。
偵測要等到新 session 的 seq 超過舊期望值才恢復，期間 log 被 `sequence_regressed` 灌爆。

---

### [x] H-02 · 不支援的圖表區間每根 bar 都印一次診斷，與檔案自述矛盾
`EL/TS2Python_Exporter.el:149`

`UnsupportedLogged` 只擋住 138–146 行那則 -5 訊息，接著就落到 149 行沒有 once-guard 的
`If PubRC < 0 and LogErrors Then Print(...)`。
但檔頭 13 行承諾「Anything else is refused by the DLL with rc = -5 and logged once」，
-5 訊息本身也寫「Indicator is idle on this chart」。
掛到 2 分鐘或週線圖（`LogErrors` 預設 True）：TradeStation 的 Print Log 每根 bar 收到五行訊息，
「update every tick」模式下是每個 tick 一次，把 operator 用來看真錯誤的 log 灌爆。

---

### [x] H-03 · 秒級圖表可能被當成 1 分鐘發布，只有 README 警語擋著
`EL/TS2Python_Exporter.el:25-30`

檔頭 KNOWN GAP 自述：秒級圖表也可能回報 `BarType = 1` + `BarInterval = 1`，
那樣 1 秒 bar 會被當 1 分鐘 bar 發出，且 `BarType_ext` 從未對 live 安裝驗證過。
`.el`、DLL 的 `wire_timeframe`、binding 三邊都沒有擋，唯一的緩解是散文式的「Use minute or daily charts only」。
拖到 1 秒圖上就會把 `bars/timeframe=1m/` 填滿帶著分鐘形狀時間戳的 1 秒 bar ——
正是同一份檔頭（line 22）說這套設計要防的「下游無從偵測」型腐蝕。

**兩條可行路**：DLL 要求呼叫端傳 `BarType_ext`、無法區分時回 -5；或 wire 帶上原始 `bar_type`/`bar_interval` 讓訂閱端自己判斷。

---

## I. 內部一致性 / 清理

### [x] I-01 · timeframe 詞彙有五份各自獨立的定義
`bindings/python/src/tradestation_data/wire/el_subscriber.py:45`

同一組字串現在散在：`SUPPORTED_TIMEFRAMES`、`storage/resampler.py`（`Timeframe` StrEnum、`_INTERVALS`、
`_MINUTES`、本 PR 新增的 `_ANCHOR_LOCAL`）、`tools/clear_bar_cache.py:34`（`TIER3_TIMEFRAMES`）、
`scripts/aggregate_parquet.py:71`（`_TF_MINUTES`，漏了 `1d`）、`cpp/src/ts2python.cpp:wire_timeframe`、
`contract/v3/bar.schema.json` 的 enum。
41–44 行的註解寫明了意圖（「Deliberately the same vocabulary the storage layer partitions on」），但沒有任何機制強制。

加一個 `4h` 要改六處：漏了 `_ANCHOR_LOCAL` → `_bucket_expr` 查詢時 KeyError；
漏了 `_MINUTES` → `timeframe_to_minutes` 爆；漏了 `TIER3_TIMEFRAMES` → cache 清理工具永遠留下那個分區。
既有的 `Timeframe` StrEnum 就是天然的單一定義來源。

---

### [x] I-02 · `_SequenceTracker.observe` 的回傳值沒有任何 production 呼叫端讀
`bindings/python/src/tradestation_data/wire/el_subscriber.py:68`

`observe` 文件寫「return how many were lost immediately before it」且有三條不同的 return path，
但唯一的 production 呼叫端（`:271`）把回傳值丟掉；只有 unit test 在讀。
公開面是 `messages_lost` 這個方法自己已在維護的累加器。
維護者會以為這個回傳值接到了什麼東西（例如在呼叫端讀它做 per-gap 處理），
實際上 gap 早已在方法內部 log 過也計過了。

---

## 判定為誤報（verifier 已駁回，不需處理）

| 位置 | 宣稱 | 駁回理由 |
|------|------|----------|
| `contract/tools/record.py:103` | `--record` 說是 append 卻用 `"w"` 截斷 | 兩位 finder 都提，verifier 駁回 |
| `cpp/src/ts2python.cpp:64` | `reserve_seq` 在 mutex 內為 symbol 配置 `std::string`，熱路徑多一次 heap 配置 | 效能影響不成立 |
| `bindings/python/src/tradestation_data/wire/el_subscriber.py:409` | `_floor_to_minute_utc` 結尾有無意義的 `- timedelta(0)` | 讀錯程式碼 |
| `bindings/python/scripts/aggregate_parquet.py:99` | （與 C-02 同一項的重複回報） | 去重時判為重複 |
