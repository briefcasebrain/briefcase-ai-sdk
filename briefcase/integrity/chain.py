"""Tamper-evident hash chain over append-only records.

Each appended row is paired with one chain entry so a verifier can prove,
after the fact, that:

  1. The row payload has not been altered since it was first written.
  2. The ordering implied by ``prior_hash`` has not been re-stitched.
  3. When a signing key was attached, the writer attested to the entry.

Spec version 1. Each entry's hash is::

    sha256(canonical_json({
        v:            1,
        id:           <row id>,
        table:        <stream name>,
        entity:       <partition key within the stream, or null>,
        observed_at:  <RFC 3339, nanosecond precision>,
        recorded_at:  <RFC 3339, nanosecond precision>,
        payload_hash: <sha256 of the canonical row payload>,
        supersedes:   <id of the row this one replaces, or null>,
        prior_hash:   <hex hash of the previous entry, or 32 zero bytes for genesis>,
    }))

``table`` and ``entity`` name a stream and an optional partition within
it; the wire keys are frozen so hashes stay stable across releases.
Canonical JSON is sorted-keys, tight separators, UTF-8 (see
:mod:`briefcase.integrity.canonical`; the chain profile stringifies
non-JSON values via ``fallback=str``).

Persistence lives behind the ``HashChainStore`` Protocol; see
:mod:`briefcase.integrity.stores` for an in-memory store and a JSONL
file store. Entry signing needs the ``integrity`` extra (PyNaCl); the
module itself imports on a bare install.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from briefcase.integrity.canonical import canonical_json

HASH_SPEC_VERSION = 1
GENESIS_PRIOR_HASH = "00" * 32  # 64 hex chars == 32 bytes of zero


@dataclass(frozen=True)
class HashChainEntry:
    """A single immutable link in a per-stream hash chain.

    ``hash`` is the hex sha256 over the canonical JSON of the entry's
    other fields (excluding ``hash`` and ``signature``). ``signature``,
    when present, is a base64url Ed25519 signature over the raw 32-byte
    hash digest.
    """

    row_id: str
    table: str
    entity_id: Optional[str]
    observed_at: datetime
    recorded_at: datetime
    payload_hash: str
    supersedes: Optional[str]
    prior_hash: str
    hash: str
    signature: Optional[str] = None


def _rfc3339_nanos(dt: datetime) -> str:
    """Render a ``datetime`` as RFC 3339 with nanosecond precision.

    Timestamps are canonicalized at nanosecond granularity so two rows
    written in the same microsecond still encode deterministically.
    Python's ``datetime`` only carries microseconds, so three zeros are
    appended to reach the canonical 9-digit fractional second. Naive
    datetimes are treated as UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    micros = dt.microsecond
    nanos = "{:06d}000".format(micros)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + nanos + "Z"


def compute_payload_hash(row_payload: Dict[str, Any]) -> str:
    """Hex sha256 of the canonical JSON encoding of ``row_payload``."""
    return hashlib.sha256(canonical_json(row_payload, fallback=str)).hexdigest()


def _entry_input(
    row_id: str,
    table: str,
    entity_id: Optional[str],
    observed_at: datetime,
    recorded_at: datetime,
    payload_hash: str,
    supersedes: Optional[str],
    prior_hash: str,
) -> Dict[str, Any]:
    return {
        "v": HASH_SPEC_VERSION,
        "id": row_id,
        "table": table,
        "entity": entity_id,
        "observed_at": _rfc3339_nanos(observed_at),
        "recorded_at": _rfc3339_nanos(recorded_at),
        "payload_hash": payload_hash,
        "supersedes": supersedes,
        "prior_hash": prior_hash,
    }


def compute_entry_hash(
    row_id: str,
    table: str,
    entity_id: Optional[str],
    observed_at: datetime,
    recorded_at: datetime,
    payload_hash: str,
    supersedes: Optional[str],
    prior_hash: str,
) -> str:
    """Compute the hex sha256 of an entry's canonical input."""
    blob = canonical_json(
        _entry_input(
            row_id=row_id,
            table=table,
            entity_id=entity_id,
            observed_at=observed_at,
            recorded_at=recorded_at,
            payload_hash=payload_hash,
            supersedes=supersedes,
            prior_hash=prior_hash,
        ),
        fallback=str,
    )
    return hashlib.sha256(blob).hexdigest()


