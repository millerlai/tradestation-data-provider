# TS2Python DLL — Error Codes

`cpp/include/ts2python.h` 的 C ABI 每個進入點都回傳帶號 `int`。
非負值代表成功，負值代表特定失敗模式。**這些數字就是契約** —— EasyLanguage 端在需要
時應原樣 `Print` 出來。

| Code | 意義 | 由誰回傳 | 處理方式 |
|-----:|---|---|---|
| `0` | 成功。socket 已綁定、有訂閱者、**這張圖的 hello 已送出** | `EL_InitChart` `EL_Publish` | — |
| `1` | 這張圖在本 session 已宣告過。沿用既有 socket，第二次為 no-op | `EL_InitChart` | 不需處理。Indicator 可選擇不重複輸出 "init ok" |
| `-1` | 未初始化 —— 在成功的 init 之前呼叫了 publish | `EL_Publish` | 先呼叫 `EL_InitChart` |
| `-2` | ZeroMQ 送出失敗。可能是觸及 high-water mark 導致 `send()` 回傳 `EAGAIN`，或非預期的 `zmq::error_t` | `EL_InitChart` `EL_Publish` | 記錄後繼續，下一筆會重試。若持續發生，檢查 SUB 端是否存在 |
| `-3` | socket 建立或 `connect()` 失敗 —— endpoint 字串無效，或 context / socket 建不出來 | `EL_InitChart` `EL_Shutdown` | 檢查 `ZMQEndpoint` 的字串。**這不再是「port 被佔用」**，見下節 |
| `-4` | 參數無效。`zmq_endpoint` 或 `symbol` 為 null；**或 payload `snprintf` 被截斷**（代表數值輸入異常超出範圍） | `EL_InitChart` `EL_Publish` | 上游資料問題，確認 EL indicator 傳入的型別 |
| `-6` | **ABI 不符 —— 呼叫端是早於本協定的 `.ELD`** | `EL_Init` `EL_PublishTick` `EL_PublishBar`（三者皆為墓碑） | 重新匯入隨這顆 DLL 一起發布的 `.ELD`。見下節 |
| `-7` | **尚無訂閱者。可重試，而且是啟動時的正常狀態** | `EL_InitChart` | 不需處理。Indicator 保持 `InitDone = False`，下一根 bar 再呼叫一次。**hub 沒開的表現也是這個** |
| `-8` | **endpoint 衝突** —— 這張圖要求的 endpoint 與第一張圖已綁定的不同 | `EL_InitChart` | 把該圖的 `ZMQEndpoint` input 改成與其他圖一致。見下節 |
| `-9` | **訂閱佇列讀不到** —— socket 故障，這次呼叫無從回答「有沒有人在聽」 | `EL_InitChart` | 真正的錯誤，不是可重試的啟動狀態。檢查 endpoint 與 socket 狀態。見下節 |
| `-10` | **這個 symbol 沒有訂閱者，這一筆資料已經遺失** | `EL_Publish` | 檢查 consumer 是否在跑、它的 symbol 清單是否包含這張圖、以及 **hub 是否還活著**。每個「無訂閱者事件」每張圖只回報一次。見下節 |

## `-3`：以前的處置建議是錯的

`-3` 過去的意思是「bind 失敗」，而文件給的處置是「用 `netstat` 找出佔用者、結束它、
重新 Verify」。那段建議設想的佔用者只有兩種 —— 別的程式，或前一個 TradeStation session
殘留的 handle —— **兩種都是外來的、可以殺掉的**。

它漏掉了第三種，而那正是實務上唯一會發生的一種：**佔用者是本次 session 自己的另一個
chart 程序**。TradeStation 10 每開一張圖就是一個新的 `orchart.exe`，第一個搶到 port，
其餘每一個都拿到 `-3`。照那段建議去殺掉佔用者，等於把裡面正常運作的圖一起弄死。

現在 DLL 是 **connect 側**，這個失敗模式不存在了：`connect()` 不會因為「已被佔用」失敗。
`-3` 剩下的意思是 endpoint 字串無效或 socket 建不出來 —— 真正罕見的狀況。
「沒有人在聽」由 `-7` 表達，「這一筆沒人收」由 `-10` 表達。

## `-7` 不是錯誤

ZeroMQ PUB/SUB 在沒有訂閱者時**靜默丟棄所有送出的訊息，且不回報任何東西**。前一代的
init 只要 bind 成功就回 0，於是 TradeStation 先開、consumer 後開的情況下，Print Log 印
著 "init ok"，而每一根 bar 都進了垃圾桶。

現在 DLL 的 socket 是 **XPUB** 而不是 PUB —— 差別在於訂閱事件會以可讀訊息的形式送回
publisher，所以 DLL 有辦法回答「到底有沒有人在聽」。`EL_InitChart` 在控制 topic 尚無訂閱者
之前回 `-7` 並且**不發布任何東西**。

