from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import zmq
import zmq.asyncio

# pyzmq's asyncio integration needs add_reader/add_writer, which the
# Windows ProactorEventLoop does not support natively. Switch to the
# selector-based loop so socket cleanup is reliable under pytest.
#
# This is the one place still using the deprecated policy API — src/ and
# examples/ both moved to asyncio.run(loop_factory=...). It cannot follow:
# pytest-asyncio owns loop creation here, and 1.3.0 exposes only the
# policy-based `event_loop_policy` fixture, with no loop_factory hook (it
# calls asyncio.set_event_loop_policy internally and silences the warning
# with warnings.simplefilter). So this has to wait for pytest-asyncio, and
# when 3.16 removes the API pytest-asyncio breaks with or without this line.
# Recheck on the next pytest-asyncio major.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# `tests/` is not on pythonpath (only `src/` is — see pyproject.toml).
# The sink registry tests need a stable, importable module name to
# point ``module:attr`` target strings at. Load `_sink_fixtures.py`
# directly and register it in sys.modules under the bare name once,
# at collection time, so target strings like
# ``_sink_fixtures:FakeSink`` resolve via importlib.
def _register_sink_fixtures() -> None:
    if "_sink_fixtures" in sys.modules:
        return
    fixtures_path = Path(__file__).parent / "_sink_fixtures.py"
    spec = importlib.util.spec_from_file_location("_sink_fixtures", fixtures_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["_sink_fixtures"] = module
    spec.loader.exec_module(module)


_register_sink_fixtures()


@pytest_asyncio.fixture
async def zmq_inproc_bus() -> AsyncIterator[tuple[zmq.asyncio.Context, zmq.asyncio.Socket, str]]:
    """
    Shared ZMQ context + bound PUB socket for in-process tests.
    Endpoint uses inproc:// so there's no network and no handshake delay.
    """
    ctx = zmq.asyncio.Context()
    pub = ctx.socket(zmq.PUB)
    endpoint = f"inproc://test-{uuid.uuid4().hex}"
    pub.bind(endpoint)
    try:
        yield ctx, pub, endpoint
    finally:
        pub.close(linger=0)
        # destroy() forcibly closes any stray sockets. On Windows +
        # Python 3.13 + pyzmq 27, plain `ctx.term()` occasionally
        # raises an access violation when an async SUB socket was
        # attached to this context.
        ctx.destroy(linger=0)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Ensure async tests pick up the asyncio mode automatically
    for item in items:
        if "asyncio" in item.keywords:
            continue
