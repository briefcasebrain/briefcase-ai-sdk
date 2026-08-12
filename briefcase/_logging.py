"""Centralized logging for the briefcase SDK.

Follows Python library best practice: the library attaches only a
:class:`logging.NullHandler` to the top-level ``briefcase`` logger and emits
nothing unless the application opts in. Turn on visible output with
:func:`enable_logging` or by setting the ``BRIEFCASE_LOG_LEVEL`` environment
variable (e.g. ``BRIEFCASE_LOG_LEVEL=DEBUG``).

    import briefcase
    briefcase.enable_logging("DEBUG")     # opt-in; off by default

All briefcase modules obtain their logger via :func:`get_logger`, so they are
children of the ``briefcase`` logger and inherit this configuration.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional, Union

LIBRARY_LOGGER_NAME = "briefcase"

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

# Name tag for the handler added by enable_logging(), so the operation stays
# idempotent and disable_logging() can find and remove it.
_HANDLER_NAME = "_briefcase_stream_handler"

_root = logging.getLogger(LIBRARY_LOGGER_NAME)

# Best practice: never let the library emit "No handlers could be found".
if not any(isinstance(h, logging.NullHandler) for h in _root.handlers):
    _root.addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a logger for a briefcase module.

    Pass ``__name__`` from inside the package (already ``briefcase.*``) so the
    logger inherits the library NullHandler and any handler added by
    :func:`enable_logging`.
    """
    return logging.getLogger(name)


def _coerce_level(level: Union[int, str]) -> int:
    """Resolve a level name or number to an int level."""
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    if name.isdigit():
        return int(name)
    resolved = logging.getLevelName(name)
    if isinstance(resolved, int):
        return resolved
    raise ValueError(f"Unknown log level: {level!r}")


def _existing_handler() -> Optional[logging.Handler]:
    for handler in _root.handlers:
        if getattr(handler, "name", None) == _HANDLER_NAME:
            return handler
    return None


def enable_logging(
    level: Union[int, str] = "INFO",
    *,
    stream=None,
    fmt: Optional[str] = None,
    datefmt: Optional[str] = None,
) -> logging.Logger:
    """Turn on visible briefcase logging (opt-in).

    Idempotently adds a single :class:`logging.StreamHandler` to the
    ``briefcase`` logger, sets its level, and disables propagation so records are
    not emitted twice when the application has also configured the root logger.

    Args:
        level:   Level name (``"DEBUG"``) or number. Default ``"INFO"``.
        stream:  Target stream. Default ``sys.stderr``.
        fmt:     ``logging.Formatter`` format string. A sensible default is used.
        datefmt: Date format for ``asctime``.

    Returns:
        The ``briefcase`` logger.
    """
    lvl = _coerce_level(level)
    handler = _existing_handler()
    if handler is None:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.name = _HANDLER_NAME
        _root.addHandler(handler)
    elif stream is not None and isinstance(handler, logging.StreamHandler):
        # Only a StreamHandler has setStream. The handler is looked up by name,
        # so a caller's own handler under that name would otherwise crash here.
        handler.setStream(stream)
    handler.setFormatter(
        logging.Formatter(fmt or _DEFAULT_FORMAT, datefmt or _DEFAULT_DATEFMT)
    )
    handler.setLevel(lvl)
    _root.setLevel(lvl)
    _root.propagate = False
    return _root


def set_log_level(level: Union[int, str]) -> None:
    """Set the briefcase logger level without adding a handler."""
    lvl = _coerce_level(level)
    _root.setLevel(lvl)
    handler = _existing_handler()
    if handler is not None:
        handler.setLevel(lvl)


def disable_logging() -> None:
    """Remove the handler added by :func:`enable_logging` and restore silence."""
    handler = _existing_handler()
    if handler is not None:
        _root.removeHandler(handler)
        handler.close()
    _root.setLevel(logging.NOTSET)
    _root.propagate = True


def _configure_from_env() -> None:
    """Enable logging from ``BRIEFCASE_LOG_LEVEL`` if set. Called at import."""
    env_level = os.getenv("BRIEFCASE_LOG_LEVEL")
    if not env_level:
        return
    try:
        enable_logging(env_level)
    except ValueError:
        enable_logging("WARNING")
        _root.warning("Ignoring invalid BRIEFCASE_LOG_LEVEL=%r", env_level)


_configure_from_env()


__all__ = [
    "LIBRARY_LOGGER_NAME",
    "get_logger",
    "enable_logging",
    "set_log_level",
    "disable_logging",
]
