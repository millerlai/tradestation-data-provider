# contract — 語言中立規格 (SSoT)

本目錄定義 `TS2Python.dll` 送上 ZeroMQ 的資料契約。**它是本 repo 的產品**；
各語言的 subscriber binding（含 `bindings/python/`）都只是這份契約的實作。

## 讀者

- **要寫新語言 binding 的人** — 從這裡開始，讀完就足以正確實作，不需要讀任何
  既有 binding 的原始碼。
- **要改 wire 格式的人** — 先改這裡，再改 `cpp/`，最後改各 binding。

## 內容

| 檔案 | 內容 |
| --- | --- |
| [`v3/envelope.md`](v3/envelope.md) | **目前版本** —— frame 結構與 payload 格式 |
| [`v3/tick.schema.json`](v3/tick.schema.json) | `EL_PublishTick` payload |
| [`v3/bar.schema.json`](v3/bar.schema.json) | `EL_PublishBar` payload（含 `tf`） |
| [`v2/`](v2/) [`v1/`](v1/) | 舊版。**仍須支援** —— 舊 DLL 可能留在使用者機器上 |
| [`semantics.md`](semantics.md) | **schema 管不到但 binding 必須一致的規則** |
| [`error_codes.md`](error_codes.md) | DLL C ABI 回傳碼 |
| [`compat.md`](compat.md) | DLL ABI × wire version 相容矩陣 |
| [`fixtures/`](fixtures/) | 錄自真實 DLL 的 frame + 語言中立期望解析結果 |
| [`tools/`](tools/) | fixture 錄製工具 |

## 最重要的一件事

**`semantics.md` 比 schema 重要。** JSON Schema 只能驗證「欄位存在、型別正確」，
但 binding 之間真正會產生分歧的是**語意**：

- 哪個時間戳是 bar 邊界的權威來源
- bar 用左標籤還是右標籤
- 哪些 symbol 的 `bid`/`ask` 該視為無效
- 收到比預期小的 `seq` 時該不該回退期望值
- 各 timeframe 的 bucket 錨在哪裡

這些在 wire 上看不出來。一個只讀 schema 就動手寫的 binding，會產出**通過驗證但與
其他 binding 不一致**的資料。

## 規範的權威來源

本目錄的內容以 **`cpp/src/ts2python.cpp` 的實作為準**。

> 歷史教訓：規格曾寫在消費端 repo 的 `docs/design.md` §5，結果與實作脫節而長期無人
> 發現 —— 該文件描述的欄位是 `ts_el`，實作早已改為 `ts_utc` + `ts_str`；文件沒有
> `kind` 欄位，實作已支援 `bar_1m`；文件說用 `mktime`（system-local），實作用的是
> `std::chrono::zoned_time`（America/New_York）。
>
> 契約與實作放在同一個 repo、且被 conformance fixtures 綁住，就是為了讓這種漂移
> 在 CI 就爆掉。
