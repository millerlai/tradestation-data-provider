# EL — EasyLanguage Exporter

> 📖 [English README](README.md)

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

先裝 DLL，再裝 indicator —— Verify 的時候 DLL 就必須已經在位：

```powershell
cd cpp
.\install-to-tradestation.bat     # 自動找到 TradeStation 並複製對應架構的 DLL
```

不想自己 build 也可以，[`../cpp/prebuilt/`](../cpp/prebuilt/) 裡有現成的 binary；
細節見 [`../cpp/README.zh-TW.md`](../cpp/README.zh-TW.md)。

1. 在 TradeStation 開啟 EasyLanguage Editor
2. 建立新的 Indicator，名稱 `TS2Python_Exporter`
3. 貼上 `TS2Python_Exporter.el` 內容
4. 確認 `TS2Python.dll` 已放在 TradeStation DLL 搜尋路徑（上面的安裝腳本會處理）
5. Verify indicator（F3）
6. 掛到目標 chart（SPY、QQQ、`$TICK`, …），data series 設為 **tick 或 1 分鐘**

## 支援的 chart 間隔

| chart | 行為 |
| --- | --- |
| Tick series（`BarType = 0`） | 逐筆送出 |
| 1 / 5 / 15 / 30 / 60 分鐘圖 | 依照對應的 timeframe (`1m`, `5m`, 等) 送出完整 OHLC |
| 日線圖（`BarType = 2`, `BarInterval = 0` 或 `1`） | 以 `1d` timeframe 送出完整 OHLC。TradeStation 10 實測回報 `0`；`1` 也收，那是實機量測前 ABI 寫定的值 |
| 週 / 月 / P&F / 其他不支援的間隔 | **閒置**，並由 DLL 回傳 `-5` 拒收，Print 一次原因 |
| 秒級圖表 | **閒置**，由 indicator 自行偵測後停止送出，Print 一次原因 |

### 秒級圖表為何要另外擋

`BarType` 與 `BarInterval` **分不出** 1 秒圖與 1 分鐘圖 —— 兩者都可能回報 `1` / `1`。
若照 1 分鐘送出，那些 bar 會填進 `bars/timeframe=1m/` 分區，而且下游查不出來：
`TsStr` 由 `Time` 組出，而 `Time` 只有分鐘解析度，**秒在離開 indicator 之前就沒了**。

擋法不依賴任何版本相關常數：分鐘圖的 `Date` / `Time` 每根 bar 都前進，秒級圖表則會在
同一分鐘內重複。Indicator 偵測到連續兩根 bar 的 `Date` 與 `Time` 相同（且 `BarType = 1`，
排除本來就一分鐘多筆的 tick series）就閂住並停止送出。

> TradeStation 另有 `BarType_ext` 可區分秒級與分鐘級 intraday，但各版本取值不同、
> 未對實機確認，因此**不用**它當判斷依據。若要釘出那些數值：在已知的 1 分鐘圖與
> 1 秒圖上各 `Print(BarType_ext)` 一次。

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