Indicator 對任何負值 rc 都保持 `InitDone = False`，所以這件事會自己解決：consumer 一
起來，下一根 bar 的 `EL_InitChart` 就會回 0 並開始發布。指標只會在 Print Log 說一次
「waiting for a subscriber」，不會每根 bar 洗版。

**hub 沒開起來的表現也是 `-7`**，而且這是對的：hub 沒開就等於沒有人在聽。

### 「下一根 bar」可能是隔天開盤

重試的節奏不由 DLL 決定，也不是計時器 —— **EasyLanguage 的 indicator 只在有資料進來時
求值**。所以「下一根 bar」對一個盤前不跳動的 symbol（breadth 指數如 `$TICK`、`$ADD`、
`$VOLD`、`$TRIN`）可能是**隔天開盤**。

實測 2026-08-14：那批圖的第一根收盤 5 分鐘 bar 出現在 09:34:59，也就是 RTH 開盤後的第一
個 5 分鐘邊界，不是啟動時。這不是故障，但它意味著：**consumer 應該在 TradeStation 之前
起來**，否則某些 symbol 會安靜到下一次它們真的有成交為止。

> **這代表 `cpp/Release/TS2Python_TestHarness.exe` 必須先有訂閱者才跑得動。**
> 先開 `contract/tools/record.py`（或任何 SUB），否則 harness 會等到
> `--subscriber-timeout-ms` 逾時後以 `-7` 退出。

## `-8`：一個 process 只有一個 endpoint

DLL 的 socket 是**整個 process 共用一個**。哪張圖先跑到 `EL_InitChart` 就由它建立並
`connect`，之後每一張圖拿到的都是同一個 socket —— 這是刻意的，多張圖共用一條通道正是
這個設計的重點。（TradeStation 10 一張圖一個程序，所以這一條實務上很難踩到；規則不變。）

代價是後面的圖傳進來的 `zmq_endpoint` **無處可用**。以前那個參數會被靜默丟棄，然後回
`0`：Print Log 印著 "publishing starts now"，而那張圖的每一筆資料其實送往第一張圖選的
port。設在別的 port 上的 consumer 一整個 session 收不到東西，兩邊都沒有任何錯誤碼或
log 可以解釋。

現在這種情況回 `-8`。修法是把工作區裡所有圖表的 `ZMQEndpoint` input 改成同一個值 ——
若你真的需要兩條獨立通道，那需要的是兩個 process，不是兩個 endpoint。

## `-9`：讓 `-7` 保持誠實

訂閱事件是以**可讀訊息**的形式送回 XPUB 的，所以 DLL 判斷「有沒有人在聽」的唯一辦法
就是把那個佇列讀出來。這代表兩件完全不同的事會走到同一個結果：

| 事實 | 讀出來的訂閱集合 |
|---|---|
| 還沒有人訂閱 | 空 |
| socket 故障，佇列根本讀不到 | 空 |

以前兩者都回 `-7`。而 `-7` 在 indicator 端的語意是「正常啟動狀態」，只印一次就安靜
重試 —— 於是 socket 故障會表現成：consumer 明明在跑，Print Log 只有一行
"waiting for a subscriber"，整個 session 零筆發布，兩端都沒有錯誤。

現在讀取失敗回 `-9`，indicator 會每根 bar 印一次 `EL_InitChart FAILED rc=-9`。

**`EL_Publish` 不會回 `-9`，這是刻意的。** 它同樣每次呼叫都 drain，但它是**先 drain、
後送出**，而且手上已經有一筆真實資料 —— EasyLanguage 不重送失敗的 publish，所以在這裡
失敗等於把那根 bar**刪掉**而不是延後。訂閱簿記不值一個資料點；下一次 publish 會再
drain 一次。

## `-10`：這一筆資料送進了虛空

`-7` 這道閘門**只涵蓋 init**。EasyLanguage 的 `InitDone` 一旦為 True 就不再跑 init 區塊，
所以 consumer 在盤中重啟時，空窗期的每一根 bar 都送進一個沒有訂閱者的 socket ——
ZeroMQ 靜默丟棄、`send()` 回報成功，於是 `EL_Publish` 以前對一根**已經不存在於任何地方**
的 bar 回傳 `0`。這個 binding 不做 backfill，那些資料就是沒了。

問題是**逐 symbol** 判斷的，不是控制 topic：consumer 只訂閱它被設定的那些 symbol，
所以一張圖可以「宣告成功」之後每一筆都沒人收。

**每個事件每張圖只回報一次。** 一次普通的 consumer 重啟否則會讓每張圖的每根 bar 都在
Print Log 留一行（indicator 對任何負值 rc 都會印）。DLL 端記在 chart registry 上，
該 symbol 的訂閱者一回來就清除，下一次斷線會重新回報一次。

`EL_Publish` **仍然照送**：送出的成本是零，而且訂閱者有可能在檢查與送出之間接上。

### hub 斷線也算，而且它需要一個 socket monitor 才看得見

