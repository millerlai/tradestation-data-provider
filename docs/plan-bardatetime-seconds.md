# 修正計畫 — `bar_time` 帶上真正的秒數（`BarDateTime`）

**Branch:** `fix/bardatetime-seconds`
**起點:** `a749b1d`（`fix/code-review-2026-08-02`，PR #32）
**日期:** 2026-08-03
**修的問題:** `TODO_ISSUES.md` I1 — 秒級圖表每分鐘只有一根 bar 落地

---

## A. 已證實的事實

每一條都有 live 量測，不是推論。全部來自 `EL/Probe_TimePrecision_And_B1.el`
（一支只 Print、不碰 DLL/ZMQ 的診斷 indicator）。

### A1. 秒級圖表是 `BarType = 14`，不是「`BarType = 1` 但更細」

```
[cat] BarType=   1.00, BarInterval=   1.00, symbol=SPY   ← 1 分鐘圖
[cat] BarType=  14.00, BarInterval=  30.00, symbol=SPY   ← 30 秒圖
```

TradeStation 官方 `BarType` 值表（使用者提供）：

```
0 TickBar   1 Minute   2 Daily   3 Weekly   4 Monthly   5 Point & Figure
6 (reserved)   7 (reserved)   8 Kagi   9 Kase   10 Line Break
11 Momentum   12 Range   13 Renko   14 Second   15 Renko Custom   16 Renko Mean
```

所以 30 秒圖與 1 分鐘圖在 Python buffer 的 key
`(symbol, bar_type, bar_interval)` 上**天生就不會混在一起**。原本擔心的
「修法會誤傷正常 1 分鐘圖」不存在。

### A2. `Date`/`Time` 保留字沒有秒數，`BarDateTime` 有

同一份 log 的同兩行（`BarType=14/BarInterval=30`，2026-08-03 live）：

```
call=1 bar#=1 now=2026-08/03-07:20:00  fmt_now=2026-08/03-07:20:00
call=2 bar#=2 now=2026-08/03-07:20:00  fmt_now=2026-08/03-07:20:30
                  ^^^^^^^^^^ 撞名           ^^^^^^^^^^ 區分開
```

`bar#1` 與 `bar#2` 是**兩根不同的、已收完的 bar**。舊的 `Date`/`Time` 組法兩根
都給 `07:20:00`；`BarDateTime` 給 `07:20:00` 與 `07:20:30`。

TradeStation 官方文件對 `BarDateTime` 的說法：

> "The BarDateTime reserved word allows you to reference the current date and
> time properties of the bar, **including seconds**."

`BarDateTime[BarsAgo].FieldName`，回傳 `DateTime` 類別物件，屬性有
`Hour` / `Minute` / `Second` / `ELDate` / `ELTime` / `ELDateTimeEx`。

> **不可與 `elsystem.DateTime.CurrentTime` / `.Now` 混淆。** 那兩個讀的是
> **電腦系統時鐘**，不是 bar 自己的時間，會重演 `ts`（收訊時鐘）在歷史回放時
> 「整段塌成同一瞬間」的事故。`BarDateTime` 是 bar-scoped，那才是要用的。

### A3. `BarDateTime.Format()` 直接產出 wire 需要的字串

```
bdt_now= 7:25:30                    ← 手動 Hour/Minute/Second，空格補位
fmt_now=2026-08/03-07:25:30         ← BarDateTime.Format("%Y-%m/%d-%H:%M:%S")
```

補零正確、格式與現行 `TsStr`（`yyyy-MM/dd-HH:mm:ss`）完全一致，一次呼叫完成。
不需要手動 `NumToStr` + 補零。

### A4. 成形中的 bar，時間戳不會漂移

log 裡 `call=10` 重複 7 次、`call=11` 重複 8 次 —— `CallNum = CallNum + 1`
不可能重複。原因是 **EasyLanguage 的 intra-bar 語意：「Update Every Tick」的
重算呼叫中，變數賦值不會 commit**，每次 tick 先把變數回捲到上一根 bar 收盤時
的值再重跑。所以 `CallNum` 每次都從 9 加到 10。

