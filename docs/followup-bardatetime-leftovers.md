# 待修清單 — `BarDateTime` 改動的遺留與更早的文件漂移

**日期:** 2026-08-03
**來源:** `docs/plan-bardatetime-seconds.md`（P1–P6 已完成）之後的自我複查
**分支:** `fix/bardatetime-seconds`
**狀態:** ✅ **A / B / C 三組全部完成**（見下方每項的勾選）

分成三組。**A 組不是文件**，會影響行為或影響第二個 binding 的實作，優先度最高。

驗證：`262 passed`、conformance 30/30、ruff、ruff format、mypy 全清、
`point.schema.json` JSON 合法。

---

## A. 非文件 — 會改變行為或誤導實作

### ✅ A1. `contract/point.schema.json:50` 的 `ts_str` 描述仍要求 floor

**現況**

```json
"description": "EasyLanguage Date+Time, yyyy-MM/dd-HH:mm:ss (24-hour), America/New_York wall clock. AUTHORITATIVE for bar_time: parse as ET, convert to UTC, floor to the minute, store it. ..."
```

**問題**：這是 wire schema 本身的規範文字，也是第二個 binding 作者最先讀、而且會照著實作
的東西。它現在與 `contract/semantics.md` §2.1 直接矛盾。因為是 `description` 欄位，
**照它實作的 binding 會靜默砍掉秒數、而且完全通過 schema 驗證** —— 沒有任何一層會發現。

這是 P1 的漏網：改了 `semantics.md`，沒掃 `contract/` 底下的 schema 檔。

**怎麼修**

把 `floor to the minute, store it` 改成保留秒數的說法，並指向 §1.3：

```
"AUTHORITATIVE for bar_time: parse as ET, convert to UTC, store it --
 SECONDS INCLUDED. Produced by EasyLanguage's BarDateTime, which carries
 real seconds (semantics.md 1.3); Date/Time do not, and flooring here
 collapses a sub-minute chart's bars onto one bar_time. It is the point's
 CLOSE time and lands that way -- no shift onto a left edge, no grid
 alignment. See semantics.md 2 / 2.1."
```

**順帶檢查**：`contract/` 下其他 `*.json` / `wire.md` 是否有同樣說法。這次只確認了
`point.schema.json` 與 `semantics.md`，`wire.md` 尚未逐行讀過。

**驗證**：改完 `grep -rn "floor" contract/` 應只剩 §1.1 那段講 `ts` 退路的歷史說明。

---

### ✅ A2. `_floor_to_minute_utc` 讓兩條路徑規則不一致（程式碼）

**位置**：`bindings/python/src/tradestation_data/wire/el_subscriber.py:498-500`，
唯一呼叫點在 `:445`（`ts_str` 缺席時的退路）。

```python
def _floor_to_minute_utc(epoch_seconds: float) -> datetime:
    ts = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return ts.replace(second=0, microsecond=0)
```

**問題**：P3 刻意留著它，理由寫「輸入是收訊時鐘，本來就沒有 bar 邊界的意義」——
那個理由站不住：

1. **兩條路徑規則不一致**：有 `ts_str` → 保留秒數；沒有 → 砍秒數。
2. **方向是錯的**：在 30 秒圖上，砍秒數會讓一分鐘內的兩根 bar 塌成同一個 `bar_time`，
   接著被 `_handle_provider_bar` 的 buffer 當成同一根的更新吃掉一根 ——
   **正是這整個修正要消滅的失效模式，卻在退路上原封不動留著**。不砍的話，至少同一秒
   以外的 bar 還分得開。
3. **可達**：`semantics.md` §1.1 的表格明文允許 publisher 省略 `ts_str`
   （「欄位不存在或為 `""` → 允許退回 `ts`」）。現行 EL 一定會送，但契約允許不送，
   第二個 binding 或別的 publisher 打得到。

**嚴重度**：中低 —— 是降級路徑，而且每次都會 `log.warning("ts_str_absent_using_recv_clock")`。
不是高危，但它是真的程式碼行為，不是註解。

**怎麼修**（建議）

移除 flooring，讓兩條路徑一致：

```python
def _recv_clock_utc(epoch_seconds: float) -> datetime:
    """`ts_str` 缺席時的退路。秒數保留 —— 與 `_parse_el_str_as_et` 同一條規則。

    這裡曾經 floor 到分鐘，跟舊的 §2.1 一致。§2.1 反轉後留著它會讓兩條路徑
    對同一個問題給出不同答案，而且方向是錯的：砍秒數只會讓降級路徑更容易
    塌成同一個 bar_time。
    """
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)
```

改名是因為函式名本身就是那條規則的宣告。呼叫點 `:445` 跟著改。

**要一併確認**：`semantics.md` §1.1 那段「全部 floor 到同一分鐘、塌成同一個
`bar_time`」是在描述**歷史事故**（zh-TW `FormatTime("tt")` 事件）。移除 flooring 後
該段的因果要重寫 —— 回放時整段共用一個 `ts`，就算不 floor 也還是會塌，只是塌在秒
而不是分鐘。**論點仍然成立，但句子要改**，別讓它讀起來像在描述現行程式碼。

