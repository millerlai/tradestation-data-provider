# wire — envelope（`proto` 2）

> 權威來源：`cpp/src/ts2python.cpp`（`EL_Publish`）。
> 本文如與實作不符，以實作為準並修正本文。
>
> 本文自成一體。**沒有舊版本可以相容** —— 理由見〈為什麼版本欄位叫 `proto` 而不是 `v`〉
> 與〈新舊部署不相容時會發生什麼〉。

## Transport

**三方，中間一個 forwarder。兩端都 `connect`，`bind` 的是 hub。**

| 角色 | Socket | 動作 | 預設 endpoint |
| --- | --- | --- | --- |
| Publisher（DLL） | `XPUB` | `connect` | `tcp://127.0.0.1:5555` |
| **hub 前台** | `XSUB` | **`bind`** | `tcp://127.0.0.1:5555` |
| **hub 後台** | `XPUB` | **`bind`** | `tcp://127.0.0.1:5556` |
| Consumer | `SUB` | `connect` | `tcp://127.0.0.1:5556` |

| 項目 | 值 |
| --- | --- |
| 送達保證 | **無**，但可偵測（`seq`）；「有沒有人在聽」則可確知 |
| 對應 DLL ABI | `EL_DllVersion() == 4` |

publisher 端是 **XPUB 而不是 PUB**。送出語意完全相同；差別是訂閱事件會以可讀訊息回到
publisher，所以 DLL 有辦法回答「到底有沒有人在聽」。這是 `EL_InitChart` 能夠在無人訂閱時
回 `-7` 並拒絕發布的唯一依據 —— PUB 在沒有訂閱者時靜默丟棄一切且不回報任何東西。

consumer 端是普通的 `SUB` 且仍然 `connect`，**除了 port 之外不需要任何改動**。

## 為什麼需要 hub

TradeStation 10 用 `-multiexe` 把圖表分散到多個 `orchart.exe` 程序，**每開一張圖就是一個
新的程序**，而 DLL 的狀態是每個程序各一份（普通全域，沒有共享節區）。

`bind()` 天生獨佔。DLL 過去直接 bind，於是只有搶到 port 的那一個程序能發布，其餘每一個
都拿到 `-3`，而且佔用者是同一個 TradeStation session 的兄弟程序，活到 TradeStation 關掉
為止 —— 實測 25 個 symbol 只有 6 張圖在發。

不對稱的是 **bind 獨佔而 connect 不獨佔**，不是「port 是固定的」。改成動態 port 只會把
「綁不到」換成「找不到」。所以承受「有很多個」的那一側必須是 connect 側，也就是 publisher。

**但兩端都 connect 就沒有人 bind 了**，所以中間需要一個 bind 兩個 port 的 forwarder。
它同時換回一件事：多個 consumer 仍然各自 connect，互不干擾。

### 為什麼不是「consumer 直接 bind」

那是最少零件的做法，而且**行不通**，理由值得寫下來因為它看起來顯然可行：

ZMQ 的訂閱必須**逆流**傳到 publisher（3.0 起 topic 過濾在 publisher 端做），而在 bind 側，
訂閱只會送給「送出那一瞬間已經連上的」publisher。之後才連上的一律收不到，而且 bind 側的
subscriber **沒有任何事件可以知道有新 publisher 接上了**（`SUB` / `XSUB` 都沒有）。
一張圖一個程序、陸續啟動 —— 每一張圖都是晚加入者，會全部卡在 `-7`。

hub 能成立的關鍵不是「它是中介」，而是**它的 XSUB 在 bind 側、可以掛 socket monitor**，
因此拿得到「有新 publisher 接上」這個事件。那是這條路上唯一能取得它的位置。

## hub 的義務

**以下每一條少做一條，就有一個保證會靜默失效。** 這些不是實作細節：它們繞的是四個
libzmq 未文件化的行為（見下節），任何語言的重寫都必須逐條滿足。

