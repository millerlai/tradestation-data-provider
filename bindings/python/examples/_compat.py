"""Start an asyncio loop that pyzmq can actually use on Windows.

Every entry point that touches `zmq.asyncio` needs this, so the three
examples that do share it rather than repeating it. (03_read_history.py runs
entirely offline and never opens a socket, so it does not import this.)

**The problem.** pyzmq's asyncio integration registers its notification FD
through `loop.add_reader()`. Windows' default ProactorEventLoop does not
implement that. The failure is silent and expensive to diagnose: the SUB
socket connects, `recv_multipart()` is awaited, and nothing ever wakes it —
no exception, no log line, just a program that looks like a quiet market.

**Why not just call set_event_loop_policy.** That is what
`tradestation_data.runtime.main` does, and on Python <= 3.13 it is correct.
But 3.14 deprecates both `asyncio.set_event_loop_policy()` and
`WindowsSelectorEventLoopPolicy`, with removal slated for 3.16, and this
package supports 3.11 through 3.14. `asyncio.run(..., loop_factory=...)`
arrived in 3.12 and is the replacement, so the version check below picks
whichever spelling the running interpreter accepts.

Copy this function into your own entry point, or call
`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`
before `asyncio.run()` if you only target 3.11-3.13.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    """`asyncio.run(coro)`, forced onto a selector loop on Windows."""
    if sys.platform != "win32":
        return asyncio.run(coro)

    if sys.version_info >= (3, 12):
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)
