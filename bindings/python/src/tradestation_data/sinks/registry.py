from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tradestation_data.sinks.base import Sink
from tradestation_data.sinks.pipeline import SinkPipeline

log = logging.getLogger(__name__)


class SinksConfigError(ValueError):
    """Raised when ``sinks.yaml`` is malformed or a sink cannot be loaded."""


@dataclass(frozen=True, slots=True)
class SinkConfig:
    """One entry from ``sinks.yaml``.

    ``target`` is a ``module:attr`` string pointing to a callable
    (class or factory) that takes ``**params`` plus ``name=<name>`` and
    returns a :class:`Sink`.
    """

    name: str
    target: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SinksConfig:
    sinks: tuple[SinkConfig, ...]


def load_sinks_config(path: Path | str) -> SinksConfig:
    """Parse ``sinks.yaml`` into a :class:`SinksConfig`.

    Empty / missing ``sinks:`` is allowed and yields an empty config —
    that's the ``--no-storage`` ephemeral-mode equivalent.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SinksConfigError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")

    entries = raw.get("sinks") or []
    if not isinstance(entries, list):
        raise SinksConfigError(f"{path}: 'sinks' must be a list, got {type(entries).__name__}")

    parsed: list[SinkConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SinksConfigError(f"{path}: sinks[{i}] must be a mapping")
        name = entry.get("name")
        target = entry.get("class")
        if not isinstance(name, str) or not name:
            raise SinksConfigError(f"{path}: sinks[{i}] missing required string 'name'")
        if not isinstance(target, str) or ":" not in target:
            raise SinksConfigError(
                f"{path}: sinks[{i}] ({name!r}) 'class' must be 'module:attr', got {target!r}"
            )
        if name in seen:
            raise SinksConfigError(f"{path}: duplicate sink name {name!r}")
        seen.add(name)
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise SinksConfigError(
                f"{path}: sinks[{i}] ({name!r}) 'params' must be a mapping, got "
                f"{type(params).__name__}"
            )
        parsed.append(SinkConfig(name=name, target=target, params=dict(params)))

    return SinksConfig(sinks=tuple(parsed))


def instantiate_sink(cfg: SinkConfig) -> Sink:
    """Import ``cfg.target`` and call it with ``**cfg.params`` plus ``name=cfg.name``.

    The target callable (typically a sink class) must accept ``name``
    as a keyword argument so the constructed instance carries the
    name declared in ``sinks.yaml``.
    """
    module_path, _, attr = cfg.target.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise SinksConfigError(
            f"sink {cfg.name!r}: cannot import module {module_path!r}: {exc}"
        ) from exc
    try:
        factory = getattr(module, attr)
    except AttributeError as exc:
        raise SinksConfigError(
            f"sink {cfg.name!r}: module {module_path!r} has no attribute {attr!r}"
        ) from exc
    if not callable(factory):
        raise SinksConfigError(f"sink {cfg.name!r}: target {cfg.target!r} is not callable")
    try:
        instance = factory(name=cfg.name, **cfg.params)
    except TypeError as exc:
        raise SinksConfigError(
            f"sink {cfg.name!r}: factory {cfg.target!r} rejected params {cfg.params!r}: {exc}"
        ) from exc
    if not isinstance(instance, Sink):
        # Sink is a runtime_checkable Protocol — this catches typos like
        # a class that forgot to implement on_bar / close.
        raise SinksConfigError(
            f"sink {cfg.name!r}: {cfg.target!r} returned {type(instance).__name__}, "
            "which does not satisfy the Sink protocol"
        )
    return instance


def build_pipeline_from_config(
    config: SinksConfig | Path | str,
) -> SinkPipeline:
    """Load (if needed) and instantiate every sink declared in ``config``.

    Sinks are instantiated in declaration order; that is also the
    order the pipeline broadcasts events in.
    """
    cfg = config if isinstance(config, SinksConfig) else load_sinks_config(config)
    sinks: list[Sink] = []
    for entry in cfg.sinks:
        sink = instantiate_sink(entry)
        sinks.append(sink)
        log.debug("sink_instantiated", extra={"sink": entry.name, "target": entry.target})
    return SinkPipeline(sinks)
