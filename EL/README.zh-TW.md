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
| 秒級圖表（`BarType = 14`，`BarInterval` = 每根幾秒） | 完整支援——`TsStr` 帶真正的秒數，所以 30 秒圖的 `07:20:00` 與 `07:20:30` 兩根不會混在一起。偵測後只 Print 一次，publishing 繼續 |

> **這件事在 2026-08-03 之前是壞的，而且壞得沒有聲音。** `TsStr` 當時是用 EL 的
> `Date`/`Time` 組出來的，那兩個保留字**根本沒有秒數**——所以上面那兩根都變成
> `07:20:00`，參考 binding 的 intra-bar 緩衝把第二根當成第一根的更新（這在真正的
> 1 分鐘圖搭配「Update Every Tick」時是**對的**行為），於是一張 30 秒圖每分鐘只
> 存下一根。全程沒有任何錯誤浮現。
>
> 現在 publisher 改用 `BarDateTime`（TradeStation 官方文件寫明它帶秒數），binding
> 也不再把秒數歸零。**若你還在跑舊的 `.ELD` 或舊的 binding，秒級圖表仍然會掉資料
> ——兩邊要一起升級。** 量測記錄見 `contract/semantics.md` §1.3。

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

`OpenInt` 在盤中圖上**不是**未平倉量。實測 SPY、`@ES`、`VXX` 與一檔 SPY 選擇權
（2026-08-02），以及之後收集的每一個分區：**`el_open_interest` 在每一列盤中資料上
都等於 `el_downticks`**，不分 category —— 期貨也一樣，而真正的未平倉量會大上好幾個
數量級。未平倉量只出現在日線以上，而且在那裡是 `DownTicks` 帶著它。兩個反轉、不是
一個，兩者都列在 [`../contract/semantics.md`](../contract/semantics.md) §3.4。

### N-tick 圖實際送出什麼

tick series 只有在 `BarInterval = 1` 時才是「一次呼叫一筆成交」。100-tick 圖的每次
呼叫帶的是一整根 bar：`Close` 是那一百筆的最後一筆，而兩個量能欄位依上表的 intraday
規則涵蓋全部一百筆 —— `Ticks` 是它們的總成交股數、`Volume` 是其中的上漲部分。兩者
都不是「100」；intraday 根本沒有任何保留字提供筆數。

**沒有任何東西被拒收。** `bar_interval` 會上 wire，明確說出這次呼叫涵蓋幾筆，消費端
看得到自己拿到的是什麼。這支 indicator 以前會在這種圖上停止送出 —— 那是舊 tick frame
沒有欄位能表達「這次呼叫是一根聚合 bar」的年代，消費端會把它存成「單一筆成交，價格
是其中一筆、成交量卻是一百筆的和」，成交量錯約兩個數量級而且無人能察覺。現在欄位存在
了，判斷就該留給下游。它仍然會在 Print Log announce 一次。

實機量測：100-tick 圖在 init 時回報 `bar_interval=100.00`、`Ticks = 760951`
（那一百筆的成交股數，不是 `100`），1-tick 圖回報 `1.00` 並且每筆成交呼叫一次
`EL_Publish`。

另外這也表示 `TsStr` 無法區分同一**秒**內的多筆成交：1-tick 圖會連送多次、全部標記
同一秒。真正區分它們的是 DLL 的收訊端 `ts`，這正是
[`../contract/semantics.md`](../contract/semantics.md) §1 規定 tick 的排序依據取自
`ts` 而非 `ts_str` 的原因。

### 秒級圖表：那個 latch 是什麼、不是什麼

`BarType` **分得出**秒級圖與分鐘圖：TradeStation 對 Second chart 回報
`BarType = 14`，`BarInterval` 的單位是秒。本檔案早先的版本宣稱兩者都回報 `1` / `1`
因此無法分辨 —— 那是沒有實測就寫下的，是錯的。

Indicator 裡的 sub-minute latch 現在**只是提示**。它在連續兩根 bar 重複同一個分鐘
解析度的 `Date` / `Time` 時觸發，告訴你這張圖比一分鐘細 —— 但那已經不代表有東西會
遺失，因為 `TsStr` 由 `BarDateTime` 組出、帶真正的秒數。它不會停止送出，本來也不
應該停。

> TradeStation 另有 `BarType_ext`，但各版本取值不同、未對實機確認，因此這裡完全不用
> 它。若要釘出那些數值：在已知的 1 分鐘圖與 1 秒圖上各 `Print(BarType_ext)` 一次。

## 設計約束

- Indicator **不做任何策略運算**，只負責呼叫 DLL 匯出資料。
- 送出的 payload 格式由 [`../contract/`](../contract/) 規範，不是這支 indicator 的
  自由；修改欄位前先改 contract。
- `InsideBid` / `InsideAsk` 原樣傳出，不在 EL 端判斷。無報價時（歷史回放、
  非 live mode、breadth symbol）它們回傳 0，由 DLL 統一正規化為 JSON `null` ——
  集中在 C ABI 一處，所有 EL 呼叫端才會一致。見
  [`../contract/semantics.md`](../contract/semantics.md) §3.1。
  **每一種資料點都送，bar 也不例外。** Bar 以前不帶報價，理由是即時報價函式描述的
  是呼叫當下那一刻、而非整根 bar。那句話是對的，但那不是這條傳輸鏈該做的判斷 ——
  跟移除硬編碼 index symbol 清單是同一個道理。
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
