"""Interpreter-exit state shared by the export pipeline and the exporters.

On Python 3.12+ the ``atexit`` hooks run after ``threading._shutdown``. From
then on the first use of asyncio's default executor (``asyncio.to_thread``,
``loop.run_in_executor``) fails: importing ``concurrent.futures.thread``
raises ``RuntimeError: can't register atexit after shutdown`` and an already
imported executor refuses new work. The exit-time drain therefore flushes
synchronously in the calling thread and starts no threads; exporters that
would hand blocking work to a thread use :func:`run_blocking`, which runs it
inline once the drain has begun.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_exiting = threading.Event()


def mark_exiting() -> None:
    """The exit-time drain has begun: start no threads from here on."""
    _exiting.set()


def clear_exiting() -> None:
    """A forked child is a new process; the parent's exit state is not its own."""
    _exiting.clear()


def exiting() -> bool:
    """True once the exit-time drain has begun."""
    return _exiting.is_set()


async def run_blocking(fn: Callable[..., T], *args: Any) -> T:
    """Run blocking ``fn`` off the event loop; inline during the exit drain.

    Falls back to calling ``fn`` in the current thread when the default
    executor cannot be used because the interpreter is shutting down. An
    error raised by ``fn`` itself is never retried inline.
    """
    if _exiting.is_set():
        return fn(*args)
    started = []

    def call() -> T:
        started.append(True)
        return fn(*args)

    try:
        return await asyncio.to_thread(call)
    except RuntimeError:
        if started:
            raise
        return fn(*args)