DLL 現在是 connect 側，而 libzmq 為了讓重連透明，**connect 側的 pipe 是跨重連保留的**
（`ZMQ_IMMEDIATE=0`，預設）。所以 hub 死掉時對端 pipe 不被銷毀、`xpipe_terminated`
不會跑、**不產生任何取消訂閱訊息** —— 訂閱集合永遠不會清空。

實測後果：hub 死著的時候 `EL_Publish` 對每一根 bar 回 `0`，而每一根都進虛空。
`send()` 二十萬次都沒有失敗過（XPUB 過了 SNDHWM 是靜默丟棄，不回錯誤），indicator 只在
`rc < 0` 才印，所以 Print Log 一行都不會有。那些 bar 也**補不回來**。

這正是 `-10` 存在要消滅的失敗模式，所以 DLL 在自己的 socket 上掛了一個 monitor：
收到 `ZMQ_EVENT_DISCONNECTED` 就清空訂閱集合，於是 `-10` 立刻開始回報，並在 hub 回來、
訂閱重新到達時自行恢復。**這個 monitor 不是最佳化，是 `-10` 在 hub 架構下成立的前提。**

## `-6` 與墓碑匯出

`EL_Init`、`EL_PublishTick`、`EL_PublishBar` 是前一代協定的匯出。三者**都仍然列在
`.def` 裡**，函式體只有 `return -6;`，而且**都保持前一代的簽章**。

保持簽章才是重點。`__stdcall` 由被呼叫端清堆疊，所以一個名字若在簽章改變後還活著，
不符的呼叫會**損毀堆疊** —— 不是回傳錯誤碼，是 TradeStation 崩潰或隨機行為。把舊名字
釘在**舊 arity** 上，舊 `.ELD` 的呼叫就會平衡、落在一個什麼都不做的函式上、拿到可讀的
`-6`。

**`EL_InitChart` 是其中關鍵的一個，因為 init 就是那道閘門**：每一個 publish 都在「init 成功」
的守衛之內，所以舊 `.ELD` 停在自己的 init，永遠碰不到改過簽章的 publish。

這道閘門曾經被放棄過一次：某一版把 `EL_Init` 這個名字**收回來重用**成五參數的 init。
舊 `.ELD` 綁的是單參數的 `EL_Init`，`DefineDLLFunc` 只按名字解析，於是它**解析得到、
呼叫得下去、然後就在 `EL_InitChart` 裡損毀堆疊** —— 沒有任何錯誤碼，DLL 這一側也看不到呼叫端
推了幾個參數。

**現在 init 的匯出名是 `EL_InitChart`，閘門回來了**，而且兩個方向都成立：

| 組合 | 結果 |
|---|---|
| 舊 `.ELD` + 新 DLL | 解析到單參數 `EL_Init` 墓碑，堆疊平衡，Print Log 得到 `-6` |
| 新 `.ELD` + 舊 DLL | 舊 DLL 沒有 `EL_InitChart`，`DefineDLLFunc` 在 Verify 階段失敗，什麼都不會跑 |

**DLL 與 `.ELD` 仍然是同一個單位，必須一起安裝、一起重新 Verify** —— 差別在於現在
裝錯了會得到一行可讀的訊息，而不是一次崩潰。四種不相容組合的完整對照見
[`wire.md`](wire.md) 的〈新舊部署不相容時會發生什麼〉。

## `-2` 與靜默丟包的差別

`-2` 是**回報得出來**的送出失敗。真正危險的是 ZMQ PUB 在超過 `SNDHWM` 時的**靜默
丟棄** —— 那不會回傳錯誤碼，publisher 完全不知情。

錯誤碼涵蓋不到這個情況，這正是 payload 帶 `seq` 的原因。見
[`wire.md`](wire.md) 的〈傳輸保證〉與 [`semantics.md`](semantics.md) §6。

## 版本識別

`EL_DllVersion()` 回傳目前 DLL 的 ABI 版本（整數），本協定為 **4**。

ABI 版本**不是** wire 版本。point frame 一個 byte 都沒變，仍然是 `proto` 2，所有已錄製的
fixture 全部繼續有效。變的是 C ABI 與一個走獨立 topic 的新增控制 frame —— 只讀 point 的
consumer 根本不會訂閱到它。

`3` → `4` 是 init 匯出改名為 `EL_InitChart`。**改一個匯出的名字就是 ABI 變更**，這個號碼
存在的目的正是說出這件事。

indicator **應該**在 init 成功後檢查它：`EL_DllVersion` 是 0 參數的匯出，簽章永遠不會
變，所以呼叫它在任何 DLL 版本上都是安全的 —— 它是唯一可以無條件先問一句「你是誰」的
進入點。回值不符時 indicator 應停止發布並記錄，而不是繼續呼叫其他匯出。

## 新增錯誤碼的規範

- 新錯誤碼必須為**負值**，取下一個可用的絕對值。**永不重用已退役的碼**。
- `0` 以外的成功碼（如冪等 init 的 `1`）應該罕見，能用單一 `0` 就用。
- 新增碼時，本表與 `ts2python.h` 的註解區塊必須在**同一個 commit** 內更新。
