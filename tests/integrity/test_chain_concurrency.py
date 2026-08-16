"""Concurrent appends must not fork the chain.

The store contract is compare-and-swap on ``prior_hash``: ``append``
raises :class:`ChainConflictError` when the entry's ``prior_hash`` no
longer matches the segment tail, and the appender re-reads the tail and
retries. The in-memory and JSONL stores both enforce it; the JSONL store
additionally holds a POSIX ``flock`` so separate processes on one host
serialize through the file itself.
"""

from __future__ import annotations

import sys
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from briefcase.integrity import (
    ChainConflictError,
    GENESIS_PRIOR_HASH,
    HashChainAppender,
    InMemoryHashChainStore,
    JsonlHashChainStore,
    verify_chain_segment,
)


def _ts(n: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, n % 60, (n * 7) % 1_000_000, tzinfo=timezone.utc)


def _append(appender: HashChainAppender, i: int, entity: str = "e") -> None:
    appender.append_row(
        table="audit",
        row_id=str(uuid.uuid4()),
        entity_id=entity,
        observed_at=_ts(i),
        recorded_at=_ts(i + 1),
        payload={"i": i},
    )


def test_store_append_rejects_stale_prior() -> None:
    store = InMemoryHashChainStore()
    appender = HashChainAppender(store)
    first = appender.append_row(
        table="audit",
        row_id=str(uuid.uuid4()),
        entity_id="e",
        observed_at=_ts(0),
        recorded_at=_ts(1),
        payload={"i": 0},
    )
    # A forked entry claiming the genesis tail after an append must be
    # rejected, not silently stored.
    import dataclasses

    fork = dataclasses.replace(first, row_id=str(uuid.uuid4()))
    with pytest.raises(ChainConflictError):
        store.append(fork)


class _ConflictOnce:
    """Store wrapper that rejects the first append with a conflict."""

    def __init__(self, inner: InMemoryHashChainStore) -> None:
        self._inner = inner
        self._fired = False

    def last_entry_hash(self, table, entity_id=None):
        return self._inner.last_entry_hash(table, entity_id)

    def append(self, entry) -> None:
        if not self._fired:
            self._fired = True
            raise ChainConflictError("injected: tail moved")
        self._inner.append(entry)


def test_appender_retries_conflict_to_success() -> None:
    inner = InMemoryHashChainStore()
    appender = HashChainAppender(_ConflictOnce(inner))
    _append(appender, 0)
    entries = inner.entries_for("audit", "e")
    assert len(entries) == 1
    assert entries[0].prior_hash == GENESIS_PRIOR_HASH


class _AlwaysConflict:
    def last_entry_hash(self, table, entity_id=None):
        return GENESIS_PRIOR_HASH

    def append(self, entry) -> None:
        raise ChainConflictError("tail moved")


def test_appender_gives_up_after_max_attempts() -> None:
    appender = HashChainAppender(_AlwaysConflict())
    with pytest.raises(ChainConflictError):
        _append(appender, 0)


def test_threaded_appends_do_not_fork() -> None:
    store = InMemoryHashChainStore()
    appender = HashChainAppender(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: _append(appender, i), range(200)))
    entries = store.entries_for("audit", "e")
    assert len(entries) == 200
    assert verify_chain_segment(entries) == (True, None)


def test_two_jsonl_store_instances_share_one_file(tmp_path: Path) -> None:
    # Two instances on the same path have independent tail caches, the
    # same situation as two processes; CAS + refresh must converge them.
    path = tmp_path / "chain.jsonl"
    a = HashChainAppender(JsonlHashChainStore(path))
    b = HashChainAppender(JsonlHashChainStore(path))
    for i in range(10):
        _append(a if i % 2 == 0 else b, i)
    entries = JsonlHashChainStore(path).load_entries()
    assert len(entries) == 10
    assert verify_chain_segment(entries) == (True, None)


def _worker(args) -> int:
    path, worker_id, count = args
    appender = HashChainAppender(JsonlHashChainStore(path))
    for i in range(count):
        _append(appender, worker_id * 1000 + i)
    return count


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")
def test_multiprocess_jsonl_appends_do_not_fork(tmp_path: Path) -> None:
    path = str(tmp_path / "chain.jsonl")
    with ProcessPoolExecutor(max_workers=3) as pool:
        totals = list(pool.map(_worker, [(path, w, 15) for w in range(3)]))
    assert sum(totals) == 45
    entries = JsonlHashChainStore(path).load_entries()
    assert len(entries) == 45
    assert verify_chain_segment(entries) == (True, None)
