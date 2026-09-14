import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from mr_data.config import settings


class _JSONLFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        # Merge in any extra fields added via `extra={...}`.
        for key in ("session_id", "details"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


class _ReadableFormatter(logging.Formatter):
    """Human-readable formatter for console output during development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# Keep a module-level cache of configured loggers so we don't rebuild handlers repeatedly.
_CONFIGURED: set[str] = set()


def reset_loggers() -> None:
    """Clear the configured-logger cache. Intended for tests."""
    _CONFIGURED.clear()


def get_logger(name: str) -> logging.Logger:
    """Return a configured JSONL logger."""
    logger = logging.getLogger(name)

    if name in _CONFIGURED:
        return logger

    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    # File handler with rotation.
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / "mr-data.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(_JSONLFormatter())
    logger.addHandler(file_handler)

    # Optional console handler.
    if settings.log_to_stdout:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(_ReadableFormatter())
        logger.addHandler(console_handler)

    _CONFIGURED.add(name)
    return logger
