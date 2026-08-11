# `on_partial_bar` code review 缺陷清單

日期：2026-08-11 · 範圍：`feat/partial-bar-callback` 相對 `main` 的未提交變更
基準：`625c9f9` · 設計文件：[`partial-bar-callback-2026-08-11.md`](partial-bar-callback-2026-08-11.md)

Review 方式：4 個 finder 角度產出 24 個候選，17 個獨立 verifier 逐條查證，
6 條被駁回、10 條存活。所有行號已人工複核。

被駁回的 6 條中有 2 條質疑「不寫進 `contract/semantics.md`」的豁免理由，
兩個 verifier 各自獨立駁回，理由與設計文件 §3.6 一致：contract 連 closed 側的
callback 都沒規範，partial 不會讓第二個 binding 觀察到任何語義差異。

---

## 總覽

| # | 缺陷 | 類別 | 位置 | 狀態 |
|---|---|---|---|---|
| R1 | `async def` callback 被靜默丟棄 | 正確性 | `ingestion.py:431` | ✅ 已修（程式） |
| R2 | 冷啟動的歷史重播被當成「發展中」 | 正確性 | `ingestion.py:334` | ✅ 已修（文件） |
| R3 | Sub-minute chart 的 partial 永遠不會經 `on_bar` 抵達 | 正確性 | `ingestion.py:376` | ✅ 已修（文件） |
| R4 | 例外 latch 永久封口所有後續失敗 | 正確性 | `ingestion.py:434` | ✅ 已修（程式） |
| R5 | `messages_lost` 是錯的診斷指標 | 正確性 | `ingestion.py:422` | ✅ 已修（文件） |
| R6 | partial 的 `bar_time` 在未來，未記載 | 正確性 | `ingestion.py:403` | ✅ 已修（文件） |
| R7 | 多筆數 tick chart 永遠不觸發 partial | 正確性 | `ingestion.py:87` | ✅ 已修（文件；行為另案） |
| R8 | advance / shutdown 收盤路徑沒有測試 | 測試覆蓋 | `test_ingestion_runtime.py:836` | ✅ 已修 |
| R9 | `docs/architecture.md` 的圖沒更新 | 文件 | `docs/architecture.md:499` | ✅ 已修（含 zh-TW） |
| R10 | 同一份契約重複五處 | 簡化 | `ingestion.py:404` | ✅ 已修 |

**修正結果**（2026-08-11）：10 條全數處理完畢，293 個測試通過，ruff / mypy 乾淨。

Review 沒抓到、但修正時一併處理的一項：`docs/architecture.zh-TW.md` 有與
`docs/architecture.md` 完全對應的兩張圖，R9 只修英文版等於把同類漂移留下一半。

新增的測試（4 個）：`test_async_callbacks_are_refused_at_construction`（R1）、
`test_partial_failure_latch_is_keyed_by_series_and_exception_type`（R4）、
`test_partial_precedes_close_on_the_wall_clock_path` 與
`test_partial_precedes_close_on_the_shutdown_path`（R8）。

---

## R1 — `async def` callback 被靜默丟棄

`ingestion.py:431` 以 `self._on_partial(bar)` 同步呼叫。使用者若傳
`async def`，回傳的 coroutine 物件會被直接丟棄：callback 一次都沒執行，
而 `bars_partial_out` 把每一幀都算成「已送達」、`partial_callback_failed`
維持 0、沒有任何 log。

**這個 diff 自己把使用者推向這個坑。** `ingestion.py:83-85` 與兩份 README
都警告「callback 太慢會回壓 ZMQ recv」，而看到這句話最直覺的反應就是寫成
`async def`。型別註記 `Callable[[Bar], None]` 只有在使用者對自己的程式碼跑
mypy 時才擋得住；本 repo 的 mypy strict 只涵蓋 `src/`。

`on_bar`（`ingestion.py:399`）有同樣的坑，但它一根 bar 錯一次；partial
是每幀一次，使用者看到的是整條通道全死，外加無上限的 RuntimeWarning。

**修法：** 建構時以 `asyncio.iscoroutinefunction()` 檢查，是 coroutine
function 就拋 `TypeError` 並在訊息裡指名修法。零熱路徑成本、失敗發生在
建構而非執行期。`on_bar` 一併加上 —— 兩個相鄰的參數只有一個有防護會更糟。

## R2 — 冷啟動的歷史重播被當成「發展中」

`_last_emitted_direct_bucket` 在 process 剛啟動時是空的，所以
`ingestion.py:327` 的 `<= last_emitted` 閘門完全不作用。TradeStation 圖表
重載時整天的歷史會走過 buffer，每一根都經由 `ingestion.py:334` 觸發
`on_partial_bar`，被宣告成「現在正在發展的那根」。

消費端如果依 partial 做即時決策 —— 也就是文件寫的用途 —— 會在啟動瞬間收到
數百根帶著數小時前價格的 bar。