（連帶：探針的 `newbar` 偵測因此**失效** —— `PrevBarNum` 一樣被回捲，所以永遠
印 `Y`。這是探針的缺陷，不是資料的性質。）

**但那些重複行正是我們要的 refire 呼叫**，而在 `bar#10` 的 7 次、`bar#11` 的
8 次重算中：

```
bar#10 ×7:  fmt_now=2026-08/03-07:25:00  fmt_prev=2026-08/03-07:24:30
bar#11 ×8:  fmt_now=2026-08/03-07:25:30  fmt_prev=2026-08/03-07:25:00
```

**每一個欄位都完全不變。** 一根成形中的 bar 被穩定標上它「將要收盤」的時間，
中途不跳動 —— 這正是 Python buffer 的 replace-last 需要的性質。`BarDateTime[1]`
（Phase 2 的 `ts_str_prev` 候選）同樣穩定。

### A5. EL 改完還不夠 —— Python 端會把秒數砍掉

`contract/semantics.md` §2.1 明文規定「秒與微秒一律歸零」，
`el_subscriber.py:523` 的 `.replace(second=0, microsecond=0)` 在執行它。
**即使 EL 送出 `07:25:30`，Python 也會變回 `07:25:00`，撞名照樣發生。**

而且這條規則有 conformance fixture 釘著：

```
smoke.jsonl        ts_str = "2026-04/18-13:30:45"
expected/smoke.json  bar_time = "2026-04-18T17:30:00Z"
```

### A6. §2.1 的立論已經過期

§2.1 的理由原文：

> 1 分鐘 bar 的 bucket 依定義是 `[分鐘邊界, +1min)`。若原樣保留秒數，
> `17:30:45` **起算**的 bucket 涵蓋 `[17:30:45, 17:31:45)`，那不是分鐘 bar

這整段是用**左邊界 bucket 模型**論證的，而那個模型正是 proto-2 重構刪掉的東西
（§2 現在寫明 `bar_time` 就是收盤時間，逐字落地，不是 bucket 起點）。
**這條規則活過了一次把它的立論基礎拆掉的重構。**

---

## B. 分階段

順序是有意義的：`contract/` 是 source of truth，先定案文字，後面全部對齊它。

| # | 階段 | 檔案 | 狀態 | Commit subject |
|---|---|---|---|---|
| P1 | 改規格 | `contract/semantics.md` §1/§1.1/§2/§2.1 | ✅ | `spec(contract)!: bar_time keeps its seconds, sourced from BarDateTime` |
| P2 | 改 publisher | `EL/TS2Python_Exporter.el` | ✅ | `feat(el)!: build ts_str from BarDateTime so sub-minute bars stay distinct` |
| P3 | 改 binding | `bindings/python/src/tradestation_data/wire/el_subscriber.py` | ✅ | `fix(wire)!: keep the seconds ts_str now carries` |
| P4 | 改 harness | `cpp/src/test_harness.cpp` | ✅ | `test(contract): cover two 30-second bars inside one minute` |
| P5 | ~~重錄 fixture~~ + 手推 expected | `contract/fixtures/` | ✅ | 同 P3 commit（見下方修正） |
| P6 | 文件收尾 | `CLAUDE.md`、`EL/README*.md`、`TODO_ISSUES.md` | ✅ | `docs: record the BarDateTime switch and close out TODO_ISSUES I1` |

圖例：⬜ 未開始 · 🔄 進行中 · ✅ 完成 · ⏸️ 卡住（原因記在下方）

### P1 — `contract/semantics.md`

改動點：

1. **§1 的表格**：`ts_str` 的「錯誤用途」欄現在寫 `❌ 當成有秒級解析度` —— 這句
   在 `BarDateTime` 之後不再成立，要改。
2. **§1 表格下方那段**：「`ts_str` 只有分鐘解析度 —— tick 圖一分鐘內的每一筆成交
   共用同一個 `bar_time`」—— tick 圖（`bar_type` 0）用的仍是 `Date`/`Time` 還是
   也換 `BarDateTime`？**這是 P2 要一併決定的**，§1 的文字跟著結果走。
