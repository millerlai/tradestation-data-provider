# TS2Python DLL — Error Codes

`cpp/include/ts2python.h` 的 C ABI 每個進入點都回傳帶號 `int`。
非負值代表成功，負值代表特定失敗模式。**這些數字就是契約** —— EasyLanguage 端在需要
時應原樣 `Print` 出來。

| Code | 意義 | 由誰回傳 | 處理方式 |
|-----:|---|---|---|
| `0` | 成功 | 全部 | — |
| `1` | 在 DLL 已綁定後再次呼叫 init。沿用既有 socket，第二次為 no-op。兩個 init 匯出共用同一個守衛，混用也一樣 | `EL_Init` `EL_Init2` | 不需處理。Indicator 可選擇不重複輸出 "init ok" |
| `-1` | 未初始化 —— 在成功的 init 之前呼叫了 publish | `EL_PublishTick` `EL_PublishBar` `EL_PublishTickEx` | 先跑 `Once` 區塊 / 呼叫 `EL_Init2` |
| `-2` | ZeroMQ 送出失敗。可能是觸及 high-water mark 導致 `send()` 回傳 `EAGAIN`，或非預期的 `zmq::error_t` | `EL_PublishTick` `EL_PublishBar` `EL_PublishTickEx` | 記錄後繼續，下一筆會重試。若持續發生，檢查 SUB 端是否存在 |
| `-3` | init 的 bind / socket 建立失敗 —— 最常見是 TCP endpoint 已被其他 process 佔用（或前一個 TradeStation session 殘留的 DLL handle） | `EL_Init` `EL_Init2` | 檢查 `netstat -ano \| findstr :5555`，結束佔用者後重新 Verify indicator |
| `-4` | 參數無效。`zmq_endpoint` 或 `symbol` 為 null；`publisher_version` 為負；**或 payload `snprintf` 被截斷**（代表數值輸入異常超出範圍） | `EL_Init` `EL_Init2` `EL_PublishTick` `EL_PublishBar` `EL_PublishTickEx` | 上游資料問題，確認 EL indicator 傳入的型別 |
| `-5` | `bar_type` / `bar_interval` 無法對應到任何 wire timeframe | `EL_PublishBar` | **不是錯誤處理問題，是設定問題**：把 indicator 掛到支援的圖表間隔上（1/5/15/30/60 分或日線）。DLL 刻意不猜 —— 猜錯會把某區間的 bar 歸進另一區間的分區，下游偵測不到 |

## `-2` 與靜默丟包的差別

`-2` 是**回報得出來**的送出失敗。真正危險的是 ZMQ PUB 在超過 `SNDHWM` 時的**靜默
丟棄** —— 那不會回傳錯誤碼，publisher 完全不知情。

錯誤碼涵蓋不到這個情況，這正是 wire v2 加入 `seq` 的原因。見
[`v1/envelope.md` 的「傳輸保證」](v1/envelope.md) 與 [`compat.md`](compat.md)。

## 版本識別

`EL_DllVersion()` 回傳目前 DLL 建置版本（整數）。它**獨立於 wire protocol 版本**
（payload 的 `v` 欄位）遞增。兩者的對應關係見 [`compat.md`](compat.md)。

## 新增錯誤碼的規範

- 新錯誤碼必須為**負值**，取下一個可用的絕對值。**永不重用已退役的碼**。
- `0` 以外的成功碼（如冪等 init 的 `1`）應該罕見，能用單一 `0` 就用。
- 新增碼時，本表與 `ts2python.h` 的註解區塊必須在**同一個 commit** 內更新。