**修法：文件，不是程式。** wire 上沒有任何「這是重播」的訊號，發明一個啟發式
判斷等於在傳輸層決定資料的意義，正是本 repo 一再拒絕的事。誠實的做法是記載
這個行為，並指出消費端手上唯一可靠的判別依據：把 `bar_time` 跟牆鐘比較 ——
真正發展中的 bar，`bar_time` 在**未來**（見 R6）。

## R3 — Sub-minute chart 的 partial 永遠不會經 `on_bar` 抵達

三根共用同一個分鐘級 `bar_time` 的 1 秒 bar，會產生 partial 三次、closed 零次，
`bars_subminute_suspected` 加 2。partial 在 `ingestion.py:376` 觸發，而那正是
`ingestion.py:356-373` 剛剛為同一批 frame 記下 `subminute_chart_suspected` 的分支。

消費端照文件的說法 —— `ingestion.py:76-79`「the same bar arrives once more
through `on_bar` when it closes」、CLAUDE.md「the two channels cannot drift
apart」 —— 會把 partial 當暫定值、只保留 `on_bar` 確認過的，於是丟掉 60 根裡的
59 根，而 partial 其實是唯一載過它們的通道。

**底層的遺失是既有行為**（且已有 warning）。這個 diff 新增的缺陷是**文件宣稱
它不會發生**。

**修法：** 修正文件，明說 sub-minute chart 上 partial 會攜帶 `on_bar` 永遠不會
交付的 bar，並與既有的 `subminute_chart_suspected` warning 互相引用。

## R4 — 例外 latch 永久封口所有後續失敗

`_partial_callback_failure_logged` 是一個全 process、永不重置、不分例外種類的
bool。第一次失敗之後，所有後續失敗都不再寫任何一行 log。

情境：callback 在啟動後第一幀拋了一個無害的 `KeyError`（暖身 cache miss）。
幾小時後同一個 callback 因為真正的原因開始每幀都拋 —— 檔案 handle 關了、
schema 變了、下游掛了。一行 log 都不會有。唯一的證據是 60 秒 heartbeat 裡的
`partial_callback_failed`：一個沒有 symbol、沒有例外型別、沒有 stack 的累計整數。

相鄰的 `_subminute_warned` 正是為了避免這個性質才做成 per-series 的；
`on_bar`（`ingestion.py:401`）則是每次都記。

**修法：** 改成 `set[tuple[key, exception_type_name]]`。per-series 單獨並不能
解決「無害的例外封住後來真正的例外」，把例外型別一起放進 key 才行，而成本
一樣是一行。

## R5 — `messages_lost` 是錯的診斷指標

`ingestion.py:422-425`、CLAUDE.md 與兩份 README 都把 `messages_lost` 指為
「callback 太慢」的徵兆。但兩端的佇列都刻意開得很大：DLL 的 XPUB 是
`sndhwm 100000`（`cpp/src/ts2python.cpp:570`，其註解自陳在 50 tps 下可撐約
30 分鐘的 SUB 停滯），SUB 是 `RCVHWM 1_000_000`（`wire/el_subscriber.py:303`）。
在那整段期間內什麼都不會被丟，`messages_lost` 讀出來是 0。

而**先造成傷害的是被餓死的 event loop**：`_advance_loop`、`_flush_loop`、
`_heartbeat_loop` 共用同一個 loop。`_advance_direct_bars` 是唯一會收掉安靜
symbol 最後一根 bar 的東西；`_flush_loop` 停擺代表 `ParquetBarSink.flush()`
不再被驅動，開著的 partition 沒有 footer、對任何 reader 都不可讀；而操作者
被告知要盯的 heartbeat 本身也被延遲。

操作者照文件去看 `messages_lost: 0`，把這段 session 判定為健康。

**修法：** 文件改成誠實的說法 —— 先壞的是 flush 與 advance，`messages_lost`
要等 HWM 填滿之後才會動。

## R6 — partial 的 `bar_time` 在未來，未記載

`bar_time` 是 bar 的收盤時間，而 EL 給發展中的 bar 蓋的是它「將要收在」的時間
（`ingestion.py:245-251` 明講，`_advance_direct_bars` 的 `bar_time + grace <= now`
判準也只有在這個前提下才成立）。

於是 `on_bar` 的消費端永遠只看到 `bar_time <= now`，而 `on_partial_bar` 的消費端
幾乎每一次都看到 `bar_time > now` —— 5 分鐘圖上超前 5 分鐘，`bar_type 2` 的日線
上超前一整個交易日。

消費端若用 `bar_time` 當 key 畫即時 K 線，發展中那根會被畫到最後一根收盤 K 的
右邊一格，中間留一個永久的洞；若套用 `if bar.bar_time > now: skip` 這種合理性
過濾（對 `on_bar` 是正確且無作用的），會靜默丟掉 100% 的 partial，而 heartbeat
的 `bars_partial_out` 照常上升。

