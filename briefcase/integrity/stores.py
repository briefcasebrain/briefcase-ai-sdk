"""Hash-chain stores: in-memory and JSONL file.

Both implement the ``HashChainStore`` Protocol. The in-memory store is for
tests and short-lived processes; the JSONL store persists one entry per
line and is single-writer per file (concurrent writers would fork the
chain, see ``HashChainAppender``). Databases and object stores are
app-side adapters: implement the two-method Protocol against your schema.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from briefcase.integrity.chain import GENESIS_PRIOR_HASH, HashChainEntry


class TruncatedChainFileError(ValueError):
    """A chain file's final line is not complete JSON.

    Raised instead of silently skipping the tail so a crash mid-write (or
    truncation by an attacker) is surfaced to the verifier.
    """


class InMemoryHashChainStore:
    """Thread-safe in-memory ``HashChainStore``.

    Entries are kept in insertion order; ``last_entry_hash`` scans the
    tail for the most recent matching entry. Suitable for tests, not for
    durable audit trails.
    """

    def __init__(self) -> None:
        self._entries: List[HashChainEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: HashChainEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def last_entry_hash(self, table: str, entity_id: Optional[str] = None) -> str:
        with self._lock:
            for entry in reversed(self._entries):
                if entry.table != table:
                    continue
                if entity_id is not None and entry.entity_id != entity_id:
                    continue
                return entry.hash
        return GENESIS_PRIOR_HASH

    def entries_for(self, table: str, entity_id: Optional[str] = None) -> List[HashChainEntry]:
        """Chronologically-ordered slice for ``table``/``entity_id``."""
        with self._lock:
            return [
                e
                for e in self._entries
                if e.table == table and (entity_id is None or e.entity_id == entity_id)
            ]

    def all_entries(self) -> List[HashChainEntry]:
        with self._lock:
            return list(self._entries)


def _opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _entry_to_dict(entry: HashChainEntry) -> Dict[str, object]:
    d = asdict(entry)
    d["observed_at"] = entry.observed_at.isoformat()
    d["recorded_at"] = entry.recorded_at.isoformat()
    return d


def _entry_from_dict(d: Dict[str, object]) -> HashChainEntry:
    return HashChainEntry(
        row_id=str(d["row_id"]),
        table=str(d["table"]),
        entity_id=None if d.get("entity_id") is None else str(d["entity_id"]),
        observed_at=datetime.fromisoformat(str(d["observed_at"])),
        recorded_at=datetime.fromisoformat(str(d["recorded_at"])),
        payload_hash=str(d["payload_hash"]),
        supersedes=None if d.get("supersedes") is None else str(d["supersedes"]),
        prior_hash=str(d["prior_hash"]),
        hash=str(d["hash"]),
        signature=None if d.get("signature") is None else str(d["signature"]),
    )


class JsonlHashChainStore:
    """``HashChainStore`` appending entries to a JSONL file.

    One JSON object per line. Parent directories are created 0700 and the
    file is opened owner-only 0600 (the same posture as the JSONL record
    exporter): entries carry only hashes, but the chain's own integrity
    deserves a private file. Segment tails are tracked in memory after an
    initial scan, so ``last_entry_hash`` never re-reads the file.

    Single-writer per file: open one store per path per process and
    serialize appends per segment (the appender's contract).
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._fh = None
        self._tails: Dict[Tuple[str, Optional[str]], str] = {}
        self._table_tails: Dict[str, str] = {}
        parent = self._path.parent
        if parent and not parent.exists():
            missing = []
            probe = parent
            while not probe.exists():
                missing.append(probe)
                if probe.parent == probe:
                    break
                probe = probe.parent
            for directory in reversed(missing):
                try:
                    os.mkdir(directory, 0o700)
                except FileExistsError:
                    pass
        for entry in self.load_entries():
            self._tails[(entry.table, entry.entity_id)] = entry.hash
            self._table_tails[entry.table] = entry.hash

    def _ensure_open(self):
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8", opener=_opener)
            if hasattr(os, "fchmod"):
                os.fchmod(self._fh.fileno(), 0o600)
        return self._fh

    def append(self, entry: HashChainEntry) -> None:
        line = json.dumps(_entry_to_dict(entry), sort_keys=True, separators=(",", ":"))
        with self._lock:
            fh = self._ensure_open()
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            self._tails[(entry.table, entry.entity_id)] = entry.hash
            self._table_tails[entry.table] = entry.hash

    def last_entry_hash(self, table: str, entity_id: Optional[str] = None) -> str:
        with self._lock:
            if entity_id is not None:
                return self._tails.get((table, entity_id), GENESIS_PRIOR_HASH)
            # No partition given: the most recently appended entry for the
            # table regardless of partition, matching the in-memory store.
            return self._table_tails.get(table, GENESIS_PRIOR_HASH)

    def load_entries(self) -> List[HashChainEntry]:
        """Read every entry in file order, for verification.

        Raises :class:`TruncatedChainFileError` when the final line is not
        parseable JSON (crash mid-write); any earlier malformed line also
        raises, since a verifier must never skip records silently.
        """
        if not self._path.exists():
            return []
        entries: List[HashChainEntry] = []
        with open(self._path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entries.append(_entry_from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                raise TruncatedChainFileError(
                    "chain file %s line %d is not a valid entry" % (self._path, index + 1)
                ) from exc
        return entries

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


__all__ = [
    "InMemoryHashChainStore",
    "JsonlHashChainStore",
    "TruncatedChainFileError",
]
