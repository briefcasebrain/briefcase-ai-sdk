"""JSONL and in-memory store behavior: round trips, tails, perms, truncation."""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from briefcase.integrity import (
    GENESIS_PRIOR_HASH,
    HashChainAppender,
    HashChainStore,
    InMemoryHashChainStore,
    JsonlHashChainStore,
    TruncatedChainFileError,
    verify_chain_segment,
)


def _ts(n: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, n % 60, tzinfo=timezone.utc)


def _append(store: HashChainStore, n: int, table: str = "audit", entity: str = "e") -> list:
    appender = HashChainAppender(store)
    return [
        appender.append_row(
            table=table,
            row_id=str(uuid.uuid4()),
            entity_id=entity,
            observed_at=_ts(i),
            recorded_at=_ts(i + 1),
            payload={"i": i},
        )
        for i in range(n)
    ]


def test_jsonl_round_trip_and_verify(tmp_path: Path) -> None:
    path = tmp_path / "chain.jsonl"
    store = JsonlHashChainStore(path)
    written = _append(store, 5)
    store.close()

    reloaded = JsonlHashChainStore(path)
    entries = reloaded.load_entries()
    assert entries == written
    assert verify_chain_segment(entries) == (True, None)
    # Tail state survives the reopen: the next append keeps chaining.
    more = _append(reloaded, 1)
    assert more[0].prior_hash == written[-1].hash
    reloaded.close()


def test_jsonl_tails_per_segment_and_per_table(tmp_path: Path) -> None:
    store = JsonlHashChainStore(tmp_path / "chain.jsonl")
    a = _append(store, 2, entity="A")
    b = _append(store, 2, entity="B")
    assert store.last_entry_hash("audit", "A") == a[-1].hash
    assert store.last_entry_hash("audit", "B") == b[-1].hash
    # No partition: most recently appended entry for the table.
    assert store.last_entry_hash("audit") == b[-1].hash
    assert store.last_entry_hash("other") == GENESIS_PRIOR_HASH
    store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_jsonl_dirs_0700_and_file_0600(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "chain.jsonl"
    store = JsonlHashChainStore(path)
    _append(store, 1)
    store.close()
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o600)
    assert oct(os.stat(path.parent).st_mode & 0o777) == oct(0o700)
    assert oct(os.stat(path.parent.parent).st_mode & 0o777) == oct(0o700)


def test_jsonl_truncated_tail_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "chain.jsonl"
    store = JsonlHashChainStore(path)
    _append(store, 3)
    store.close()

    content = path.read_text()
    path.write_text(content[:-20])  # cut into the last line mid-record
    with pytest.raises(TruncatedChainFileError):
        JsonlHashChainStore(path)


def test_jsonl_tampered_line_fails_verification(tmp_path: Path) -> None:
    path = tmp_path / "chain.jsonl"
    store = JsonlHashChainStore(path)
    _append(store, 3)
    store.close()

    lines = path.read_text().splitlines()
    assert '"payload_hash":"' in lines[1]
    lines[1] = lines[1].replace('"payload_hash":"', '"payload_hash":"ff', 1)
    path.write_text("\n".join(lines) + "\n")
    entries = JsonlHashChainStore(path).load_entries()
    ok, failing = verify_chain_segment(entries)
    assert ok is False
    assert failing == entries[1].row_id


def test_jsonl_empty_or_missing_file(tmp_path: Path) -> None:
    store = JsonlHashChainStore(tmp_path / "missing.jsonl")
    assert store.load_entries() == []
    assert store.last_entry_hash("audit") == GENESIS_PRIOR_HASH


def test_in_memory_segment_slices() -> None:
    store = InMemoryHashChainStore()
    a = _append(store, 2, entity="A")
    b = _append(store, 1, entity="B")
    assert store.entries_for("audit", "A") == a
    assert store.entries_for("audit", "B") == b
    assert store.entries_for("audit") == a + b
    assert store.last_entry_hash("audit") == b[-1].hash
    assert store.last_entry_hash("audit", "A") == a[-1].hash


def test_both_stores_satisfy_the_protocol(tmp_path: Path) -> None:
    assert isinstance(InMemoryHashChainStore(), HashChainStore)
    assert isinstance(JsonlHashChainStore(tmp_path / "c.jsonl"), HashChainStore)
