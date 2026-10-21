"""Structured JSON logging with bound context (run_id/node/subq_id/claim_id).

Plain stdlib logging for the Core path — see BLUEPRINT.md §3 ("upgrade to
structlog in Hardening if time remains"). Never log secrets or full source
texts (BLUEPRINT.md §12) — that's a discipline rule for callers, not
something this module can enforce.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context.get())
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def bind(**kwargs: object) -> None:
    ctx = dict(_context.get())
    ctx.update(kwargs)
    _context.set(ctx)


def unbind_all() -> None:
    _context.set({})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_extra(logger: logging.Logger, level: int, message: str, **fields: object) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
