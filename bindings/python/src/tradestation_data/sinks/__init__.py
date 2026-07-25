"""Pluggable sink framework for emitted Ticks and Bars.

A :class:`Sink` consumes :class:`Tick` and :class:`Bar` events from the
ingestion runtime and is responsible for the output side (writing
Parquet, CSV, in-memory buffering, dispatching to user callbacks, ...).
Sinks are declared in ``config/sinks.yaml`` and instantiated by
:func:`tradestation_data.sinks.registry.build_pipeline_from_config`.

See ``docs/sinks.md`` (or README) for the YAML schema and how to write
a custom sink.
"""

from tradestation_data.sinks.base import Sink
from tradestation_data.sinks.pipeline import SinkPipeline
from tradestation_data.sinks.registry import (
    SinkConfig,
    SinksConfigError,
    build_pipeline_from_config,
    instantiate_sink,
    load_sinks_config,
)

__all__ = [
    "Sink",
    "SinkConfig",
    "SinkPipeline",
    "SinksConfigError",
    "build_pipeline_from_config",
    "instantiate_sink",
    "load_sinks_config",
]
