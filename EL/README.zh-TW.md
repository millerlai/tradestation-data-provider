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

> **DLL 與 `.ELD` 是一組的，永遠要一起換。**
> `EL_PublishTick` 與 `EL_PublishBar` 曾經在協定重寫後沿用原名但**簽章不同**，而
> `__stdcall` 由被呼叫端清堆疊，所以簽章不符的呼叫會**損毀堆疊**，不是回傳錯誤。
> 兩道守衛讓每一種錯配都變成看得懂的失敗：本 indicator 綁定 `EL_Init3`，舊 DLL
> 沒有這個匯出（Verify 就會失敗）；而新 DLL 把 `EL_Init` / `EL_Init2` 保留為回傳
> `-6` 的墓碑，讓舊 `.ELD` 停在 init、走不到 publish。init 之後另有一道
> `EL_DllVersion()` 檢查。完整對照見 [`../contract/wire.md`](../contract/wire.md)。

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

## Inputs

| input | 預設 | 作用 |
| --- | --- | --- |
| `ZMQEndpoint` | `tcp://127.0.0.1:5555` | DLL 發布的位址 |
| `Enabled` | `True` | 總開關 |
| `LogErrors` | `True` | init 失敗、sub-minute / aggregated-tick 圖表偵測、非零的 publish 回傳碼 |
| `LogPublish` | `False` | 每次 publish 印一行 —— 見下 |

### `LogPublish`

每次 publish 印出全部五個量值保留字。因為它們是原樣上 wire 的，這一行也就**正好等於
subscriber 收到的內容**：

```
[TS2Python] bar  2026-07/24-15:59:00 bar_type=1.00 bar_interval=1.00
            o=742.31 h=742.60 l=742.28 c=742.55
            volume=13465 ticks=21152 upticks=13465 downticks=7687 openint=0
            rc=0
```

這個一致性正是重點：落地的分區可以逐行對照來源，中間沒有任何對映步驟需要推敲。
`volume` 與 `ticks` 並排，因為那一對正是在不同圖表型態之間會反轉語意的欄位。

沒有任何間隔被拒收，也沒有圖表型態會讓 publish 停下來——`LogErrors` 涵蓋兩種只印一次
就繼續發布的偵測：

```
[TS2Python] sub-minute chart detected on symbol=SPY — two consecutive bars
            share date=1260724.00 time=1600.00. ts_str has minute resolution...
[TS2Python] aggregated tick chart detected on symbol=SPY — bar_interval=100.
            each call carries 100 prints aggregated into one bar...
```

這兩種情況以前都會讓 publish 整個停掉——那是這支腳本自己判斷資料不值得送。現在
`bar_type`/`bar_interval` 會逐一隨每個 frame 上 wire，讓消費端自己看得出圖表是什麼，
同一分鐘內的多個 frame 則靠 wire 的接收端 `ts` 分開。

平時請關閉 `LogPublish`。在 tick 圖、或任何開啟 "update every tick" 的圖上，它會每筆
成交印一次。

## 支援的 chart 間隔

每種圖表型態都會被轉發——沒有拒收，也沒有閒置狀態。

| chart | 行為 |
| --- | --- |
| 1-tick series（`BarType = 0`, `BarInterval = 1`） | 逐筆送出 |
| N-tick series（`BarType = 0`, `BarInterval > 1`） | 每次呼叫送一根聚合 bar；偵測後只 Print 一次（見上），publishing 繼續 |
| 1 / 5 / 15 / 30 / 60 分鐘圖及其他任何 intraday 間隔（`BarType = 1`） | 送出完整 OHLC，`bar_type`/`bar_interval` 逐字上 wire |
| 日線圖（`BarType = 2`, `BarInterval = 0` 或 `1`） | 送出完整 OHLC。TradeStation 10 實測回報 `0`；`1` 也收，那是實機量測前 ABI 寫定的值 |
| 週 / 月 / P&F / 其他任何 bar type | 同樣被轉發；`bar_type` 本身就能指名，沒有任何對映或拒收 |
| Sub-minute / sub-second 圖表（`BarType = 1`，連續兩根 bar 共用同一個分鐘解析度的 `Date`/`Time`） | 偵測後只 Print 一次，publishing 繼續——**請見下方但書** |