def _sign_hash(signing_key: bytes, hash_hex: str) -> str:
    """Sign the raw 32 bytes of the entry hash; return base64url-nopad.

    Needs the ``integrity`` extra (PyNaCl); Ed25519 signing is
    deterministic, so the output is stable for a given seed and hash.
    """
    if len(signing_key) != 32:
        raise ValueError("ed25519 signing key must be a 32-byte seed")
    try:
        from nacl.signing import SigningKey
    except ImportError as exc:  # pragma: no cover - exercised via tests that block nacl
        raise ImportError(
            "Signing hash-chain entries requires PyNaCl. "
            "Install it with: pip install briefcase-ai[integrity]"
        ) from exc
    raw = bytes.fromhex(hash_hex)
    sig = SigningKey(signing_key).sign(raw).signature
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


@runtime_checkable
class HashChainStore(Protocol):
    """Persistence abstraction for hash-chain entries."""

    def last_entry_hash(self, table: str, entity_id: Optional[str] = None) -> str:
        """Hash of the most recent entry in the (table, entity) segment,
        or ``GENESIS_PRIOR_HASH`` when the segment is empty."""
        ...

    def append(self, entry: HashChainEntry) -> None:
        """Persist one entry."""
        ...


class HashChainAppender:
    """Build and persist hash-chain entries.

    Threading: callers serialize ``append_row`` per ``(table, entity_id)``
    segment. Two appenders racing on the same segment would both read the
    same ``prior_hash`` and the chain would fork.
    """

    def __init__(self, store: HashChainStore, signing_key: Optional[bytes] = None):
        self._store = store
        self._signing_key = signing_key

    def append_row(
        self,
        table: str,
        row_id: str,
        entity_id: Optional[str],
        observed_at: datetime,
        recorded_at: datetime,
        payload: Dict[str, Any],
        supersedes: Optional[str] = None,
    ) -> HashChainEntry:
        payload_hash = compute_payload_hash(payload)
        prior_hash = self._store.last_entry_hash(table=table, entity_id=entity_id)
        hash_hex = compute_entry_hash(
            row_id=row_id,
            table=table,
            entity_id=entity_id,
            observed_at=observed_at,
            recorded_at=recorded_at,
            payload_hash=payload_hash,
            supersedes=supersedes,
            prior_hash=prior_hash,
        )
        signature = _sign_hash(self._signing_key, hash_hex) if self._signing_key else None
        entry = HashChainEntry(
            row_id=row_id,
            table=table,
            entity_id=entity_id,
            observed_at=observed_at,
            recorded_at=recorded_at,
            payload_hash=payload_hash,
            supersedes=supersedes,
            prior_hash=prior_hash,
            hash=hash_hex,
            signature=signature,
        )
        self._store.append(entry)
        return entry


def verify_chain_segment(
    entries: List[HashChainEntry],
    *,
    expected_prior: str = GENESIS_PRIOR_HASH,
) -> Tuple[bool, Optional[str]]:
    """Verify a chronologically-ordered slice of the chain.

    For each entry, (a) recompute the entry hash from its declared fields
    and confirm it matches ``entry.hash``, and (b) confirm
    ``entry.prior_hash`` equals the previous entry's ``hash``. The first
    entry is checked against ``expected_prior``, which defaults to the
    genesis sentinel; pass the hash preceding a mid-chain window to verify
    a slice that does not start at genesis.

    Returns ``(True, None)`` if every entry in the slice is consistent;
    otherwise ``(False, row_id)`` for the first failing entry.

    Verifying ``payload_hash`` against the row itself requires re-fetching
    the row payload from the underlying store; this function verifies the
    chain's internal integrity only.
    """
    for entry in entries:
        if entry.prior_hash != expected_prior:
            return False, entry.row_id
        recomputed = compute_entry_hash(
            row_id=entry.row_id,
            table=entry.table,
            entity_id=entry.entity_id,
            observed_at=entry.observed_at,
            recorded_at=entry.recorded_at,
            payload_hash=entry.payload_hash,
            supersedes=entry.supersedes,
            prior_hash=entry.prior_hash,
        )
        if recomputed != entry.hash:
            return False, entry.row_id
        expected_prior = entry.hash
    return True, None


__all__ = [
    "GENESIS_PRIOR_HASH",
    "HASH_SPEC_VERSION",
    "HashChainAppender",
    "HashChainEntry",
    "HashChainStore",
    "compute_entry_hash",
    "compute_payload_hash",
    "verify_chain_segment",
]
