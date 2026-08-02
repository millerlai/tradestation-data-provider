# 修正計畫 — 把 bar 的起點放上 wire,並修正量值 fixture

**Branch:** `verify/live-tradestation-proto-1`
**起點:** `42cf9a8`(main,PR #30 合併後)
**日期:** 2026-08-02

起因是一次 live 驗證:SPY 掛 5m/15m/30m/1h/1d,資料 2026-07-27~31。5m/15m/30m/1d
正確;1h 有 24/42 根錯,其中每天固定遺失一根完整小時。追查過程另外揭露兩件事,
其中一件是我在 F10(`dc81146`)寫進 main 的錯誤。

---

## A. 已證實的事實

每一條都有證據,不是推論。

### A1. TradeStation 的 1h 在區段邊界重啟格線

07-31 的 `LogPublish` 顯示 1h **每天發 15 根**,收盤時間:

```
07:00 08:00 09:00 09:30 10:30 11:30 12:30 13:30 14:30 15:30 16:00 17:00 18:00 19:00 20:00
```

用已落地的 30m 逐根反推,得到實際涵蓋區間 —— 連續、不重疊、覆蓋 06:00–20:00:

```
06:00–07:00  07:00–08:00  08:00–09:00  09:00–09:30 ←殘根(盤前段尾)
09:30–10:30  10:30–11:30  11:30–12:30  12:30–13:30  13:30–14:30  14:30–15:30
15:30–16:00 ←殘根(RTH 段尾)
16:00–17:00  17:00–18:00  18:00–19:00  19:00–20:00
```

驗證方式:每根 1h 都能由 1 或 2 根連續 30m 精確合成(O/H/L/C 與 `el_volume` 全等)。
最直接的一筆:log 裡收盤 09:30 的 1h 與收盤 09:30 的 30m **逐欄位元相同**,證明那根
1h 只有 30 分鐘。

### A2. 每天遺失一根完整小時

15 根發出、14 個標籤落地。`close 09:00 → 08:30` 與 `close 09:30 → 08:30` 撞同一格,
後到的殘根覆蓋前者。

磁碟證據:落地在 `08:30` 的值是 `o=743.14 c=744.68 vol=177115` —— 那是 log 裡收盤
09:30 的殘根;而完整的 08:00–09:00(`o=744.68 c=743.09 vol=128573`)在整個
`timeframe=1h` 下**找不到任何一列**。

### A3. 只有 1h 中招,原因是區段長度

使用者的 chart session 是 06:00–20:00,RTH 在 09:30 / 16:00 重啟,三段長度:

| 區段 | 長度 | 5m | 15m | 30m | 60m |
| --- | --- | --- | --- | --- | --- |
| 盤前 06:00–09:30 | 210 | 整除 | 整除 | 整除 | **餘 30** |
| RTH 09:30–16:00 | 390 | 整除 | 整除 | 整除 | **餘 30** |
| 盤後 16:00–20:00 | 240 | 整除 | 整除 | 整除 | 整除 |

30m 的 log 有 28 根、收盤 06:30→20:00、**完全沒有殘根**,所以現行的
`align(close − 1min, tf)` 全部命中。這不是運氣,是 30 整除三段。

**`210` 來自使用者的 chart 設定,不是固定值。** 這是「binding 無論如何都推算不出來」
的關鍵 —— 任何格線規則都猜不到 210。

### A4. `el_open_interest` 帶的是 `el_downticks` 的值

盤中每一列都成立,已寫進 Parquet:

```
5m n=839 / 15m n=280 / 30m n=140 / 1h n=69   open_interest==downticks: 全部
                                              open_interest==0       : 0 列
1d n=499                                      open_interest==0       : 全部
```

**publisher 沒有錯。** `EL_PublishBar` 的引數順序正確(`Volume, Ticks, UpTicks,
DownTicks, OpenInt`),而 `LogPublish` 的 Print 讀的是同一批 EL 變數、在 DLL 之前
就印出 `openint == downticks`。所以是 **EasyLanguage 的 `OpenInt` 保留字本身**在這
張圖上回傳了 `DownTicks` 的值。依 repo 規則(逐字轉發、逐字落地),存下它是正確的;
但這是 §3.4 必須記的事實。

### A5. `Ticks == UpTicks + DownTicks` 是恆等式 —— 我在 F10 弄錯了

```
5m 839/839   15m 280/280   30m 140/140   1h 69/69      ticks == up+down
1d 499/499                                             ticks == volume
```

F10(`dc81146`)我把 harness 改成 `Ticks` **嚴格大於**兩者之和,理由是「價格不變的
成交兩邊都不算」。真實資料說那是恆等式。**原本的 fixture(`100/180/100/80`)是忠實的,
我把它改成不忠實。** 日線同理:我寫 `kDailyQty` 的 `ticks=612345 ≠ volume`,實測是
`volume == ticks == upticks, downticks == 0`。

F10 的診斷(fixture 抓不到欄位互換)沒錯,處方錯了 —— 用發明的數字換可測試性,正是
這個 repo 明令 fixture 不能做的事。

### A6. ABI 一定要改名

`dumpbin` 顯示 `EL_PublishBar = _EL_PublishBar@88`。
88 = symbol(4) + el_timestamp(4) + bar_type(4) + bar_interval(4) + OHLC(32) + 五個量值(40)。
多一個 `const char*` → `@92`。`__stdcall` 由被呼叫端清堆疊,簽章不符會**弄壞堆疊**而
不是回傳錯誤,所以必須沿用 `EL_Init3` 的做法:新名 + 舊名留墓碑回 `-6`。

---

### A7. `Category` 是 wire 缺的那個維度

EasyLanguage 的 `Category` 保留字回傳商品分類(TradeStation 官方值表):

```
0 Future    1 Future Option   2 Stock      3 Stock Option   4 Index
5 Currency Option   6 Mutual Fund   7 Money Market Fund   8 Index Option
9 Cash   10 Bond   11 Spread   12 Forex   13 CPC Symbol   14 Composite
18 Stock CFD   19 Forex CFD   20 Index CFD   21 Future CFD
```

實作細節:官方說明要求**先指派給數值變數**才能取值(`Value1 = Category;`),不能
直接內嵌使用。

**wire 現在沒有任何欄位說得出一根 bar 來自哪一類商品**,而至少三條既有規則其實都
依賴它:

| 既有規則 | 現在怎麼決定 | 問題 |
| --- | --- | --- |
| §3.4 的 Volume/Ticks 盤中↔日線互換表 | 文件註明「TradeStation 官方定義(**股票商品**)」 | 只保證對 `Category=2` 成立,wire 說不出來 |
| index/breadth 的報價無效化 | `el_subscriber.py:41` 硬編碼 6 個字串 | 沒列到的指數會把無意義的 bid/ask 存成真報價 |
| `el_open_interest` 的意義 | 假設「股票是 0」 | 該假設已被 A4 推翻 |

**必須在 publisher 分支之外解決。** 若指標用 `Category` 決定「送 OpenInt 還是送 0」,
落地的 0 就分不出是「TradeStation 說 0」還是「指標判斷後填的 0」—— 那正是 `pv` /
`publisher_version` 長出來的模式,`.el` 第 264–283 行的註解記著它的代價。正確做法是
**把 `Category` 本身當第六個逐字欄位送上 wire**,判斷留給消費端與 semantics.md。

### A8. `Category` 的實測值(2026-08-02,live)

```
category=2  SPY              openint=33109    downticks=33109   ticks=78961     volume=45852
category=0  @ES              openint=1370     downticks=1370    ticks=3023      volume=1653
category=4  $TICK            openint=0        downticks=0       ticks=0         volume=0
category=4  $VIX.X           openint=0        downticks=0       ticks=0         volume=0
category=2  VXX              openint=582719   downticks=582719  ticks=1150495   volume=567776
category=4  $ADD             openint=0        downticks=0       ticks=0         volume=0
category=4  $VOLD            openint=0        downticks=0       ticks=0         volume=0
category=4  $TRIN            openint=0        downticks=0       ticks=0         volume=0
category=3  SPY 260803C745   openint=158      downticks=158     ticks=539       volume=381
```

推得四條:

1. **`openint == downticks` 在每一個分類都成立,期貨也不例外。** @ES 是真正有未平倉量
   的商品,前月 ES 的 OI 是**百萬量級**,而這裡是 1370 —— 恰好等於該根 bar 的
   `DownTicks`。這殺死了「SPY 沒有 OI 所以 TS 回傳垃圾」的解釋:現象與商品類別無關,
   是結構性的。**我們讀到的不是未平倉量。**
2. **`ticks == volume + downticks` 在股票 / 期貨 / 選擇權全部精確成立**(78961、3023、
   1150495、539 四筆逐一驗算)。A5 的恆等式不限於股票,我在 F10 的改動是全面錯誤。
3. **`VXX` 是 `category=2 (Stock)`,不是 Index。** 它有 567,776 的成交量,是可交易的
   ETN。而它在 `DEFAULT_INDEX_SYMBOLS` 裡,所以 `el_subscriber.py:450-451` 正在
   **無條件丟棄 VXX 真實的 bid/ask**。假說證實,這是一個現行的資料遺失 bug。
4. **所有 breadth/index 都是 `category=4` 且五個量值全為 0。** 分類能乾淨地識別它們,
   硬編碼清單可以改由 wire 事實驅動。

`el_subscriber.py:444` 的註解寫著:

> the DLL cannot do the second — it holds no symbol taxonomy.

**這句現在不成立了。** `Category` 就是 symbol taxonomy,而且 TradeStation 主動給 EL。

---

## B. 未證實 —— 需要你提供,我不猜

| # | 問題 | 為什麼擋住計畫 | 怎麼取得 |
| --- | --- | --- | --- |
| B1 | EL 的 `Time[1]` / `Date[1]` 是否真的回傳「上一根 bar 的收盤日期/時間」? | **整個 Phase 2 的承重假設。** A1 證明的是「區段連續」,不是 `[1]` 在這個 indicator 的執行環境下的行為 | 跑那支只 Print 的探針,或你直接確認 |
| B2 | 每天第一根是否**必定**是完整的一個 `tf`? | 決定「當日第一根用 `close − tf`」能不能成立。目前只有一組 session 設定的一個觀測 | 把 session 起點改成不整除的值(例如 06:15)重掛 1h,貼 log |
| B3 | EL 有哪些 session 保留字可用(`Sess1StartTime`?)、名稱與回傳格式為何? | 若 B2 不成立,就需要把 session 起點也放上 wire。我不確定字彙,亂寫會 Verify 失敗 | 你在 EL Editor 確認 |
| B4 | `1d` 圖上 `Time` / `Time[1]` 是什麼? | 決定 1d 要不要也帶起點,還是維持 04:00 ET 錨定 | 1d 圖開 `LogPublish` 貼 log |
| ~~B5~~ | ~~裸寫的 `OpenInt` 讀到什麼~~ | **已答,見 A9** | — |
| ~~B6~~ | ~~1m 為什麼沒有資料~~ | **已答** —— 使用者沒有掛載 1m 圖,不是 bug | — |
| ~~B7~~ | ~~`Category` 實際回傳什麼~~ | **已答,見 A8** | — |

---

## C. 分階段

### Phase 0 — flush 修正(**已完成,未 commit**)

`BarWriter` 加上第二個 seal 觸發:該 ET 日已過完 **且** 已一整個 `max_flush_seconds`
沒有新 bar。修的是「replay 完最後一天要等 Ctrl+C 才可讀」。

安裝安全網的理由記在 code 裡:只看牆鐘會在 replay burst 中途 seal,而 `write` 對
sealed 分區是拒收 —— 那會把「讀不到」變成「掉資料」。既有測試
`test_should_flush_triggers_on_buffered_count` 抓到了第一版的這個缺陷。

`324 passed` / ruff / ruff format / mypy 全清。**不依賴 B 區任何答案,可立即 commit。**

### Phase 1 — F10 量值修正(**不依賴 B1–B4,可立即開始**)

改 `cpp/src/test_harness.cpp` 的三組常數,改回真實形狀:

| 常數 | 現在(我 F10 寫錯的) | 改成 | 依據 |
| --- | --- | --- | --- |
| `kTickQty` | `{100, 195, 100, 80, 0}` | `{100, 180, 100, 80, 80}` | A5 恆等式 + A4 |
| `kBarQty` | `{12000, 22500, 12000, 9000, 0}` | `{12000, 21000, 12000, 9000, 9000}` | 同上 |
| `kDailyQty` | `{88400000, 612345, 88400000, 0, 0}` | `{70698008, 70698008, 70698008, 0, 0}` | A5 日線列 |

> `kDailyQty` **存在**這件事是 F10 對的部分 —— 日線的形狀確實與盤中不同(`downticks=0`),
> 原本 fixture 在 `1d` 上發盤中形狀是真的錯。錯的只有那組數字。

`kTickQty` / `kBarQty` 的 `open_interest` 是否要設成 `downticks`,**擋在 B5**。
在 B5 有答案前,兩個選擇都不安全:設 0 不忠實,設 `downticks` 可能把一個只在股票上
成立的 TS 怪癖烤進 conformance fixture。

同時重寫 `contract/semantics.md` §3.4 的「fixture 抓得到什麼」表 —— F10 我寫的版本
過度樂觀:

| 錯誤實作 | fixture 抓得到嗎 |
| --- | --- |
| `el_volume` ↔ `el_upticks` 互換 | ❌ 兩種régime都相等 |
| `el_ticks` 由 `up+down` 算出 | ❌ **恆等式成立,所以永遠抓不到**(F10 我聲稱抓得到) |
| `el_open_interest` 抄 `el_downticks` | ❌ 盤中兩者相等;日線兩者皆 0 |
| `el_ticks` ↔ `el_downticks` 互換 | ✅ |
| 任一量值 ↔ OHLC 互換 | ✅ |

結論要寫進去:**這幾欄不是 fixture 守得住的,只能靠 `el_` 命名與 code review。**
假裝守得住比坦承守不住更危險。

驗證:重錄 4 份 fixture、手推 `expected/*.json`、mutation 重跑(預期
「算出 `el_ticks`」這一項**會**通過 —— 那正是誠實的結果)。

### Phase 2 — proto 2 / ABI 2(**擋在 B1;B2/B3 決定要送幾個欄位**)

若 B1 成立且 B2 成立(段從段首切,第一根必完整):

```
EL          TsStrPrev = Date[1] + Time[1]        ← 新增,一樣逐字
C ABI       EL_PublishBar2(..., const char* el_timestamp_prev)   @92
            EL_PublishBar 留墓碑回 -6            ← 堆疊安全
wire        bar.schema.json 加必填 ts_str_prev,proto 2
binding     Date[1]==Date → bar_time = ts_str_prev（逐字,不 align）
            Date[1]<>Date → bar_time = close − tf
```

若 B2 不成立,還要多送 session 起點(欄位名待 B3)。

連帶要處理的:
- `align_bar_time` 對 bar 只剩 `1d` 用得到(呼叫點只有
  `el_subscriber.py:532`)。§2.2 的格線論述要改寫,不能直接刪 —— 它同時是
  `1d` 的 04:00 ET 錨定依據
- `EL_PublishTick` 不需要改,但 `proto` 是 frame 層級的鍵,tick frame 也會變成 2
- `contract/error_codes.md` 加 `EL_PublishBar` 墓碑的 `-6`
- 重錄 fixture、重推 expected、`CLAUDE.md` 的「proto 1 / ABI 1」段落全部要改

### Phase 3 — 重收資料

使用者會清除既有 `data/`。Phase 2 上線後重掛 1m/5m/15m/30m/1h/1d 重收,再跑一次
本文件 A 區的所有檢查。

---

## D. 順序理由

Phase 1 排在 Phase 2 前面,即使兩者都要重錄 fixture(等於錄兩次):

1. Phase 1 **完全不擋**,Phase 2 擋在 B1
2. main 現在帶著一份不忠實的 fixture(A5),那是我放進去的,越早拿掉越好
3. Phase 2 動 ABI,失敗時要滾回;Phase 1 已經落地的話,滾回的範圍比較乾淨

## E. 驗證門檻

每個 commit 前,從 `bindings/python/`:

```
uv run --with pytest-timeout pytest -q --timeout=30
uv run ruff check . ; uv run ruff format --check .
uv run mypy
```

Phase 1 / Phase 2 另外要跑 mutation 檢查,並把「哪些變異抓不到」如實記錄。