1. `XSUB` **bind** 前台，面向 publisher。
2. `XPUB` **bind** 後台，面向 consumer，且**必須**設 `ZMQ_XPUB_VERBOSE`。理由與 DLL 設它
   的理由相同：consumer 重啟時新舊訂閱者可能重疊幾毫秒，非 verbose 的 XPUB 會把後來那個
   當成重複訂閱吃掉，重連的 consumer 就一個 hello 都收不到。
3. 前台收到的訊息**原封不動**轉發到後台。hub **絕不解析 payload** —— 它是 transport，
   不是 binding。
4. 後台收到 `\x01topic` → **原樣轉發到前台一次**，並把該 topic 的轉發次數 +1。
5. 後台收到 `\x00topic` → 往前台送出**與轉發次數相同數量**的 `\x00topic`，並清除計數。
   若該 topic 從未被記錄過（計數為 0），仍然**至少送出一筆**。這個下限是刻意的：兩種
   偏差的代價不對稱 —— 多送一筆，上游 `rm()` 回 false、不轉發，完全無害；少送一筆，
   publisher 會永遠以為有人在聽，`-10` 從此不再出現。追蹤本身若有 bug，寧可被這個下限
   蓋掉，也不要變成靜默的資料遺失。
6. 前台掛 socket monitor（`ACCEPTED` / `HANDSHAKE_SUCCEEDED`）。每次事件、以及每次訂閱
   集合變動，都排定數次 **wake**：往前台送 `\x01__ts2py_wake__` 緊接 `\x00__ts2py_wake__`。
7. **真實訂閱永不重送。** 喚醒只用那個 nonce topic。
8. `LINGER = 0`；前台 `RCVHWM`、後台 `SNDHWM` 比照 publisher 端的量級（1M / 100k）。
9. 前台 bind 失敗必須以可讀訊息結束，並點名兩個可能：另一個 hub，或一顆反轉之前、
   仍然會 bind 的舊 `TS2Python.dll`。

> nonce 的名字要同時滿足兩件事：不是 `__ts2py__` 的前綴，也不被 `__ts2py__` 前綴。
> `__ts2py_wake__` 的第 8 個字元是 `w` 而控制 topic 是 `_`，兩邊互不覆蓋 —— 所以它既不會
> 誤觸 hello 重播，也不會被當成控制 topic 的訂閱。

不變式：**轉發計數是 hub 對上游 XSUB 訂閱樹的鏡射，而 hub 是該 socket 唯一的寫入者。**
任何繞過計數的寫入都會讓鏡射失真，取消訂閱就再也送不出去。

## 四個 libzmq 行為，以及上面每一條在繞什麼

沒有任何一條出現在 zguide 或 `zmq_proxy(3)` 裡。全部只在「subscriber 在 bind 側、
publisher 在 connect 側」這個組合上顯現，量測於 libzmq 4.3.5。

| | 行為 | 依據 | 被哪一條繞掉 | 少了會怎樣 |
| --- | --- | --- | --- | --- |
| L1 | 全新 pipe 的**第一次寫入會擱淺**：`ypipe_t::flush()` 在新 pipe 上回 `true`，`pipe_t::flush()` 因此跳過 `send_activate_read`，而 XPUB 讀訂閱只有 `process_activate_read` 一條路。任何後續寫入會把它們一起釋出 | `src/ypipe.hpp`、`src/pipe.cpp` | 第 6 條（wake） | `xattach_pipe` 替新 publisher 重放的訂閱沒人讀 → 晚啟動的圖卡在 `-7`。**但見下方的但書** |
| L2 | XSUB 的訂閱**引用計數**：`xsub_t::xsend` 每次訂閱 `add()`，取消訂閱只在 `rm()` 回 true 時才轉發 | `src/xsub.cpp` | 第 4、7 條 | 重送真實訂閱會灌大計數，取消訂閱再也送不出去 → **`-10` 靜默死亡** |
| L3 | `ZMQ_XPUB_VERBOSE` 回報**每一次**訂閱，但只回報**最後一次**取消訂閱 | `src/xpub.cpp` | 第 5 條（計數鏡射） | N 個 consumer 訂同一 topic 時上游計數停在 N-1 → 全部離線後 publisher 仍以為有人在聽，`-7` 失準 |
| L4 | connect 側的 pipe **跨重連保留**（`ZMQ_IMMEDIATE=0`，預設），對端死亡不觸發 `xpipe_terminated`，因此不產生取消訂閱 | `src/session_base.cpp` | 不由 hub 繞，由 **DLL 的 socket monitor** 繞 | hub 死掉時 DLL 的訂閱集合永不清空，`EL_Publish` 對每根 bar 回 `0` 而資料進虛空，兩端都沒有任何訊號 |

