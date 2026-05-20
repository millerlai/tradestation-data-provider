from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

# `_sink_fixtures` is registered in sys.modules by tests/conftest.py
# at collection time. Importing it here gives the test direct access
# to the same classes the registry will resolve via importlib.
import _sink_fixtures  # type: ignore[import-not-found]
from tradestation_data.domain.bar import Bar
from tradestation_data.sinks.registry import (
    SinkConfig,
    SinksConfigError,
    build_pipeline_from_config,
    instantiate_sink,
    load_sinks_config,
)


def _bar(symbol: str = "SPY") -> Bar:
    return Bar(
        symbol=symbol,
        bucket_start=datetime(2026, 4, 20, 13, 30, tzinfo=UTC),
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10,
        vwap=1.4,
        tick_count=3,
        source="t",
    )


def test_load_sinks_config_happy_path(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sinks.yaml"
    cfg_path.write_text(
        """
sinks:
  - name: a
    class: _sink_fixtures:FakeSink
    params:
      label: alpha
  - name: b
    class: _sink_fixtures:fake_factory
""",
        encoding="utf-8",
    )
    cfg = load_sinks_config(cfg_path)
    assert len(cfg.sinks) == 2
    assert cfg.sinks[0] == SinkConfig(
        name="a", target="_sink_fixtures:FakeSink", params={"label": "alpha"}
    )
    assert cfg.sinks[1].params == {}


def test_load_sinks_config_allows_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("sinks: []\n", encoding="utf-8")
    assert load_sinks_config(p).sinks == ()


def test_load_sinks_config_rejects_top_level_list(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- foo\n- bar\n", encoding="utf-8")
    with pytest.raises(SinksConfigError, match="top-level must be a mapping"):
        load_sinks_config(p)


@pytest.mark.parametrize(
    "yaml_body, match",
    [
        ("sinks:\n  - {}\n", "missing required string 'name'"),
        ("sinks:\n  - name: x\n", "'class' must be 'module:attr'"),
        ("sinks:\n  - name: x\n    class: bad-no-colon\n", "'class' must be 'module:attr'"),
        (
            "sinks:\n  - name: x\n    class: m:c\n    params: notamap\n",
            "'params' must be a mapping",
        ),
        ("sinks: 'notalist'\n", "'sinks' must be a list"),
    ],
)
def test_load_sinks_config_validation_errors(tmp_path: Path, yaml_body: str, match: str) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(yaml_body, encoding="utf-8")
    with pytest.raises(SinksConfigError, match=match):
        load_sinks_config(p)


def test_load_sinks_config_rejects_duplicate_names(tmp_path: Path) -> None:
    p = tmp_path / "dup.yaml"
    p.write_text(
        """
sinks:
  - name: dup
    class: _sink_fixtures:FakeSink
  - name: dup
    class: _sink_fixtures:FakeSink
""",
        encoding="utf-8",
    )
    with pytest.raises(SinksConfigError, match="duplicate sink name 'dup'"):
        load_sinks_config(p)


def test_instantiate_sink_passes_name_and_params() -> None:
    cfg = SinkConfig(
        name="my_sink",
        target="_sink_fixtures:FakeSink",
        params={"label": "beta"},
    )
    sink = instantiate_sink(cfg)
    assert isinstance(sink, _sink_fixtures.FakeSink)
    assert sink.name == "my_sink"
    assert sink.label == "beta"


def test_instantiate_sink_supports_factory_callable() -> None:
    cfg = SinkConfig(
        name="factory_sink",
        target="_sink_fixtures:fake_factory",
        params={},
    )
    sink = instantiate_sink(cfg)
    assert sink.name == "factory_sink"


def test_instantiate_sink_errors_on_missing_module() -> None:
    cfg = SinkConfig(name="x", target="does.not.exist:Anything", params={})
    with pytest.raises(SinksConfigError, match="cannot import module"):
        instantiate_sink(cfg)


def test_instantiate_sink_errors_on_missing_attr() -> None:
    cfg = SinkConfig(name="x", target="_sink_fixtures:NoSuchAttribute", params={})
    with pytest.raises(SinksConfigError, match="has no attribute"):
        instantiate_sink(cfg)


def test_instantiate_sink_errors_when_target_not_callable() -> None:
    cfg = SinkConfig(name="x", target="_sink_fixtures:NOT_CALLABLE", params={})
    with pytest.raises(SinksConfigError, match="is not callable"):
        instantiate_sink(cfg)


def test_instantiate_sink_errors_on_bad_params() -> None:
    cfg = SinkConfig(
        name="x",
        target="_sink_fixtures:FakeSink",
        params={"label": "ok", "extra_unknown_arg": 1},
    )
    with pytest.raises(SinksConfigError, match="rejected params"):
        instantiate_sink(cfg)


def test_instantiate_sink_errors_when_result_not_sink() -> None:
    cfg = SinkConfig(name="x", target="_sink_fixtures:make_non_sink", params={})
    with pytest.raises(SinksConfigError, match="does not satisfy the Sink protocol"):
        instantiate_sink(cfg)


def test_build_pipeline_from_config_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "sinks.yaml"
    p.write_text(
        """
sinks:
  - name: a
    class: _sink_fixtures:FakeSink
  - name: b
    class: _sink_fixtures:FakeSink
    params: { label: beta }
""",
        encoding="utf-8",
    )
    pipe = build_pipeline_from_config(p)
    assert len(pipe) == 2
    names = [getattr(s, "name", "?") for s in pipe]
    assert names == ["a", "b"]

    pipe.on_bar(_bar())
    for sink in pipe:
        assert len(sink.bars) == 1  # type: ignore[attr-defined]


def test_build_pipeline_from_config_accepts_loaded_config(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text(
        "sinks:\n  - name: a\n    class: _sink_fixtures:FakeSink\n",
        encoding="utf-8",
    )
    cfg = load_sinks_config(p)
    pipe = build_pipeline_from_config(cfg)
    assert len(pipe) == 1
