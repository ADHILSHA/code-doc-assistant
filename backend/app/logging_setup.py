"""Structured (JSON-lines) logging with a per-request trace id (SPEC.md §6
Phase 5 task 4).

Every log line goes through `_JsonFormatter`, which always includes a
`request_id` field. That field is populated from `request_id_var`, a
`contextvars.ContextVar` that `app.middleware.RequestContextMiddleware`
sets once per incoming HTTP request — so any log call made anywhere
during that request's handling (a route handler, jobs.py, the agent loop,
...) picks up the same id automatically, without threading it through
every function signature. Log lines emitted outside a request (a
background indexing job kicked off via `BackgroundTasks`, which runs
*after* the request that started it has already returned — see
api/repos.py) simply get `request_id: null`, which is correct: there's no
single HTTP request that "owns" that work.

Never logs request/response bodies (only method/path/status/duration) —
that's what keeps this compliant with "never log" the GitHub PAT
(SPEC.md §6 Phase 5 task 2): the token only ever appears in a request
body, which this logging layer never touches.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# Attributes every stdlib LogRecord carries — excluded from the "extra
# fields" pass-through in _JsonFormatter so we don't dump logging's own
# internals (like `msg`/`args`/`stack_info`) into every line.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        # Structured fields a call site passed via `logger.info(msg, extra={...})`.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent — safe to call from both `main.py`'s module load and
    test setup without double-registering handlers."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestIdFilter())
    root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