### L1 的但書：wake 是有意保留的、沒有測試涵蓋的機制

L1 的擱淺現象是量出來的：一個**不做 poll** 的 XSUB，在 publisher 接上之後等 10 秒，
`xattach_pipe` 重放的訂閱一筆都沒送達；把 XSUB 關掉（termination 強制排空）才突然出現。

但**一個持續 `poll()` 的 XSUB 會自己把它釋出** —— 同樣的情境，不再送任何東西、只是持續
poll，訂閱在 10 秒內就到了 publisher。實作上 hub 本來就是一個 poll 迴圈，所以第 6 條的
wake 在這個形狀下是雙保險，而且**任何測試都無法證明它必要**：把 wake 拿掉，測試照樣全綠。

這件事必須寫在這裡，因為它有兩個相反的陷阱：

- 把 wake 當成死碼刪掉 —— 那是在賭 libzmq 命令處理的一個**未文件化的副作用**，它不是保證，
  而且換一個實作形狀（例如事件驅動而非輪詢的 hub）就不成立；
- 以為測試涵蓋了它 —— 沒有。這是**已知且刻意保留的測試缺口**，不是疏漏。

**L2 與 L3 不同**：兩者都有自動化測試會在移除機制後轉紅（實測過 —— 把
`decide_subscription_forward` 的 `\x00` 分支從「乘上鏡射計數」改成只送一筆，
`test_unsubscribe_reaches_publisher_once_every_consumer_is_gone` 與對應的單元測試立刻轉紅）。

**L4 是第三種情況：它有證據，但沒有自動化測試。** 它的機制在 C++（DLL 的
`drain_monitor()`），而 CI 只跑 Python，根本不編譯 C++。它是用真實 DLL 手動驗證的：

```
harness --mode stress --seconds 16 --rate 20，中途殺掉 hub
  hub 全程存活    ->  sent=320 failed=0
  hub 第 6 秒被殺  ->  sent=114 failed=206
```

那 206 筆就是「沒有 monitor 的話會靜默消失、而且 `EL_Publish` 回 0」的資料。
**改動 `drain_monitor()` 或 `g_monitor` 之後，必須用手重跑這個情境** —— 沒有任何測試會替你發現它壞了。

### L3 的標準修法在這個版本上不存在

`ZMQ_XSUB_VERBOSE_UNSUBSCRIBE` 是 L3 的正解。**libzmq 4.3.5 不支援它**，`setsockopt`
回 `EINVAL`（pyzmq 有常數但底層沒有實作）。升級 libzmq 之後若要改用它，**必須先重跑
「所有 consumer 離線 → publisher 收到取消訂閱」那條測試**。

L2 / L3 的實測數字（XSUB 的 pipe 已接上且醒著，排除 L1 的干擾）：

```
送 3 次 \x01SPY  ->  publisher 收到 3 次   （訂閱不去重，每次都讓計數 +1）
送 3 次 \x00SPY  ->  publisher 收到 1 次   （只有把計數歸零的那一次會轉發）
```

這個不對稱就是第 4、5 條存在的全部理由。

