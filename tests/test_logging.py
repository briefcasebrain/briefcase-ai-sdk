"""Tests for the centralized briefcase logging module."""

import io
import logging

import pytest

import briefcase
from briefcase import _logging as bl


@pytest.fixture(autouse=True)
def _clean_logging():
    """Each test starts and ends with logging disabled (no stream handler)."""
    bl.disable_logging()
    yield
    bl.disable_logging()


def test_nullhandler_attached_to_top_level_logger():
    root = logging.getLogger("briefcase")
    assert any(isinstance(h, logging.NullHandler) for h in root.handlers)


def test_silent_by_default():
    bl.disable_logging()
    root = logging.getLogger("briefcase")
    assert bl._existing_handler() is None  # library adds no stream handler
    assert root.level == logging.NOTSET


def test_enable_logging_is_idempotent_and_emits():
    stream = io.StringIO()
    bl.enable_logging("DEBUG", stream=stream)
    bl.enable_logging("DEBUG", stream=stream)  # second call must not add a 2nd handler
    root = logging.getLogger("briefcase")
    stream_handlers = [
        h for h in root.handlers if getattr(h, "name", None) == bl._HANDLER_NAME
    ]
    assert len(stream_handlers) == 1
    assert root.propagate is False
    briefcase.get_logger("briefcase.test").debug("hello-debug")
    assert "hello-debug" in stream.getvalue()


def test_level_filtering():
    stream = io.StringIO()
    bl.enable_logging("WARNING", stream=stream)
    log = briefcase.get_logger("briefcase.test")
    log.info("info-should-be-hidden")
    log.warning("warning-should-show")
    out = stream.getvalue()
    assert "warning-should-show" in out
    assert "info-should-be-hidden" not in out


def test_set_log_level():
    stream = io.StringIO()
    bl.enable_logging("ERROR", stream=stream)
    bl.set_log_level("DEBUG")
    briefcase.get_logger("briefcase.test").debug("now-visible")
    assert "now-visible" in stream.getvalue()


def test_env_var_auto_enables(monkeypatch):
    bl.disable_logging()
    monkeypatch.setenv("BRIEFCASE_LOG_LEVEL", "ERROR")
    bl._configure_from_env()
    root = logging.getLogger("briefcase")
    assert root.level == logging.ERROR
    assert bl._existing_handler() is not None


def test_invalid_env_var_does_not_crash(monkeypatch):
    bl.disable_logging()
    monkeypatch.setenv("BRIEFCASE_LOG_LEVEL", "NOPE")
    bl._configure_from_env()  # must not raise
    # falls back to WARNING and stays usable
    assert logging.getLogger("briefcase").level == logging.WARNING


def test_get_logger_namespacing():
    assert briefcase.get_logger("briefcase.foo.bar").name == "briefcase.foo.bar"


def test_disable_logging_restores_silence():
    bl.enable_logging("DEBUG", stream=io.StringIO())
    bl.disable_logging()
    assert bl._existing_handler() is None
    assert logging.getLogger("briefcase").propagate is True
