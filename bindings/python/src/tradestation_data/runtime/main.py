"""
CLI entry point for the data-provider ingestion runtime.

Wires TradeStationELProvider (ZMQ SUB) → MarketSnapshot + BarAggregator
→ SinkPipeline (declared in ``config/sinks.yaml``), installs a SIGINT
handler for graceful shutdown, and runs IngestionRuntime until
interrupted.

This is the data-collection-only fork of the runtime — strategy /
broker / risk wiring has been removed. The ingestion loop dispatches
ticks and 1-min bars to a user-configurable sink pipeline; the default
``config/sinks.yaml`` writes both as Hive-partitioned Parquet under
``data/`` (matching the historical layout), but users can swap in any
sink declared in ``sinks.yaml`` — see ``sinks/`` for the protocol.

Invoke with ``uv run python -m tradestation_data.runtime.main ...``
(or the ``tradestation-data-ingest`` console script declared in
pyproject.toml).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import ClassVar

from tradestation_data.aggregation.bar_aggregator import BarAggregator
from tradestation_data.aggregation.snapshot import MarketSnapshot
from tradestation_data.domain.bar import Bar
from tradestation_data.runtime.config import load_symbols
from tradestation_data.runtime.ingestion import IngestionRuntime
from tradestation_data.sinks import (
    Sink,
    SinkPipeline,
    SinksConfigError,
    build_pipeline_from_config,
)
from tradestation_data.sinks.base import BaseSink
from tradestation_data.sinks.parquet import ParquetBarSink, ParquetTickSink
from tradestation_data.storage.bar_writer import BAR_SCHEMA, _bars_to_table
from tradestation_data.wire.el_subscriber import TradeStationELProvider


def _configure_logging(level: str, *, json_output: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s %(extra_dump)s")
        )
        handler.addFilter(_ExtraDumpFilter())
    root.addHandler(handler)


class _JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter — timestamp, level, logger, msg, + extras."""

    _STD_KEYS: ClassVar[set[str]] = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in self._STD_KEYS and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _PrintingBarSink(BaseSink):
    """Print the first ``limit`` bars to stdout in the on-disk parquet schema.

    Useful for verifying what bars.parquet would contain without opening
    the file afterwards. Activated via ``--print-bars N``; sits in the
    pipeline alongside (not in place of) the configured persistence sinks.
    """

    def __init__(self, *, name: str = "_print_bars", limit: int) -> None:
        self.name = name
        self._limit = limit
        self._printed = 0

    def on_bar(self, bar: Bar) -> None:
        if self._printed >= self._limit:
            return
        table = _bars_to_table([bar])
        if self._printed == 0:
            print("--- bars.parquet schema ---", flush=True)
            print(BAR_SCHEMA, flush=True)
            print(f"--- first {self._limit} bar(s) emitted ---", flush=True)
        for row in table.to_pylist():
            print(json.dumps(row, default=str), flush=True)
        self._printed += 1
        if self._printed == self._limit:
            print(f"--- end of first {self._limit} bar(s) ---", flush=True)


class _ExtraDumpFilter(logging.Filter):
    """Serialise `extra` kwargs into record.extra_dump for the plain formatter.

    Attached to the StreamHandler (not to a logger) so it fires for every
    record that reaches the handler — including records from child loggers
    that only propagate up to root's handlers, not to root's own filters.
    """

    _STD: ClassVar[set[str]] = _JsonFormatter._STD_KEYS | {"extra_dump"}

    def filter(self, record: logging.LogRecord) -> bool:
        extras = {
            k: v for k, v in record.__dict__.items() if k not in self._STD and not k.startswith("_")
        }
        record.extra_dump = json.dumps(extras, default=str) if extras else ""
        return True


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tradestation-data-ingest",
        description="Run the TradeStation EL → Python ingestion runtime.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=Path("config/symbols.yaml"),
        help="Path to symbols.yaml (default: config/symbols.yaml).",
    )
    p.add_argument(
        "--sinks-config",
        type=Path,
        default=Path("config/sinks.yaml"),
        help="Path to sinks.yaml declaring the output pipeline. "
        "If the file is missing, falls back to built-in default Parquet sinks "
        "rooted under --data-root (default: config/sinks.yaml).",
    )
    p.add_argument(
        "--endpoint",
        default="tcp://127.0.0.1:5555",
        help="ZeroMQ SUB endpoint to connect to (default: tcp://127.0.0.1:5555).",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="Root dir for the built-in default Parquet sinks (used only when "
        "--sinks-config is missing or --no-storage is set together with "
        "--print-bars). Ticks go under {root}/ticks/, bars under {root}/bars/.",
    )
    p.add_argument(
        "--no-storage",
        action="store_true",
        help="Disable all sinks (ignore --sinks-config). Use for smoke testing.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument(
        "--log-json",
        action="store_true",
        help="Emit logs as JSON objects (one per line). Useful for log shippers.",
    )
    p.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=60.0,
        help="Heartbeat log interval (default: 60s).",
    )
    p.add_argument(
        "--print-bars",
        type=int,
        default=0,
        metavar="N",
        help="Print the first N closed bars to stdout in the bars.parquet schema. "
        "0 = disabled (default). Independent of the configured persistence sinks.",
    )
    return p.parse_args(argv)