`ZMQ_IMMEDIATE=1` 看起來像 L4 的一行修法（它確實讓取消訂閱出現），但它同時讓 hub 重啟後
資料**完全不再流動** —— 把暫時失效換成永久失效。不要用。

## 版本歪斜怎麼被發現

| 組合 | 症狀 |
| --- | --- |
| 舊 DLL（會 bind 5555）+ hub | 誰先起誰贏，另一邊拿到 `EADDRINUSE` / `-3`，兩者都有可讀訊息 |
| 新 DLL + 沒開 hub | 收不到訂閱 → `-7` → Print Log 的「waiting for a subscriber」。這**就是**正常啟動狀態的訊息 |
| 舊 consumer（connect 5555） | 撞上 hub 的 XSUB，socket 型別不相容 → 靜默。由 binding 的啟動診斷攔下（見〈binding 的義務〉） |

`proto` 與 `EL_DllVersion` 都**沒有變**：point frame 一個 byte 都沒動，C ABI 的匯出名與
簽章也沒動。transport 拓樸不是這兩個號碼描述的東西。

## Frame 結構

2 個 frame（`ZMQ_SNDMORE` 串接）：frame 1 是 UTF-8 topic，frame 2 是 UTF-8 JSON
payload。**逐一精確訂閱，並在收訊後以字串完全相等再過濾一次**
（ZMQ 訂閱是 prefix match，訂 `SPY` 會收到 `SPYG`）—— 見 [`semantics.md`](semantics.md) §5。

topic 有兩種，**而 topic 本身就是 payload 形狀的鑑別子**：

| topic | payload | 產生者 |
| --- | --- | --- |
| symbol（`SPY`、`$VIX.X`…） | data point | `EL_Publish` |
| `__ts2py__`（固定） | chart 宣告（hello） | `EL_InitChart` |

沒有新增 `kind` 欄位，point frame 一個 byte 都沒變。

## Payload —— 只有一種

**一個 frame 形狀,不論來自什麼圖。** 沒有 `kind`,也沒有 `tf`。

```json
{
  "proto": 2,
  "seq": 1,
  "sid": 1785646054360588,
  "ts": 1785646062.364744,
  "ts_str": "2026-04/18-13:30:45",
  "bar_type": 0,
  "bar_interval": 1,
  "category": 2,
  "o": 450.0, "h": 450.0, "l": 450.0, "c": 450.0,
  "el_volume": 100, "el_ticks": 180,
  "el_upticks": 100, "el_downticks": 80, "el_open_interest": 80,
  "bid": 449.99, "ask": 450.01
}
```

| 欄位 | 型別 | 意義 |
| --- | --- | --- |
| `proto` | int | 協定版本。**目前只有 2**,缺這個鍵就不是這個協定 |
| `seq` | int | 每個 symbol 各自單調遞增。**每一個 frame 都必須有** |
| `sid` | int | publisher session id。DLL 重啟會變 —— 那是重置,不是遺漏 |
| `ts` | float | DLL 收訊端 wall clock（UTC epoch 秒）。量測延遲用,也是 `ts_str` 缺席時的最後手段 |
| `ts_str` | string | EL 的 `Date` + `Time`,`yyyy-MM/dd-HH:mm:ss`,ET 牆鐘,逐字。**`bar_time` 的權威來源**,原樣落地（`semantics.md` §2） |
| `bar_type` | int | EL 的 `BarType`,逐字。0 = tick 序列,1 = 盤中分鐘,2 = 日線 |
| `bar_interval` | int | EL 的 `BarInterval`,逐字。`bar_type` 為 1 時就是分鐘數 |
| `category` | int | EL 的 `Category`,逐字。0 期貨 / 2 股票 / 3 股票選擇權 / 4 指數 …（`semantics.md` §3.5） |
| `o` `h` `l` `c` | float | EL 的 `Open`/`High`/`Low`/`Close`。1-tick 序列上四者是同一筆成交 |
| `el_volume` `el_ticks` `el_upticks` `el_downticks` `el_open_interest` | int | EL 的五個保留字,逐字（`semantics.md` §3.4） |
| `bid` `ask` | float \| null | EL 的 `InsideBid` / `InsideAsk`,publisher 沒有報價時為 `null` |

