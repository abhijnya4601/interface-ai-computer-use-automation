"""
Structured logging. One line of JSON per event, with the current `run_id` threaded in
automatically via a contextvar so every log line from discovery or replay is correlatable
without passing the id around.

    from common.logging import get_logger, run_context

    log = get_logger("replay")
    with run_context("replay_abc123"):
        log.info("step_done", step_id="s4", tier="role_name", ms=120)

Emits: {"ts": ..., "level": "info", "logger": "replay", "run_id": "replay_abc123",
        "event": "step_done", "step_id": "s4", "tier": "role_name", "ms": 120}

Deliberately tiny (stdlib only, no structlog dependency): a JSON formatter + a contextvar. The
point is that the codebase stops using bare `print()` for anything a machine would want to
parse, not to adopt a logging framework.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import time

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        rid = _run_id.get()
        if rid:
            payload["run_id"] = rid
        # structured fields passed via `extra={"fields": {...}}`
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        return json.dumps(payload, default=str)


_ROOT_NAME = "agentconsole"


class _StructuredLogger:
    def __init__(self, name: str):
        self._log = logging.getLogger(f"{_ROOT_NAME}.{name}")

    def _emit(self, level: str, event: str, **fields):
        self._log.log(_LEVELS[level], event, extra={"fields": fields})

    def debug(self, event: str, **f):
        self._emit("debug", event, **f)

    def info(self, event: str, **f):
        self._emit("info", event, **f)

    def warning(self, event: str, **f):
        self._emit("warning", event, **f)

    def error(self, event: str, **f):
        self._emit("error", event, **f)


_configured = False


def _configure() -> None:
    """Attach a single JSON handler to the `agentconsole` logger — NOT the root logger, so this
    never fights pytest's caplog or an application's own logging config. Records still propagate
    to root for anything else that wants them."""
    global _configured
    if _configured:
        return
    lg = logging.getLogger(_ROOT_NAME)
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, _JsonFormatter)
               for h in lg.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        lg.addHandler(handler)
    lg.setLevel(_LEVELS.get(os.environ.get("LOG_LEVEL", "info").lower(), 20))
    lg.propagate = True
    _configured = True


def get_logger(name: str) -> _StructuredLogger:
    _configure()
    return _StructuredLogger(name)


@contextlib.contextmanager
def run_context(run_id: str):
    """Bind `run_id` for the duration of the block; every log line inside carries it."""
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


def current_run_id() -> str | None:
    return _run_id.get()


def now() -> float:
    return time.time()
