# EL — EasyLanguage Exporter

TradeStation EasyLanguage indicator，負責把 tick / bar 資料經由 C++ bridge DLL
（[`../cpp/`](../cpp/)）推上 ZeroMQ，供任何語言的 subscriber 消費。

這是整條資料鏈的**上游起點**：

```
TradeStation Chart → TS2Python_Exporter.el → TS2Python.dll → ZMQ PUB → subscriber
```

## 檔案

- `TS2Python_Exporter.el` — Exporter indicator ELS 原始碼（複製貼到 TradeStation
  EasyLanguage Editor）

## 部署步驟

1. 在 TradeStation 開啟 EasyLanguage Editor
2. 建立新的 Indicator，名稱 `TS2Python_Exporter`
3. 貼上 `TS2Python_Exporter.el` 內容
4. 確認 `TS2Python.dll` 已放在 TradeStation DLL 搜尋路徑（見 [`../cpp/README.md`](../cpp/README.md)）
5. Verify indicator（F3）
6. 掛到目標 chart（SPY、QQQ、`$TICK`, …），data series 設為 **tick 或 1 分鐘**

## 支援的 chart 間隔

| chart | 行為 |
| --- | --- |
| Tick series（`BarType = 0`） | 逐筆送出 |
| **1 分鐘**（`BarType = 1` 且 `BarInterval = 1`） | 送出完整 OHLC |
| 其他 intraday 間隔（5 分、15 分、60 分 …） | **閒置**，Print 一次原因 |
| 日 / 週 / 月 / P&F | 閒置，Print 一次原因 |

其他間隔之所以閒置而非送出：`BarType = 1` 涵蓋所有 intraday 分鐘圖，而 wire 目前
寫死 `bar_1m`。從 5 分鐘圖送出會把 5 分鐘 bar 存進 `timeframe=1m` 分區，下游再從
這批資料「推導」5m —— 全程沒有任何錯誤訊息。讓圖表安靜下來才是誠實的失敗方式。

多 timeframe 將透過 wire 的 `tf` 欄位支援。

> ⚠️ **待驗證**：秒級 chart 可能同樣回報 `BarType = 1`、`BarInterval = 1`，
> 那樣 1 秒 bar 會通過檢查並被當成 1 分鐘 bar 送出。TradeStation 有
> `BarType_ext` 可區分秒級與分鐘級 intraday，但各版本取值不同，尚未對照實機確認。
> 在確認前請只使用分鐘圖。

## 設計約束

- Indicator **不做任何策略運算**，只負責呼叫 DLL 匯出資料。
- 送出的 payload 格式由 [`../contract/`](../contract/) 規範，不是這支 indicator 的
  自由；修改欄位前先改 contract。
- `InsideBid` / `InsideAsk` 原樣傳出，不在 EL 端判斷。無報價時（歷史回放、
  非 live mode、breadth symbol）它們回傳 0，由 DLL 統一正規化為 JSON `null` ——
  集中在 C ABI 一處，所有 EL 呼叫端才會一致。見
  [`../contract/semantics.md`](../contract/semantics.md) §3.1。

## 盤前資料

Exporter 送出什麼區間的資料，完全由 **chart 的 session 設定**決定，不需要改 EL 程式碼：

1. 在掛 `TS2Python_Exporter` 的 chart 上：右鍵 → **Format Symbol → Settings**
2. **Session** 由「Regular Session」改為含盤前的 template（例如 08:00–16:00 ET 的
   自訂 template，或內建 extended template）
3. Exporter 會照常把盤前 bars 送出

> 注意：改 session 只影響**資料範圍**，不影響 session 邊界語意。
> `session_open_utc` 固定為 09:30 ET，與 chart session 設定無關 ——
> 見 [`../contract/semantics.md`](../contract/semantics.md)。
> 消費端若有假設 RTH-only 的計算窗口，需自行確認啟用 extended session 後的行為。
