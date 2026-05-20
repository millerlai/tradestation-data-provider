# TradeStation-Data-Provider

獨立的 TradeStation 資料收集 / 清理 / 檢查 / 補全管線

## 用途

- **收集**：訂閱 C++ DLL 的 ZeroMQ PUB socket，將 tick 與 1-min bar 持久化為 Hive-partitioned Parquet。
- **聚合**：把 1-min bar 聚合為 5m / 15m / 30m / 1h。
- **檢查**：驗證 Parquet 資料完整性、稽核 bar cache 是否與 tick 重建一致。
- **補全**：對缺失的 session bar 做 ffill / bfill / interpolate 補值。
- **清理**：去除重複 bar、清除 Tier 3 bar cache。

## 結構

```
tradeStation-data-provider/
├── pyproject.toml
├── config/
│   └── symbols.yaml
├── scripts/                  # 給人使用的 CLI 包裝
│   ├── run_ingestion.py      # 啟動即時 ingestion
│   ├── aggregate_parquet.py  # 1m → Nm 聚合
│   ├── audit_bar_cache.py    # 週稽核
│   ├── clear_bar_cache.py    # 清 Tier 3 cache
│   ├── dedupe_bars.py        # 去重複 bar
│   ├── dump_parquet.py       # 看 parquet 內容
│   ├── imputation_parquet.py # 缺值補全
│   ├── verify_parquet.py     # 驗證完整性
│   ├── simple_sub.py         # ZMQ wire 驗證
│   └── _common.py            # uv run 共用 helper
├── src/tradestation_data/        # 核心 Python package
│   ├── domain/               # Bar / Tick / Order / Position
│   ├── aggregation/          # BarAggregator / MarketSnapshot
│   ├── storage/              # BarWriter / TickWriter / HistoryStore / Resampler
│   ├── providers/            # TradeStationELProvider (ZMQ SUB)
│   ├── sinks/                # Pluggable output sinks (Parquet / InMemory / Callback / 自訂)
│   ├── runtime/              # IngestionRuntime + CLI entry (`tradestation-data-ingest`)
│   └── tools/                # audit_bar_cache / clear_bar_cache modules
└── tests/                    # 對應的 unit / integration tests
```

## 安裝

```powershell
# 從專案根目錄
uv sync                       # 安裝 base deps
uv sync --extra dev           # + pytest / ruff / mypy
```

## 常用指令

```powershell
# 啟動 ingestion（DLL 已執行，PUB 在 tcp://127.0.0.1:5555）
python scripts/run_ingestion.py

# 把 1m 聚合到 5m
python scripts/aggregate_parquet.py --symbol all --timeframe 5m `
  --input data/bars/timeframe=1m --output data/bars

# 驗證資料完整性
python scripts/verify_parquet.py --start-date 2026-03-20 --end-date 2026-04-17

# 缺值補全（先 dry-run）
python scripts/imputation_parquet.py --start-date 2026-03-20 --end-date 2026-04-17 --dry-run

# 跑 tests
uv run pytest
```


本專案的 `runtime/main.py` 與 `runtime/ingestion.py` 是 **純資料管線版本**

## 可插拔 Sink 架構

收到的 tick 和 bar 由 `SinkPipeline` fan-out 給多個 `Sink`，每個 sink 代表一個輸出目的地或格式。預設 sink 在 `config/sinks.yaml` 宣告，啟動時動態載入，**不需修改本專案原始碼**就能加入自訂輸出。

### 內建 sinks

| Sink | 用途 |
|---|---|
| `tradestation_data.sinks.parquet:ParquetBarSink` | 寫 Hive-partitioned 1m bar Parquet（預設啟用）|
| `tradestation_data.sinks.parquet:ParquetTickSink` | 寫 Hive-partitioned tick Parquet（預設啟用）|
| `tradestation_data.sinks.memory:InMemorySink` | 在記憶體緩衝（測試 / notebook）|
| `tradestation_data.sinks.callback:CallbackSink` | 動態註冊 Python callback，依 symbol 派發 |

### `config/sinks.yaml` 範例

```yaml
sinks:
  - name: bars_parquet
    class: tradestation_data.sinks.parquet:ParquetBarSink
    params:
      root: data/bars
      timeframe: 1m
      compression: zstd

  - name: dispatch
    class: tradestation_data.sinks.callback:CallbackSink

  - name: my_csv
    class: my_pkg.sinks:HourlyCsvSink   # 使用者自訂 sink
    params:
      root: out/csv
```

### 寫一個自訂 sink

```python
# my_pkg/sinks.py
from tradestation_data.sinks.base import BaseSink
from tradestation_data.domain.bar import Bar

class HourlyCsvSink(BaseSink):
    def __init__(self, *, name: str, root: str) -> None:
        self.name = name
        self.root = root
        # 開檔 / 設緩衝 ...

    def on_bar(self, bar: Bar) -> None:
        # 寫一列 CSV
        ...

    def close(self) -> None:
        # flush + 關檔
        ...
```

在 `sinks.yaml` 用 `class: my_pkg.sinks:HourlyCsvSink` 引用即可，只要該套件能被 `import` 到。

### 用 `CallbackSink` 在程式內接收資料

```python
from tradestation_data.sinks.callback import get_sink

sink = get_sink("dispatch")   # name 對應 sinks.yaml

def on_spy_bar(bar):
    print(bar.symbol, bar.close)

sink.on("SPY", "bar", on_spy_bar)
sink.on_any("tick", lambda t: ...)   # 所有 symbol
```

Callback 在 ingest loop 中同步呼叫，請保持輕量；需要重活就 spawn task / thread。

## 注意事項

- C++ DLL 仍由主專案編譯與部署，本專案只負責 Python 端。
- 預設的資料根目錄是 `<project-root>/data/`；可用 `--data-root` 覆寫（僅在 `sinks.yaml` 缺檔時的 fallback 路徑生效）。
- 主要透過 `--sinks-config config/sinks.yaml` 控制輸出；smoke test 用 `--no-storage`。