### 為什麼不再分 tick 與 bar

wire 曾經有兩種形狀,用 `kind` 區分:tick 只送 `Close`、丟掉 `BarType`/`BarInterval`;
bar 送 OHLC、丟掉 `bid`/`ask`。兩邊都在**丟掉圖表已經提供的欄位**,依據是這個
publisher 自己對「哪些數字在哪種圖上有意義」的判斷 —— 而那個判斷發生在 wire 之外,
消費端看不出它做過。

TradeStation 對每一種圖都提供同一組保留字。1-tick 序列的 `Open = High = Low = Close`
是一個**事實**,值得落地,不是值得省略的冗餘。全部送出去也是唯一能撐過
「TradeStation 改變某個字的定義」的做法:這一層沒有會過時的意見。

### 為什麼 `bar_type` / `bar_interval` 不再映射成 `tf`

DLL 曾經把這一對映射成 `"5m"`、`"1d"` 之類的字串,並對**映射不出來的組合回 `-5`、
整根不送**。2 分鐘圖、週線圖、2 日圖因此完全不會出現在 wire 上。

現在原值直接上 wire,不映射也不拒收。落地的分區就是 `bartype={N}/interval={M}/`,
所以「這個 binding 沒有名字的間隔」不再等於「這筆資料不存在」。

## hello —— chart 宣告 frame

topic 固定為 **`__ts2py__`**。由 `EL_InitChart` 送出，每張圖一筆。

```json
{
  "proto": 2,
  "seq": 1,
  "sid": 1786079270794516,
  "ts": 1786079271.165191,
  "symbol": "$VIX.X",
  "category": 4,
  "bar_type": 1,
  "bar_interval": 1
}
```

| 欄位 | 型別 | 意義 |
| --- | --- | --- |
| `proto` | int | 恆為 `2`。point frame 沒有變，所以 wire 版本沒有變 |
| `seq` | int | 控制 topic 自己的序號，與各 symbol 的序號互不相干 |
| `sid` | int | publisher session id，與 point frame 同一個值 |
| `ts` | float | DLL 收到 `EL_InitChart` 呼叫的 UTC epoch 秒 |
| `symbol` | string | EL `GetSymbolName` |
| `category` | int | EL `Category`，逐字（§3.5 的表） |
| `bar_type` | int | EL `BarType`，逐字 |
| `bar_interval` | int | EL `BarInterval`，逐字 |

它**不是 data point**：沒有 OHLC、沒有五個量值、沒有報價。不要用 point 的 schema 去
驗它，也不要把它交給消費 point 的路徑。

### 為什麼是獨立 topic 而不是欄位

consumer 是**逐 symbol 訂閱**的，而且訂閱清單來自它自己的設定檔。所以一張掛在「設定檔
裡沒有的 symbol」上的圖，如果把 hello 發在該 symbol 的 topic 上，就**永遠不會被任何人
收到** —— 而那恰好正是 operator 最需要被告知的情況（圖開著、資料進不來、分區空著）。

固定 topic 解決這件事，而且順帶讓 point frame 完全不用動：鑑別子是 topic，不是新欄位。
`kind` 是這個 repo 已經移除過一次的東西，不會再加回來。

`__` 開頭是為了離開 TradeStation 的 symbol 空間 —— ZMQ 訂閱是 prefix match，一個可能
成為真實 symbol 前綴（或被真實 symbol 前綴）的 topic 會互相誤送。

### binding 的義務

