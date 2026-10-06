"""JSON structured logging (ported from smart-gateway-extractor)."""

from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Zero-dependency JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }
        if record.stack_info:
            entry["stack_info"] = record.stack_info
        return json.dumps(entry, default=str)


def setup_logging() -> None:
    """Configure JSON logging for the entire application."""
    formatter = JsonFormatter()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.getenv("CCUC_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")).upper())


UVICORN_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "ccuc.logging_config.JsonFormatter"}},
    "handlers": {
        "default": {
            "formatter": "json",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        }
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
    },
}
