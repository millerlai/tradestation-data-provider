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
| 6 | 1 | **目前** | `kind` 區分 `tick` / `bar_1m`；`ts` / `ts_utc` / `ts_str` 三時間戳。無送達保證、無缺漏偵測 |
| 7 | 2 | **規劃中** | v1 全部欄位 + `seq`（per-symbol 單調遞增）+ `sid`（publisher session id） |

> ABI 與 wire 目前一對一，但**不保證永遠如此** —— 例如修正 DLL 的執行緒安全問題會
> 升 ABI 而不動 wire。新增列時務必同時填寫兩欄。

## Binding 的版本處理義務

### 必須向下相容

subscriber 讀到 `"v": 1`（無 `seq` 欄位）時：

- **降級為不偵測缺漏**，記錄**一次**警告
- **不得拒收、不得拋錯**

理由：舊版 DLL 可能仍部署在使用者的 TradeStation 上，而 DLL 的更新不受 binding
控制。強制要求 v2 會讓升級 binding 直接中斷資料收集。

### 必須拒絕未知的高版本

讀到 `v` 大於自己支援的最高版本時，binding **應拒收並明確報錯**，而非猜測欄位語意。
未知的高版本代表 payload 可能有本 binding 不理解的語意變更。

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
