"""One advisory lock for every command that reads or writes the connect journal or a tool's config.

``connect``, ``mcp install``, ``policy init``/``off`` and ``--undo`` each hold it while they apply and record, so
two commands never interleave their journal updates (a lost ownership record would leave an enforcement hook that
``policy off`` cannot see). It is advisory: an editor saving the same file inside the few milliseconds between our
final compare and the rename can still lose its save, and no operating system gives us better without the tool's
cooperation. ``fcntl`` on POSIX, ``msvcrt`` on Windows; the same pattern as the engine install lock.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

from briefcase.connect import edits

_held = threading.local()


@contextlib.contextmanager
def command_lock(enabled: bool = True) -> Iterator[None]:
    """Exclusive, blocking, and re-entrant within one process. ``enabled=False`` (a dry run) takes no lock. It is
    taken only around the part that reads and writes the journal, so a command that writes nothing creates nothing,
    and the lock file is never removed (removing it would let a waiter and a third writer hold different inodes)."""
    if not enabled:
        yield
        return
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = edits.journal_path().parent / ".command.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            else:  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