這是 partial 通道獨有的新前提，diff 新增的四個文件面沒有一個提到。

**修法：** 記載它。同時它也是 R2 的判別依據。

## R7 — 多筆數 tick chart 永遠不觸發 partial

`ingestion.py:320` 的豁免只看 `bar_type == 0`，沒看 `bar_interval`。100 筆
tick chart（`bar_type` 0、`bar_interval` 100，wire 合法且無人拒絕）註冊
`on_partial_bar` 會一次都不觸發。

而 `ingestion.py:87-88` 的 docstring 說「every print on such a chart is already
a finished point, so there is no partial state to report」，這句只在
`bar_interval == 1` 時成立。

**Reviewer 另外主張**該圖每一次 intra-bar 更新都會被當成完成的 bar 送進
`_on_closed_bar`，於是每根真 bar 產生上百列 Parquet。這是**既有且刻意**的行為
（CLAUDE.md：「Tick charts bypass the buffer entirely」），而且 TradeStation
在 tick chart 上到底會不會重送 intra-bar，本次 review 並未實測。

**修法：** 只修 docstring 的過度宣稱。改動 tick chart 的緩衝策略超出本 diff
範圍，且前提未經驗證 —— 另案處理。

## R8 — advance / shutdown 收盤路徑沒有測試

`test_every_closed_bar_was_partial_first`（`test_ingestion_runtime.py:836`）
只走 rollover，收尾是手動呼叫 `runtime._drain_direct_bars()`，繞過了
`_advance_loop` 與 `_shutdown()`。

日後若有人改動 `_advance_direct_bars`，讓它重建、重新緩衝或合成它釋放的那根
bar，closed 串流就會出現 partial 從未宣告過的 bar，而整個 suite 依然全綠。
實務上，用 partial 建立即時 K 線、用 `on_bar` 定案的消費端會定案一根它從未
建立過的 K —— 而且正好發生在只會經由牆鐘路徑收盤的安靜標的上（breadth 指數、
成交稀疏的選擇權）。

**修法：** 補上涵蓋 `_advance_direct_bars` 與 `_shutdown()` 的測試。

## R9 — `docs/architecture.md` 的圖沒更新

partial 通道寫進了 CLAUDE.md 與兩份 `bindings/python/` README，但漏了
`docs/architecture.md`。它的 §7.2 至今仍把 buffer 的唯一出口畫成 closed：

```
docs/architecture.md:499  INGEST -->|"see the §7.3 decision diagram"| CLOSED["_on_closed_bar()"]
docs/architecture.md:500  CLOSED --> SNAP["MarketSnapshot.on_bar()"]
docs/architecture.md:501  CLOSED --> PIPE["SinkPipeline.on_bar()"]
```

§7 也只列了 `on_bar`。那份檔案是維護者與第二個 binding 作者被指去讀的深度
架構參考。讀它的人不會知道 partial 通道存在，於是要嘛重造一個已經存在的
路徑，要嘛把 partial 導進 `SinkPipeline` 的 sink —— 把發展中的 bar 寫進
Parquet，正是新段落宣稱這個設計要讓它不可能發生的那件事。

**修法：** 更新 §7.2 的圖與 §7 的列舉。

## R10 — 同一份契約重複五處

同三條規則現在有五份近乎逐字的副本：

| 規則 | 副本 |
|---|---|
| 回壓警告 | `ingestion.py:83-85`、`:422-425` |
| 不進 snapshot/sinks/storage | `ingestion.py:80-82`、`:417-420` |
| tick chart 不觸發 | `ingestion.py:87-88`、`:413-415` |

再加上 CLAUDE.md 與兩份 README。約 45 行散文守著一個 10 行的方法。

當觸發規則改變時（例如日後加節流，或 partial 也從 `_advance_direct_bars`
觸發），編輯者更新其中一兩份，其餘就靜默地變成錯誤文件 —— 這正是 CLAUDE.md
記載已經發生過一次的漂移（"this repo has already had a spec drift into
describing fields the DLL no longer emitted, unnoticed"）。

**修法：** 契約只留在 `_on_partial_bar` 的 docstring，class docstring 與
`_handle_provider_bar` 改成指向它（`ingestion.py:318` 已經是這樣做的）。
R3 / R5 / R6 / R7 的文字修正併入這次收斂，避免同一段話改五遍。

---

## 修正順序

1. **R1** —— 唯一會造成靜默資料遺失的
2. **R4** —— 唯一會造成靜默故障的
3. **R10 + R2/R3/R5/R6/R7** —— 契約收斂到單一處，同時把四條不實陳述改對
4. **R9** —— `docs/architecture.md`
5. **R8** —— 補測試（含 R1 / R4 的迴歸測試）

R7 只修文件，tick chart 的緩衝策略另案。
