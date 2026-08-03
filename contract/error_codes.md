# TS2Python DLL — Error Codes

`cpp/include/ts2python.h` 的 C ABI 每個進入點都回傳帶號 `int`。
非負值代表成功，負值代表特定失敗模式。**這些數字就是契約** —— EasyLanguage 端在需要
時應原樣 `Print` 出來。

| Code | 意義 | 由誰回傳 | 處理方式 |
|-----:|---|---|---|
| `0` | 成功 | 全部 | — |
| `1` | 在 DLL 已綁定後再次呼叫 init。沿用既有 socket，第二次為 no-op | `EL_Init3` | 不需處理。Indicator 可選擇不重複輸出 "init ok" |
| `-1` | 未初始化 —— 在成功的 init 之前呼叫了 publish | `EL_Publish` | 先呼叫 `EL_Init3` |
| `-2` | ZeroMQ 送出失敗。可能是觸及 high-water mark 導致 `send()` 回傳 `EAGAIN`，或非預期的 `zmq::error_t` | `EL_Publish` | 記錄後繼續，下一筆會重試。若持續發生，檢查 SUB 端是否存在 |
| `-3` | init 的 bind / socket 建立失敗 —— 最常見是 TCP endpoint 已被其他 process 佔用（或前一個 TradeStation session 殘留的 DLL handle） | `EL_Init3` | 檢查 `netstat -ano \| findstr :5555`，結束佔用者後重新 Verify indicator |
| `-4` | 參數無效。`zmq_endpoint` 或 `symbol` 為 null；**或 payload `snprintf` 被截斷**（代表數值輸入異常超出範圍） | `EL_Init3` `EL_Publish` | 上游資料問題，確認 EL indicator 傳入的型別 |
| `-6` | **ABI 不符 —— 呼叫端是早於本協定的 `.ELD`** | `EL_Init` `EL_Init2`（兩者皆為墓碑） | 重新匯入隨這顆 DLL 一起發布的 `.ELD`。見下節 |

## `-6` 與墓碑匯出

`EL_Init`、`EL_Init2`、`EL_PublishTick`、`EL_PublishBar` 都是前一代協定的匯出。它們
**仍然存在於 `.def` 裡**，但函式體只有 `return -6;`。

理由是 `__stdcall`：`EL_PublishTick` 與 `EL_PublishBar` 曾經沿用前一代的名字但**簽章不同**，
而 `__stdcall` 由被呼叫端清堆疊，所以簽章不符的呼叫會**損毀堆疊** —— 不是回傳錯誤碼，是
TradeStation 崩潰或隨機行為。

擋住它的是 init，不是名字：EasyLanguage 端的每一個 publish 呼叫都在「init 成功」的守衛
之內，init 失敗時 indicator 永遠走不到 publish。所以只要舊 `.ELD` 呼叫的那兩個 init 名字
回傳負值，改過簽章的 publish 函式就一次都碰不到。

把匯出直接刪掉也能達到目的（`DefineDLLFunc` 會解析失敗），但回 `-6` 能讓 operator 在
Print Log 看到一句 `EL_Init2 FAILED rc=-6`，而不是一個沒有上下文的解析錯誤。

反方向 —— 新 `.ELD` 配舊 DLL —— 由 `EL_Init3` 這個新名字擋住：舊 DLL 沒有這個匯出，
`DefineDLLFunc` 在 verify 階段就失敗。四種不相容組合的完整對照見
[`wire.md`](wire.md) 的〈新舊部署不相容時會發生什麼〉。

## `-2` 與靜默丟包的差別

`-2` 是**回報得出來**的送出失敗。真正危險的是 ZMQ PUB 在超過 `SNDHWM` 時的**靜默
丟棄** —— 那不會回傳錯誤碼，publisher 完全不知情。

錯誤碼涵蓋不到這個情況，這正是 payload 帶 `seq` 的原因。見
[`wire.md`](wire.md) 的〈傳輸保證〉與 [`semantics.md`](semantics.md) §6。

## 版本識別

`EL_DllVersion()` 回傳目前 DLL 的 ABI 版本（整數），本協定為 **2**。

indicator **應該**在 init 成功後檢查它：`EL_DllVersion` 是 0 參數的匯出，簽章永遠不會
變，所以呼叫它在任何 DLL 版本上都是安全的 —— 它是唯一可以無條件先問一句「你是誰」的
進入點。回值不符時 indicator 應停止發布並記錄，而不是繼續呼叫其他匯出。

## 新增錯誤碼的規範

- 新錯誤碼必須為**負值**，取下一個可用的絕對值。**永不重用已退役的碼**。
- `0` 以外的成功碼（如冪等 init 的 `1`）應該罕見，能用單一 `0` 就用。
- 新增碼時，本表與 `ts2python.h` 的註解區塊必須在**同一個 commit** 內更新。