> **秒級圖表在 wire 上跟 1 分鐘圖無法分辨。** 兩者都回報 `BarType = 1`，而 `TsStr`
> 只有分鐘解析度。目前的 Python 參考 binding 會把「共用同一個 `bar_time`」的兩個
> frame 當成同一根尚未收完的 bar 的更新——這在真正的 1 分鐘圖搭配「Update Every
> Tick」時是對的——所以它現在會把一個真正的秒級圖表的多根不同 bar 折疊成每分鐘一根。
> 要秒級資料請改用 tick 圖（`BarType = 0`，逐筆轉發）；完整說明見 exporter 檔頭的
> 註解。

### 為何五個量值全部送出、且不替你挑

TradeStation 對這些保留字在 intraday 與 daily 上的定義是**相反的**（股票商品）：

| | intraday | daily 以上 |
| --- | --- | --- |
| `Volume` | **只有上漲 tick** 的成交股數 | 總成交股數 |
| `Ticks` | **總成交股數** | tick 數 |
| `UpTicks` | 上漲 tick 成交股數 | 總成交股數 |
| `DownTicks` | 下跌 tick 成交股數 | 0 |

所以「`Volume` 是量、`Ticks` 是筆數」這個直覺讀法只在日線成立。本 indicator 先前
自己解決這件事：wire 上只有一個 `vol` 欄位，由圖表型態決定要填哪個字。這條路上出了
兩次問題。

第一次是最初的版本在所有圖表都送 `Volume`，在 intraday 上等於只送了上漲 tick 的量
—— 大約只有實際成交的一半，而且下游查不出來：那是個完全合理、只是持續偏小的數字。

第二次更根本：修法沒有消除問題，只是把它搬家。選擇仍然發生在 wire 之外，於是每一筆
payload 都得再帶一個 **publisher 慣例版本號**，只為了說明「這批數字是照哪一版規則算
的」—— 而那個號碼本身也會過期，因為本檔案住在使用者的 TradeStation 裡，DLL 或
subscriber 升級時沒有任何東西會更新它。

把五個字原樣送出、各佔一個以保留字命名的欄位，選擇就消失了，那個宣告也就不必存在。
語意反轉仍然是事實，但它現在是 consumer 查表就能解決的事
（[`../contract/semantics.md`](../contract/semantics.md) §3.4），而不是本檔案代為
決定。想要總成交股數的 consumer：intraday 取 `el_ticks`，日線取 `el_volume`。

`OpenInt` 是為了完整性而納入。它在股票與 ETF 上恆為 0，只在期貨與選擇權上有意義。

### N-tick 圖為何被拒收

tick series 只有在 `BarInterval = 1` 時才是「一次呼叫一筆成交」。100-tick 圖的每次
呼叫帶的是一整根 bar：`Close` 是那一百筆的最後一筆，而兩個量能欄位依上表的 intraday
規則涵蓋全部一百筆 —— `Ticks` 是它們的總成交股數、`Volume` 是其中的上漲部分。兩者
都不是「100」；intraday 根本沒有任何保留字提供筆數。`bar_interval` 現在會上 wire，說明這次呼叫；舊 wire 沒有欄位能
說明這次呼叫是一根 bar，於是 Tier 1 會把它記成**單一筆成交，價格是其中一筆、成交量
卻是一百筆的和** —— 成交量欄位錯約兩個數量級，而且無人能察覺。

與秒級圖不同的是，判斷所需的資訊還在：`BarInterval` 直接說明一次呼叫涵蓋幾筆。
實機量測：100-tick 圖在 init 時回報 `bar_interval=100.00`、`Ticks = 760951`
（那一百筆的成交股數，不是 `100`），1-tick 圖回報 `1.00` 並且每筆成交呼叫一次
`EL_Publish`。

另外這也表示 `TsStr` 無法區分同一分鐘內的多筆成交：1-tick 圖會連送八次、全部標記
`19:48:00`，因為 `Time` 只有分鐘解析度。真正區分它們的是 DLL 的收訊端 `ts`，這正是
[`../contract/semantics.md`](../contract/semantics.md) §1 規定 tick 時間取自 `ts`
而非 `ts_str` 的原因。

### 秒級圖表為何要另外擋

`BarType` 與 `BarInterval` **分不出** 1 秒圖與 1 分鐘圖 —— 兩者都可能回報 `1` / `1`。
若照 1 分鐘送出，那些 bar 會填進 `bartype=1/interval=1/` 分區，而且下游查不出來：
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
  **只在 tick 上送。** Bar 不帶報價：即時報價函式描述的是呼叫當下那一刻，在 bar 上
  就是它的最後一筆成交，不是整根 bar。
- 五個量值保留字不做轉換也不做挑選，一律原樣轉發。任何詮釋都屬於 consumer。

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
