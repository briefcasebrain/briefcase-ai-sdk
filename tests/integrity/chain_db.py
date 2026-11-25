"""Test helper: write a chained store in pure Python (the Rust core's schema).

The facade suite runs with a mocked native core, so it cannot ask Rust to
write a chain. This writer follows the documented format (schema migration
v2 of ``sqlite.rs``, entry and checkpoint hashes of
``briefcase.integrity.evidence``); the native suite separately proves that
chains written by Rust verify in Python and vice versa.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from briefcase.integrity import keys, merkle
from briefcase.integrity.canonical import canonical_json_v2
from briefcase.integrity.evidence import GENESIS_HASH, checkpoint_hash, compute_entry_hash, sha256_hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, snapshot_type TEXT NOT NULL,
    data_json TEXT NOT NULL, created_at DATETIME NOT NULL, created_by TEXT, checksum TEXT);
CREATE TABLE IF NOT EXISTS chain_entries (seq INTEGER PRIMARY KEY, record_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL, hash_scheme TEXT NOT NULL, record_json TEXT NOT NULL, record_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL UNIQUE, entry_hash TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain_checkpoints (checkpoint_no INTEGER PRIMARY KEY, from_seq INTEGER NOT NULL,
    seq INTEGER NOT NULL, head_hash TEXT NOT NULL, merkle_root TEXT NOT NULL,
    prev_checkpoint_hash TEXT NOT NULL, created_at TEXT NOT NULL, hash_scheme TEXT NOT NULL,
    key_id TEXT, public_key TEXT, signature TEXT, checkpoint_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class ChainWriter:
    def __init__(self, path: Path, seed: Optional[bytes] = None) -> None:
        self.path = Path(path)
        self.seed = seed
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        self.clock = 0

    def _now(self) -> str:
        self.clock += 1
        return "2026-11-22T10:00:%02d.000000Z" % (self.clock % 60)

    def append(self, record_id: str, record: Any, kind: str = "record") -> Dict[str, Any]:
        row = self.conn.execute("SELECT seq, entry_hash FROM chain_entries ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS_HASH)
        record_json = canonical_json_v2(record).decode("utf-8")
        record_hash = sha256_hex(record_json.encode("utf-8"))
        at = self._now()
        entry_hash = compute_entry_hash("jcs-1", seq, record_id, kind, prev, record_hash, at)
        self.conn.execute(
            "INSERT INTO chain_entries VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, record_id, kind, "jcs-1", record_json, record_hash, prev, entry_hash, at),
        )
        if kind in ("decision", "snapshot"):
            self.conn.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                (record_id, "Decision", json.dumps(record), "2026-11-22 10:00:00.000", None, None),
            )
        self.conn.commit()
        return {"seq": seq, "entry_hash": entry_hash}

    def checkpoint(self) -> Dict[str, Any]:
        last = self.conn.execute(
            "SELECT checkpoint_no, seq, checkpoint_hash FROM chain_checkpoints ORDER BY checkpoint_no DESC LIMIT 1"
        ).fetchone()
        start = last[1] + 1 if last else 1
        rows = self.conn.execute(
            "SELECT seq, entry_hash FROM chain_entries WHERE seq >= ? ORDER BY seq", (start,)
        ).fetchall()
        jwk = keys.public_jwk(self.seed) if self.seed else None
        cp: Dict[str, Any] = {
            "checkpoint_no": last[0] + 1 if last else 1,
            "from_seq": start,
            "seq": rows[-1][0],
            "head_hash": rows[-1][1],
            "merkle_root": merkle.merkle_root([bytes.fromhex(r[1]) for r in rows]).hex(),
            "prev_checkpoint_hash": last[2] if last else GENESIS_HASH,
            "created_at": self._now(),
            "hash_scheme": "jcs-1",
            "key_id": keys.key_id(jwk["x"]) if jwk else None,
            "public_key": jwk["x"] if jwk else None,
            "signature": None,
        }
        cp["checkpoint_hash"] = checkpoint_hash(cp)
        if self.seed:
            cp["signature"] = keys.sign_digest(self.seed, cp["checkpoint_hash"])
        cols = list(cp)
        self.conn.execute(
            "INSERT INTO chain_checkpoints (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
            [cp[c] for c in cols],
        )
        self.conn.commit()
        return cp

    def sql(self, statement: str, params=()) -> None:
        self.conn.execute(statement, params)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