1. **必須訂閱 `__ts2py__`，而且與 symbol 清單無關。** 這不是選配：publisher 在這個
   topic 上看不到訂閱者之前，`EL_InitChart` 回 `-7` 且**什麼都不發**。沒訂的 consumer 會讓
   每一張 TradeStation 圖無限期空轉。
2. **不得把 hello 當成 data point 交給下游。**
3. **收到 hello 時必須說話。** symbol 在訂閱清單內 → 記錄「開始接收」並附上四個欄位；
   不在清單內 → **警告**，並說明這張圖的資料收不到。兩者不可混為一談。
4. **一個壞掉的 hello 不得中斷串流。** 它不是市場資料，丟掉不損失任何一根 bar；讓例外
   逸出會殺掉 ingest 迴圈。
5. `symbol` 必須檢查是不是字串。JSON `null` 經 `str()` 會變成字串 `"None"`，然後以一個
   看起來像真 symbol 的名字被登記下來。
6. **啟動後若一個 frame 都沒收到，必須說一次話。** hub 沒開、連錯 port、或兩端 transport
   方向不一致，在 consumer 這一側全部長得一模一樣：安靜。而收盤後安靜是正常的，所以這個
   訊號只能由 binding 自己給。附上 endpoint 與已訂閱的 symbol 數，並沿用 `-7`「只說一次」
   的慣例 —— 它是「連上了沒」的診斷，不是持續監控。

### 重播：consumer 重開不需要動 TradeStation

DLL 記住每一張呼叫過 `EL_InitChart` 的圖。**每收到一則涵蓋控制 topic 的訂閱訊息，就把所有
已知的圖重新宣告一次。**

這是必要的：`EL_InitChart` 一張圖只跑一次（在該圖的第一根 bar），TradeStation 不會為了一張
已經開著的圖再呼叫一次。沒有這個重播機制，consumer 重啟後就再也學不到工作區裡有什麼，
除非人工把每一張圖重新 Verify。

觸發條件是**訂閱訊息本身**，不是「訂閱者數由 0 變 1」。這一點是量出來的：consumer 重啟
時，新舊兩個訂閱者可能重疊幾毫秒，libzmq 因此從未看到該 topic 掉到零訂閱者 —— 用邊緣
判斷時，**重連的 consumer 一個 hello 都收不到**，而同一個測試隔六秒再連就兩個都收到。

DLL 因此設定 **`ZMQ_XPUB_VERBOSE`**。預設的 XPUB 每個 topic 只回報**第一個**訂閱者，
重疊的那一個會被當成重複訂閱吃掉。

代價是：一個 consumer 若同時訂閱兩個都涵蓋控制 topic 的 topic（例如 `""` 與
`__ts2py__`），會收到重複的 hello。重複的宣告在消費端是冪等的；漏掉的宣告則讓 consumer
對整個工作區一無所知。

## 為什麼版本欄位叫 `proto` 而不是 `v`

前一代的 wire 用 `"v"`，版本號一路走到 `4`。本協定是重寫，版本從 `1` 重新起算 ——
**如果沿用 `"v"`，`{"v":1}` 就會同時是本協定與前一代第一版的合法開頭。**

那個碰撞不會產生錯誤，會產生錯誤的資料：

- 前一代 v1 的 bar 用 `"kind":"bar_1m"`。新 binding 的版本閘門會**放行**（`v == 1`），
  然後在 `kind` 這一關判定為未知形狀 —— 依規約是「跳過並記錄」，於是**所有 bar 被靜默丟棄**。
- 前一代 v1 的 tick 用 `"kind":"tick"`，形狀相符，於是一路走到欄位讀取才發現沒有
  `el_volume`。若 binding 用「取不到就給 0」的寫法，磁碟上就會多出一批**全 0 的量值** ——
  一個完全合理、什麼都不會失敗的數字。

改一個欄位名就讓這整類問題在結構上不存在：**舊 payload 沒有 `proto` 這個 key**，所以
「版本相符」與「其實是舊資料」永遠不會同時成立。

