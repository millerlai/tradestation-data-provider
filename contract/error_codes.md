# TS2Python DLL — Error Codes

`cpp/include/ts2python.h` 的 C ABI 每個進入點都回傳帶號 `int`。
非負值代表成功，負值代表特定失敗模式。**這些數字就是契約** —— EasyLanguage 端在需要
時應原樣 `Print` 出來。

| Code | 意義 | 由誰回傳 | 處理方式 |
|-----:|---|---|---|
| `0` | 成功。socket 已綁定、有訂閱者、**這張圖的 hello 已送出** | `EL_Init` `EL_Publish` | — |
| `1` | 這張圖在本 session 已宣告過。沿用既有 socket，第二次為 no-op | `EL_Init` | 不需處理。Indicator 可選擇不重複輸出 "init ok" |
| `-1` | 未初始化 —— 在成功的 init 之前呼叫了 publish | `EL_Publish` | 先呼叫 `EL_Init` |
| `-2` | ZeroMQ 送出失敗。可能是觸及 high-water mark 導致 `send()` 回傳 `EAGAIN`，或非預期的 `zmq::error_t` | `EL_Init` `EL_Publish` | 記錄後繼續，下一筆會重試。若持續發生，檢查 SUB 端是否存在 |
| `-3` | init 的 bind / socket 建立失敗 —— 最常見是 TCP endpoint 已被其他 process 佔用（或前一個 TradeStation session 殘留的 DLL handle） | `EL_Init` | 檢查 `netstat -ano \| findstr :5555`，結束佔用者後重新 Verify indicator |
| `-4` | 參數無效。`zmq_endpoint` 或 `symbol` 為 null；**或 payload `snprintf` 被截斷**（代表數值輸入異常超出範圍） | `EL_Init` `EL_Publish` | 上游資料問題，確認 EL indicator 傳入的型別 |
| `-6` | **ABI 不符 —— 呼叫端是早於本協定的 `.ELD`** | `EL_PublishTick` `EL_PublishBar`（兩者皆為墓碑） | 重新匯入隨這顆 DLL 一起發布的 `.ELD`。見下節 |
| `-7` | **尚無訂閱者。可重試，而且是啟動時的正常狀態** | `EL_Init` | 不需處理。Indicator 保持 `InitDone = False`，下一根 bar 再呼叫一次 |

## `-7` 不是錯誤

ZeroMQ PUB/SUB 在沒有訂閱者時**靜默丟棄所有送出的訊息，且不回報任何東西**。前一代的
init 只要 bind 成功就回 0，於是 TradeStation 先開、consumer 後開的情況下，Print Log 印
著 "init ok"，而每一根 bar 都進了垃圾桶。

現在 DLL 的 socket 是 **XPUB** 而不是 PUB —— 差別在於訂閱事件會以可讀訊息的形式送回
publisher，所以 DLL 有辦法回答「到底有沒有人在聽」。`EL_Init` 在控制 topic 尚無訂閱者
之前回 `-7` 並且**不發布任何東西**。

Indicator 對任何負值 rc 都保持 `InitDone = False`，所以這件事會自己解決：consumer 一
起來，下一根 bar 的 `EL_Init` 就會回 0 並開始發布。指標只會在 Print Log 說一次
「waiting for a subscriber」，不會每根 bar 洗版。

> **這代表 `cpp/Release/TS2Python_TestHarness.exe` 必須先有訂閱者才跑得動。**
> 先開 `contract/tools/record.py`（或任何 SUB），否則 harness 會等到
> `--subscriber-timeout-ms` 逾時後以 `-7` 退出。

## `-6` 與墓碑匯出

`EL_PublishTick`、`EL_PublishBar` 是前一代協定的匯出。它們**仍然存在於 `.def` 裡**，
但函式體只有 `return -6;`。

理由是 `__stdcall`：這兩個名字曾經沿用前一代的名字但**簽章不同**，而 `__stdcall` 由被
呼叫端清堆疊，所以簽章不符的呼叫會**損毀堆疊** —— 不是回傳錯誤碼，是 TradeStation
崩潰或隨機行為。

**它們現在擋不住任何東西，這點必須說清楚。** 以前擋住問題的是 init：每一個 publish 呼叫
都在「init 成功」的守衛之內，而 init 的名字改過（`EL_Init` → `EL_Init2` → `EL_Init3`），
所以舊 `.ELD` 停在自己的 init，永遠碰不到改過簽章的 publish。

這一版把 `EL_Init` 這個名字**收回來重用**，參數從 1 個變成 5 個。舊 `.ELD` 綁的是單參數
的 `EL_Init`，`DefineDLLFunc` 只按名字解析，所以它**解析得到、呼叫得下去、然後在
`EL_Init` 裡就損毀堆疊** —— 比前一代更早，而且沒有任何錯誤碼。DLL 這一側看不到呼叫端
推了幾個參數，程式碼補救不了。

保留這兩個墓碑仍然只有好處：刪掉匯出只會讓失敗變成一個沒有上下文的 `DefineDLLFunc`
解析錯誤。但**它們不再是安全網**。

**DLL 與 `.ELD` 是同一個單位，必須一起安裝、一起重新 Verify。**

反方向是安全的 —— 新 `.ELD` 配舊 DLL：ABI 2 的 DLL 匯出的是 `EL_Init3`，沒有五參數的
`EL_Init`，`DefineDLLFunc` 在 verify 階段就失敗，什麼都不會跑。四種不相容組合的完整
對照見 [`wire.md`](wire.md) 的〈新舊部署不相容時會發生什麼〉。

## `-2` 與靜默丟包的差別

`-2` 是**回報得出來**的送出失敗。真正危險的是 ZMQ PUB 在超過 `SNDHWM` 時的**靜默
丟棄** —— 那不會回傳錯誤碼，publisher 完全不知情。

錯誤碼涵蓋不到這個情況，這正是 payload 帶 `seq` 的原因。見
[`wire.md`](wire.md) 的〈傳輸保證〉與 [`semantics.md`](semantics.md) §6。

## 版本識別

`EL_DllVersion()` 回傳目前 DLL 的 ABI 版本（整數），本協定為 **3**。

ABI 版本**不是** wire 版本。point frame 一個 byte 都沒變，仍然是 `proto` 2，所有已錄製的
fixture 全部繼續有效。變的是 C ABI（`EL_Init` 的簽章）與一個走獨立 topic 的新增控制
frame —— 只讀 point 的 consumer 根本不會訂閱到它。

indicator **應該**在 init 成功後檢查它：`EL_DllVersion` 是 0 參數的匯出，簽章永遠不會
變，所以呼叫它在任何 DLL 版本上都是安全的 —— 它是唯一可以無條件先問一句「你是誰」的
進入點。回值不符時 indicator 應停止發布並記錄，而不是繼續呼叫其他匯出。

## 新增錯誤碼的規範

- 新錯誤碼必須為**負值**，取下一個可用的絕對值。**永不重用已退役的碼**。
- `0` 以外的成功碼（如冪等 init 的 `1`）應該罕見，能用單一 `0` 就用。
- 新增碼時，本表與 `ts2python.h` 的註解區塊必須在**同一個 commit** 內更新。
