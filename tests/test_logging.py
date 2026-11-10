"""Tests for shared per-path file handlers in mr_data.logging."""

import json
import logging.handlers
from pathlib import Path

import pytest

from mr_data import logging as mr_logging
from mr_data.config import settings
from mr_data.logging import _FILE_HANDLERS, get_logger, reset_loggers


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Provide a temporary log directory and reset logger state around the test."""
    path = tmp_path / "logs"
    path.mkdir()
    monkeypatch.setattr(settings, "log_dir", str(path))
    reset_loggers()
    yield path
    reset_loggers()


def _file_handlers(logger: logging.Logger) -> list[logging.handlers.RotatingFileHandler]:
    return [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def test_loggers_share_file_handler_by_path(log_dir):
    log_a = get_logger("mr_data.test.a")
    log_b = get_logger("mr_data.test.b")

    file_a = _file_handlers(log_a)
    file_b = _file_handlers(log_b)
    assert len(file_a) == 1
    assert len(file_b) == 1
    assert file_a[0] is file_b[0]

    # Both loggers write through the single shared handler.
    log_a.info("hello from a", extra={"event": "test"})
    log_b.info("hello from b", extra={"event": "test"})
    log_a.handlers[0].flush()

    log_path = Path(settings.log_dir) / "mr-data.log"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [(l["logger"], l["message"]) for l in lines] == [
        ("mr_data.test.a", "hello from a"),
        ("mr_data.test.b", "hello from b"),
    ]


def test_reset_loggers_clears_and_rebuilds_file_handlers(log_dir):
    logger = get_logger("mr_data.test.reset")
    old_handler = _file_handlers(logger)[0]
    assert list(_FILE_HANDLERS.values()) == [old_handler]

    reset_loggers()
    assert _FILE_HANDLERS == {}

    rebuilt = get_logger("mr_data.test.reset")
    new_handler = _file_handlers(rebuilt)[0]
    assert new_handler is not old_handler
    assert list(_FILE_HANDLERS.values()) == [new_handler]

    # The rebuilt handler still writes to the same file.
    rebuilt.info("after reset", extra={"event": "test"})
    rebuilt.handlers[0].flush()
    log_path = Path(settings.log_dir) / "mr-data.log"
    assert "after reset" in log_path.read_text(encoding="utf-8")


def test_same_logger_reconfiguration_is_short_circuited(log_dir):
    logger = get_logger("mr_data.test.idempotent")
    handler_count = len(logger.handlers)
    assert get_logger("mr_data.test.idempotent") is logger
    assert len(logger.handlers) == handler_count


def test_module_cache_matches_internal_state(log_dir):
    get_logger("mr_data.test.x")
    get_logger("mr_data.test.y")
    assert mr_logging._CONFIGURED == {"mr_data.test.x", "mr_data.test.y"}
    assert len(_FILE_HANDLERS) == 1