### binding 的義務

- **`proto` 缺席或不等於 `2` → 拒收該 frame 並記錄。** 錯誤訊息必須點名可能的原因是
  「DLL 早於本協定」，並指出修法是同時更新 `TS2Python.dll` 與 `.ELD`。
- **五個 `el_*` 欄位一律以「必填」讀取。** 缺欄位必須拋錯，**不得**套用預設值 ——
  靜默寫 0 的成本遠高於解析失敗。
- **讀不了的 frame → 跳過並記錄，不得拋錯。** proto 2 沒有 `kind` 可以未知了；一個
  frame 現在只會因為缺必填欄位或型別不對而讀不了。壞一個 frame 不代表整條串流壞了 ——
  但它必須被算進 `frames_refused`，不能只是消失。

## 兩個時間戳，不是三個

前一代的 wire 有第三個時間欄位 `ts_utc`：DLL 用 `std::chrono::zoned_time` 把 `ts_str`
解析成 UTC epoch 的結果。本協定移除它。**這是取捨，不是移除冗餘**，兩個代價要說清楚：

1. **失去一個偵測面。** binding 是用自己的時區資料庫解析 `ts_str` 的。`ts_utc` 與 `ts`
   的差距（前一代規定 > 5 秒就記錄警告）是唯一能發現「DLL 主機與 binding 主機的 tz
   database 不一致」的訊號。DST 轉換日的折疊時刻是這種不一致唯一會顯現的地方，一年兩天。
2. **`ts_str` 的可解析性不再於 publisher 端驗證。** 前一代的 DLL 解析失敗會送 `ts_utc: 0.0`，
   等於順手做了一次格式檢查。現在無效的時間字串會原樣送出，由 binding 發現 ——
   錯誤的發現點往後移了一層。**因為 binding 是唯一還能發現它的一層，
   [`semantics.md`](semantics.md) §1.1 規定它必須拒收該 frame，不得退回 `ts`。**

換到的是：wire 上只有一個權威時間來源（`ts_str`）與一個量測用時間（`ts`），沒有第三個
「存在但不得作權威用」的欄位需要每個 binding 各自記得別用。時間權威的完整規則見
[`semantics.md`](semantics.md) §1。

## 一個 publish 匯出,一個 init 匯出,三個墓碑

| 匯出 | 簽章 | 用途 |
| --- | --- | --- |
| `EL_InitChart` | `(const char* endpoint, const char* symbol, int category, int bar_type, int bar_interval)` | **唯一的 init**;綁定 socket + 宣告這張圖 |
| `EL_Publish` | 16 個參數 | **唯一的 publish** |
| `EL_Init` | `(const char* endpoint)` | **墓碑**,一律回 `-6` |
| `EL_PublishTick` | 10 個參數 | **墓碑**,一律回 `-6` |
| `EL_PublishBar` | 13 個參數 | **墓碑**,一律回 `-6` |
| `EL_Shutdown` | `()` | |
| `EL_DllVersion` | `()` | 回 `4` |

`EL_Init2` 與 `EL_Init3` 已**刪除**。

### 簽章一改,名字就改 —— 這是唯一有效的閘門

`DefineDLLFunc` 只按名字 `GetProcAddress`,而 `__stdcall` 由被呼叫端清堆疊,被呼叫端
看不見呼叫端推了幾個參數。所以**一個名字如果在簽章改變後還指向新簽章,舊 `.ELD` 會
解析得到、呼叫得下去、然後直接損毀堆疊** —— TradeStation 崩潰或行為異常,沒有任何
回傳碼,程式碼補不起來。

擋住這件事的一直是 init:每一個 publish 呼叫都在「init 成功」的閘門後面
（`EL/TS2Python_Exporter.el`）,所以只要 init 的名字跟著簽章換,舊 `.ELD` 就停在自己的
init,永遠碰不到改過簽章的 publish。