**測試**：目前沒有任何測試覆蓋 `ts_str` 缺席的退路（`grep ts_str_absent tests/` 無結果）。
修的時候補一個。

---

## ✅ B. 本次改動造成的註解過期（7 處，邏輯正確、理由過期）

`ts_str` 現在有秒數，但這些地方仍寫「`ts_str` 只有分鐘解析度」。**程式邏輯全部是對的**
—— tick chart 仍必須 bypass buffer，因為一「秒」內可以有多筆成交，`bar_time` 依然不唯一。
正確說法是 **intra-second ordering**，不是 intra-minute。

| # | 檔案:行 | 內容 |
| --- | --- | --- |
| B1 | `bindings/python/src/tradestation_data/domain/bar.py:54-55` | `ts` 欄位 docstring |
| B2 | `bindings/python/src/tradestation_data/runtime/ingestion.py:45` | class docstring（**這行是本輪 T7 自己寫的，寫完當天就過期**） |
| B3 | `bindings/python/src/tradestation_data/runtime/ingestion.py:271` | tick chart bypass 的理由 |
| B4 | `bindings/python/src/tradestation_data/storage/bar_writer.py:64` | `ts` 欄位註解 |
| B5 | `bindings/python/tests/conformance/test_wire_conformance.py:115` | 測試註解 |
| B6 | `bindings/python/tests/scripts/test_dedupe_bars_script.py:64` | 測試 docstring |
| B7 | `bindings/python/tests/test_ingestion_runtime.py:409` | 測試 docstring |

**怎麼修**：把「`ts_str` has minute resolution / 只有分鐘解析度」一律改成
「a second can hold many prints, so they share one `bar_time`」的說法，
並在 B3 補一句指向 `semantics.md` §1.3。

**驗證**：`grep -rn "minute resolution\|分鐘解析度" bindings/ ` 應只剩
`EL/TS2Python_Exporter.el:180`（那句講的是 `Date`/`Time`，仍然正確）。

---

## ✅ C. 更早重構的遺留 —— 與本次改動無關，但確實是錯的

這批不是 `BarDateTime` 造成的，是 proto-2 / tick-bar 合併那次留下的，前兩次改
`EL/README*.md` 都只掃到自己正在看的段落，沒發現。

| # | 檔案:行 | 錯在哪 |
| --- | --- | --- |
| C1 | `EL/README.md:184-195` 整節 `### Why second-based charts need their own guard` | 三句三錯：①「`BarType`/`BarInterval` **無法**區分 1 秒圖與 1 分鐘圖，都回 `1`/`1`」→ 實測是 `BarType=14`；②「`TsStr` 由 `Time` 組出，秒在離開 indicator 前就沒了」→ 已改用 `BarDateTime`；③「latch 並**停止送出**」→ proto-2 就不停了 |
| C2 | `EL/README.md:160` 標題 `### Why an N-tick chart is refused` | N-tick 圖早就不被拒收 |
| C3 | `EL/README.md:167` | 語法破碎的殘句：「`bar_interval` now travels on the wire and says the call is a bar either」—— 像是刪 `EL_PublishTick` 時改壞的 |
| C4 | `EL/README.md:167`、`EL/README.zh-TW.md:146` | 提到 `Tier 1` —— 舊架構詞彙，已不存在 |
| C5 | `EL/README.md:214`、`EL/README.zh-TW.md:182` | 「**Ticks only.** Bars carry no quote」→ proto-2 之後 bar 帶報價 |
| C6 | `EL/README.md:157-158`、`EL/README.zh-TW.md:138` | 「`OpenInt` 在股票與 ETF 上恆為 0」→ CLAUDE.md §3.4 實測：盤中每種 category 的 `el_open_interest` 都等於 `el_downticks` |
| C7 | **`contract/semantics.md:229`** | 「兩者都只適用於 tick —— bar 不帶報價」→ 同 C5，但這是 **contract 層**，比 README 嚴重 |
| C8 | `EL/README.zh-TW.md:140` | `### N-tick 圖為何被拒收` —— C2 的中文版 |

**建議做法**：C 組不要用「我讀到哪修到哪」的方式處理 —— 這次連續兩輪都證明那樣會漏。
建議對 `EL/` 與 `contract/` 跑一次完整的文件對照複查（例如 `/code-review` 或專門的
一輪 diff review），把整份 `EL/README*.md` 與 `contract/semantics.md` 逐節對照現行
程式碼與 wire 行為，一次修完。

---

## 執行順序建議

1. **A1 + A2**（contract schema + 程式碼一致性）—— 獨立 commit，附帶 A2 的新測試
2. **B1–B7** —— 一個機械 commit
3. **C 組** —— 先做完整複查再修，不要邊看邊改

驗證門檻同 `docs/plan-bardatetime-seconds.md` §C。