3. **§2 的流程圖**：`→ 轉 UTC → 秒歸零 → bar_time` 的「秒歸零」要拿掉。
4. **§2.1 整節**：規則反轉 —— 從「一律歸零」改成「原樣保留」，並把 A6 的過期
   立論改寫成現在的收盤時間模型。保留「為什麼曾經有這條規則」的歷史說明，
   照本 repo 一貫做法（規則可以改，改的理由必須留下）。

### P2 — `EL/TS2Python_Exporter.el`

`TsStr` 從

```
FormatDate("yyyy-MM/dd", ELDateToDateTime(Date))
+ "-" + FormatTime("HH:mm:ss", ElTimeToDateTime(Time))
```

改成

```
BarDateTime.Format("%Y-%m/%d-%H:%M:%S")
```

**待決定**：檔頭那段「SUB-MINUTE AND AGGREGATED-TICK CHARTS ARE DETECTED AND
LOGGED」的 sub-minute 偵測與警告，在秒數上 wire 之後還需不需要？偵測本身
（連續兩根 bar 共用 `Date`/`Time`）在 `BarDateTime` 之後就不再代表資料會遺失。

### P3 — `el_subscriber.py`

- 移除 `_parse_el_str_as_et` 結尾的 `.replace(second=0, microsecond=0)`。
- `_floor_to_minute_utc`（`ts` 缺席時的退路）是**另一件事**，先不動 —— 那條路
  的輸入是收訊時鐘，本來就沒有 bar 邊界的意義。

### P4 — `cpp/src/test_harness.cpp`

`--mode smoke` 目前把 tick 的 `13:30:45` 直接沿用給 bar frame，那個 fixture
存在的目的**就是**測 flooring。規則改了之後這個 case 的意義要重新想：是改成
測「秒數原樣保留」，還是加一個真正帶秒數的秒級圖 frame。

### P5 — fixture（**範圍比原本估計小很多**）

**原本以為要重錄 4 份 fixture，實際上不用。** DLL 從來不解析 `ts_str`（proto-2
把 `ts_utc` 拿掉時一併移除），它只是逐字轉發，所以**這次的改動完全沒有動到 DLL
的行為，錄下來的 wire 資料依然有效**。改變的只有 binding 對它的解讀。

所以 P5 實際做的是：`expected/*.json` **手工重推**（不得由 binding 產生，repo
硬規則）。四份的推導說明段落都要改（"floored to the minute" 那句），但只有
`smoke.jsonl` 帶非零秒數（`13:30:45`），所以只有它的 6 個 `bar_time` 值會變：

```
2026-04/18-13:30:45 ET (EDT, UTC-4)  →  2026-04-18T17:30:45Z   （原本寫 17:30:00Z）
```

其餘三份（`bars` / `noquote` / `session`）的 `ts_str` 秒數全是 `:00`，值不變。

**仍然缺的（需要 C++ toolchain，見 P4）**：沒有任何 fixture 涵蓋
`BarType=14`（秒級圖）。現在整條鏈的正確性只靠 `smoke` 的 `:45` 間接驗到「秒數
有保留」，但沒有一個 fixture 真正示範「同一分鐘內兩根不同的 bar」——而那正是這
整個修正要解決的情境。

### P6 — 文件

`CLAUDE.md` 的「floored to the minute」段落、`EL/README*.md` 的 sub-minute
但書（T5 剛加的）、`TODO_ISSUES.md` I1 結案。

---

## C. 驗證門檻

每個 commit 前，從 `bindings/python/`：

```
uv run pytest -q
uv run ruff check . ; uv run ruff format --check .
uv run mypy
```

P5 之後另外要跑 `uv run pytest tests/conformance`。

---

## D. 這個計畫不含什麼

- **`TODO_ISSUES.md` I2**（`_expected_bars` 的 session 格線）不在範圍內。它是
  另一個 wire 缺口（bar 的**起點**不在 wire 上），仍卡在
  `docs/plan-bar-start-on-wire.md` 的 B1–B3。
- 但 A4 已經順手為 B1 提供了第一手證據：`BarDateTime[1]` / `Time[1]` 在 refire
  中完全穩定，且正確追蹤前一根 bar。Phase 2 若要做，這份 log 可以直接引用。