def _build_default_pipeline(data_root: Path) -> SinkPipeline:
    """Construct the historical-default pipeline: Parquet bars + Parquet ticks.

    Mirrors what the pre-sink runtime did automatically when no
    ``--no-storage`` flag was passed.
    """
    return SinkPipeline(
        [
            ParquetBarSink(name="bars_parquet", root=data_root / "bars"),
            ParquetTickSink(name="ticks_parquet", root=data_root / "ticks"),
        ]
    )


def _build_pipeline(args: argparse.Namespace, log: logging.Logger) -> SinkPipeline:
    if args.no_storage:
        log.warning("storage_disabled_ephemeral_mode")
        return SinkPipeline()

    if args.sinks_config.exists():
        try:
            pipeline = build_pipeline_from_config(args.sinks_config)
        except SinksConfigError:
            log.exception("sinks_config_invalid", extra={"path": str(args.sinks_config)})
            raise
        log.info(
            "sinks_loaded",
            extra={
                "path": str(args.sinks_config),
                "count": len(pipeline),
                "names": [getattr(s, "name", "?") for s in pipeline],
            },
        )
        return pipeline

    log.info(
        "sinks_config_missing_using_default",
        extra={"path": str(args.sinks_config), "data_root": str(args.data_root)},
    )
    return _build_default_pipeline(args.data_root)


async def _amain(args: argparse.Namespace) -> int:
    log = logging.getLogger("tradestation_data.runtime.main")

    if not args.config.exists():
        log.error("config_not_found", extra={"path": str(args.config)})
        return 2

    cfg = load_symbols(args.config)
    symbols = cfg.ids()
    log.info("loaded_symbols", extra={"count": len(symbols), "path": str(args.config)})

    provider = TradeStationELProvider(endpoint=args.endpoint)
    snapshot = MarketSnapshot(symbol_policies=cfg.session_policies())
    aggregator = BarAggregator()

    try:
        pipeline = _build_pipeline(args, log)
    except SinksConfigError:
        return 3

    if args.print_bars > 0:
        # _PrintingBarSink prints to stdout, so it can co-exist with any
        # persistence sink — declare it first so it sees the bar before
        # a slow parquet write.
        printer: Sink = _PrintingBarSink(limit=args.print_bars)
        pipeline = SinkPipeline([printer, *pipeline])

    runtime = IngestionRuntime(
        provider=provider,
        symbols=symbols,
        snapshot=snapshot,
        aggregator=aggregator,
        sinks=pipeline,
        heartbeat_interval=args.heartbeat_seconds,
    )

    loop = asyncio.get_running_loop()

    def _handle_sigint() -> None:
        log.info("signal_received", extra={"signal": "SIGINT"})
        runtime.stop()

    # SIGINT handling is asyncio-native on Unix; on Windows we fall back
    # to the default KeyboardInterrupt path (caught below).
    try:
        loop.add_signal_handler(signal.SIGINT, _handle_sigint)
        loop.add_signal_handler(signal.SIGTERM, _handle_sigint)
    except NotImplementedError:
        pass  # Windows — asyncio.run catches KeyboardInterrupt for us.

    try:
        await runtime.run()
    except KeyboardInterrupt:
        log.info("keyboard_interrupt")
        runtime.stop()
    finally:
        # Belt-and-suspenders: ensure every sink's close() runs even if
        # IngestionRuntime.run()'s shutdown path was interrupted (e.g. a
        # second Ctrl+C while zmq ctx.term() was blocking in the inner
        # finally). SinkPipeline.close() is idempotent — calling it
        # again after a clean shutdown is a no-op.
        try:
            pipeline.close()
        except Exception:
            log.exception("pipeline_close_failed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.log_level, json_output=args.log_json)
    # On Windows Python 3.8+ the default is ProactorEventLoop, which does
    # not support loop.add_reader() — and pyzmq's asyncio Socket registers
    # its notification FD via add_reader. The result: SUB sockets connect
    # cleanly but await recv_multipart() never wakes up even when data
    # arrives. Force the selector loop so zmq.asyncio actually works.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
