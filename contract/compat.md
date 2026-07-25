# 相容矩陣 — DLL ABI × wire version

三個版本號各自獨立遞增，本文件是它們的對應關係。

| 版本 | 由誰決定 | 如何查詢 |
| --- | --- | --- |
| **DLL ABI** | `cpp/` | `EL_DllVersion()` |
| **wire version** | `contract/` | payload 的 `v` 欄位 |
| binding 套件版本 | 各 binding | 各語言的套件管理工具 |

binding 的使用者 pin 的是套件版本，但**真正決定能不能通的是前兩者**。

## 矩陣

| DLL ABI | wire | 狀態 | 內容 |
| ---: | ---: | --- | --- |
| 6 | 1 | 已淘汰，仍須支援 | `kind` 區分 `tick` / `bar_1m`；`ts` / `ts_utc` / `ts_str` 三時間戳。無送達保證、**無缺漏偵測**；`bid`/`ask` 恆為數字，`0` 代表無報價 |
| 7 | 2 | 已淘汰，仍須支援 | v1 全部欄位 + `seq` + `sid`；`bid`/`ask` 可為 `null` |
| 8 | 3 | **目前** | `kind` 收斂為 `tick` / `bar`，新增 `tf` 表區間；新增匯出 `EL_PublishBar`，回傳碼 `-5` 表示無法對應的區間 |

ABI 6 標為「仍須支援」而非單純淘汰：DLL 部署在使用者的 TradeStation 上，**不受
binding 升級控制**。舊 DLL 可能存活很久。

> ABI 與 wire 目前一對一，但**不保證永遠如此** —— 例如修正 DLL 的執行緒安全問題會
> 升 ABI 而不動 wire。新增列時務必同時填寫兩欄。

## Binding 的版本處理義務

### 必須向下相容

subscriber 讀到 `"v": 1`（無 `seq` 欄位）時：

- **降級為不偵測缺漏**，記錄**一次**警告
- **不得拒收、不得拋錯**

理由：舊版 DLL 可能仍部署在使用者的 TradeStation 上，而 DLL 的更新不受 binding
控制。強制要求 v2 會讓升級 binding 直接中斷資料收集。

**降級時 `messages_lost` 恆為 0，但那代表「無從得知」而非「沒有遺失」。** binding
的 API 必須讓呼叫端能區分這兩者，否則使用者會拿一個沒有意義的 0 去判斷資料可信度。
見 [`semantics.md` §6.6](semantics.md)。

### 必須拒絕未知的高版本

讀到 `v` 大於自己支援的最高版本時，binding **應拒收並明確報錯**，而非猜測欄位語意。
未知的高版本代表 payload 可能有本 binding 不理解的語意變更。

### `tf` 未知時 —— 必須拒收，不得歸入預設

這與 `kind` 未知的處理**相反**。未知的 `kind` 是「不認識的事件型別」，跳過即可；
未知的 `tf` 是「認得這是 bar，但不知道它涵蓋多長」——若以預設值歸檔，該 bar 會進到
錯誤的 `timeframe=` 分區，與真正屬於那個區間的資料混在一起，**下游再也分不出來**。

拒收並記錄，不要猜。

### `kind` 未知時

`kind` 不在已知集合內時，**跳過該訊息並記錄**，不得拋錯 —— 未來新增事件型別
（例如 `bar_5m`）時，舊 binding 才能安全忽略。

## 升版檢查清單

新增 wire 版本時：

- [ ] `contract/v<N>/` 新增 schema，舊版目錄保留
- [ ] `semantics.md` 標註哪些規則因版本而異
- [ ] 本矩陣新增一列
- [ ] `fixtures/` 新增該版本的錄製樣本，**舊版 fixtures 保留**（向下相容測試的依據）
- [ ] 各 binding 的 conformance 測試同時跑新舊 fixtures
- [ ] `cpp/` 的 `EL_DllVersion()` 依需要遞增