**ABI 3 曾經放棄這個原則**,把 `EL_Init` 這個名字收回來重用成五參數的 init。ABI 4 把
init 改名為 **`EL_InitChart`**,並把 `EL_Init` 放回**單參數**的墓碑,閘門就補回來了。
舊名字釘在舊 arity 上,舊 `.ELD` 的呼叫才會平衡。

驗證方式是看匯出的裝飾名 —— `@` 後面的數字就是參數佔用的位元組數:

```
EL_Init      = _EL_Init@4        <- 單參數墓碑,舊 .ELD 打進來會平衡
EL_InitChart = _EL_InitChart@20  <- 五參數,只有新 .ELD 找得到
```

```bash
git show 7faeabf:cpp/src/TS2Python.def   # ABI-1:有 EL_Init3,沒有 EL_Publish
git show 2a8033f:cpp/src/TS2Python.def   # ABI-2:EL_Init3 + EL_Publish
git show 510462c:cpp/src/TS2Python.def   # ABI-3:五參數 EL_InitChart（閘門缺口）
git show HEAD:cpp/src/TS2Python.def      # ABI-4:EL_InitChart + EL_Init 墓碑
```

**DLL 與 `.ELD` 仍然是同一個單位,換 DLL 就必須重新 Verify indicator**
（`cpp/install-to-tradestation.bat` 安裝完會提醒）—— 差別在於現在裝錯只會得到一行可讀的
訊息,而不是一次崩潰。

### 新舊部署不相容時會發生什麼

| 情境 | 攔截點 | 使用者看到什麼 |
| --- | --- | --- |
| 新 `.ELD` + 舊（ABI-1/2/3）DLL | 舊 DLL 沒有 `EL_InitChart` 匯出 | `DefineDLLFunc` 解析 `EL_InitChart` 失敗，TradeStation 在 verify 階段就報錯 |
| 新 `.ELD` + 版本不符的新 DLL | indicator 的 `EL_DllVersion()` latch | Print Log 出現版本不符訊息，indicator 停止發布。`EL_DllVersion` 是 0 參數，簽章永不變，呼叫它絕對安全 |
| 舊 `.ELD`（呼叫 `EL_PublishTick`/`Bar`）+ 新 DLL | 墓碑回 `-6` | Print Log 出現 `rc=-6`，不發布 |
| 舊 `.ELD`（呼叫單參數 `EL_Init`）+ 新 DLL | 單參數 `EL_Init` 墓碑回 `-6` | Print Log 出現 `rc=-6`，不發布。**堆疊平衡，不再崩潰** |

四個方向現在**都是可讀的失敗**。最後一列在 ABI 3 時是一個無法攔截的崩潰，ABI 4 的改名
把它關上了。

## 邊界與限制

| 限制 | 值 |
| --- | --- |
| tick payload 緩衝區 | 640 bytes；超出回傳 `-4` 且不送出（序號已消耗） |
| bar payload 緩衝區 | 768 bytes；同上 |
| hello payload 緩衝區 | 512 bytes；超出 `EL_InitChart` 回 `-4` |
| `ts_str` 未跳脫 | 直接插入 JSON 字串；binding **應對解析失敗有容錯** |
| hello 的 `symbol` 未跳脫 | 同上。TradeStation 的 symbol 不含 `"` 或 `\`，但 binding 不得假設 —— 壞掉的 hello 必須被拒收並計入 `frames_refused`，不得中斷串流 |

## 傳輸保證

ZeroMQ PUB/SUB 為 fire-and-forget，兩端都會靜默丟棄（`SNDHWM = 100000`、
`RCVHWM` 預設 1000）。`seq` 是 subscriber 唯一能察覺的途徑，且**序號在送出失敗時仍然
消耗** —— 那筆資料確實遺失，顯示為 gap 才誠實。

回傳碼見 [`error_codes.md`](error_codes.md)，schema 管不到的語意規則見
[`semantics.md`](semantics.md)。
