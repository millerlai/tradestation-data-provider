"""Structured-logging plumbing shared by every entry point in this package.

Every module here logs a FIXED, greppable event name and puts the variable
data in ``extra``::

    log.info("hub_heartbeat", extra={"frames_forwarded": 12043, ...})

stdlib ``logging`` does not print ``extra``. Its Formatter only emits what the
format string names explicitly, and ``extra`` kwargs otherwise become
attributes nobody reads — so the line above renders as a bare
``INFO hub_heartbeat`` with every number gone. :class:`ExtraDumpFilter` is what
puts them back.

**Why this is its own module.** ``runtime/main.py`` and ``hub.py`` both need
it, and ``hub.py`` cannot import from ``runtime.main``: that module pulls in
the whole binding (polars, pyarrow, the sink registry) and the hub is a 40-line
forwarder that must stay loadable on its own. The copy that used to live in
``hub.py`` was the alternative, and it was one bug-fix away from the two
drifting — in the module whose log output is the operator's only window into
which chart is not coming through.

This module imports **stdlib only**, on purpose. `hub.py` depending on it does
not weaken the property that matters: the hub's behaviour is specified in
``contract/wire.md`` and a reimplementation in another language never reads any
of this.
"""

from __future__ import annotations

import json
import logging
from typing import ClassVar, Final

#: Attributes every ``LogRecord`` carries. Anything on a record that is NOT in
#: here came from a caller's ``extra`` and is what we want to surface.
#: Taken from `logging.LogRecord.__init__` plus the three the library adds
#: later (``message``, ``asctime``, ``taskName``).
STD_LOG_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
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
)

#: Format string for the plain (non-JSON) handler. ``%(extra_dump)s`` is the
#: attribute :class:`ExtraDumpFilter` sets; a handler using this format string
#: MUST have that filter attached or every record raises a KeyError.
PLAIN_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s %(message)s %(extra_dump)s"


class ExtraDumpFilter(logging.Filter):
    """Serialise ``extra`` kwargs onto ``record.extra_dump``.

    Attach to the **handler**, not to a logger: a filter on a logger does not
    see records from child loggers, which only propagate up to root's handlers.
    Attached to the handler it fires for every record that reaches it.
    """

    _STD: ClassVar[frozenset[str]] = STD_LOG_RECORD_KEYS | {"extra_dump"}

    def filter(self, record: logging.LogRecord) -> bool:
        extras = {
            k: v for k, v in record.__dict__.items() if k not in self._STD and not k.startswith("_")
        }
        record.extra_dump = json.dumps(extras, default=str) if extras else ""
        return True
