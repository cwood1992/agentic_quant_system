"""Structured JSON logging for the agentic quant trading system.

Provides JSON-formatted log output to both rotating file and console,
with optional agent_id and cycle context injection.
"""

import json
import logging
import logging.handlers
import os
import traceback
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Required keys: timestamp, level, component, message.
    Optional keys (included only when present): agent_id, cycle, exception.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }

        if getattr(record, "agent_id", None) is not None:
            entry["agent_id"] = record.agent_id

        if getattr(record, "cycle", None) is not None:
            entry["cycle"] = record.cycle

        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )

        return json.dumps(entry)


def setup_logging(log_dir: str = "logs") -> None:
    """Configure the root logger with rotating file and console handlers.

    Args:
        log_dir: Directory for log files. Created if it does not exist.

    File handler: DEBUG level, midnight rotation, 30-day retention.
    Console handler: INFO level.
    Both use JSONFormatter.
    """
    os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers on repeated calls
    if root_logger.handlers:
        return

    formatter = JSONFormatter()

    # Rotating file handler — rotates at midnight, keeps 30 days
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "system.log"),
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def get_logger(
    component: str, agent_id: str = None
) -> logging.Logger | logging.LoggerAdapter:
    """Return a logger (or adapter) for the given component.

    Args:
        component: Logical component name used as the logger name.
        agent_id: If provided, wraps the logger in a LoggerAdapter that
                  injects ``agent_id`` into every record.

    Returns:
        A ``logging.Logger`` when *agent_id* is None, otherwise a
        ``logging.LoggerAdapter``.
    """
    logger = logging.getLogger(component)

    if agent_id is not None:
        return logging.LoggerAdapter(logger, {"agent_id": agent_id})

    return logger
