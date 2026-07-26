"""Start an asyncio loop that pyzmq can actually use on Windows.

Every entry point that touches `zmq.asyncio` needs this, so the three
examples that do share it rather than repeating it. (03_read_history.py runs
entirely offline and never opens a socket, so it does not import this.)

**The problem.** pyzmq's asyncio integration registers its notification FD
through `loop.add_reader()`. Windows' default ProactorEventLoop does not
implement that. The failure is silent and expensive to diagnose: the SUB
socket connects, `recv_multipart()` is awaited, and nothing ever wakes it —
no exception, no log line, just a program that looks like a quiet market.

**Why loop_factory rather than set_event_loop_policy.** The older spelling,
`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`,
does the same job on 3.13 and earlier. But 3.14 deprecates the entire policy
API and it is slated for removal in 3.16, so that spelling turns into an
`AttributeError` on the platform this package cares about most.
`asyncio.run(..., loop_factory=...)` arrived in 3.12 and is the replacement.

Copy this function into your own entry point. `tradestation_data.runtime.main`
uses the same shape.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, forced onto a selector loop on Windows."""
    # loop_factory=None is exactly asyncio.run's default, so one call covers
    # both platforms.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(coro, loop_factory=loop_factory)
