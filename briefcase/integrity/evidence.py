"""Verify evidence without the compiled core: chained SQLite stores and JSONL.

An independent, pure-Python implementation of the checks the Rust core runs
in ``SqliteBackend.verify_chain`` (same hashes, same Merkle layout, same
problem kinds), so a chain written by Rust is re-verified here byte for byte
and ``briefcase verify`` works on any install. It reads the database
read-only and never writes to it.

What it can see, for a chained store (``SqliteBackend(path, chained=True)``):

* an entry changed in place (``modified_entry``), or the decision row next
  to it (``snapshot_modified`` / ``snapshot_missing``);
* entries deleted from the middle (``missing_entries``) or reordered /
  inserted (``broken_link``);
* rows written around the chain (``unchained_record``);
* checkpoints edited, removed, re-signed or signed by an unexpected key
  (``checkpoint_*``, ``unexpected_key``), and entries rewritten after a
  checkpoint (``checkpoint_mismatch``).

What it cannot see from the database alone: removal of the newest entries
after the last checkpoint (and of that checkpoint). Pass a checkpoint kept
elsewhere as ``against`` to check that (``truncated``). A writer holding the
signing key can rebuild the whole log consistently; only a checkpoint
anchored outside the writer's reach (or a pinned key plus an earlier
anchor) exposes that.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from briefcase.integrity import keys, merkle
from briefcase.integrity.canonical import (
    HASH_SCHEME_V2,
    canonical_json_v2,
    parse_canonical_text,
)

GENESIS_HASH = "0" * 64

# Problem kinds that break the chain at a given seq.
_BREAKING = {
    "broken_link",
    "modified_entry",
    "missing_entries",
    "snapshot_modified",
    "snapshot_missing",
    "unknown_hash_scheme",
}

NOTE_TRUNCATION = (
    "Removal of the newest entries after the last checkpoint cannot be detected from the "
    "database alone; verify against a checkpoint kept outside it (against_checkpoint)."
)
NOTE_UNSIGNED = (
    "No checkpoint is signed: anyone able to write the database could have rebuilt it consistently."
)


@dataclass
class Problem:
    kind: str
    seq: Optional[int]
    detail: str


@dataclass
class VerifyReport:
    """Same keys as the Rust core's ``VerifyReport`` plus ``source``/``format``."""

    intact: bool = False
    entries_checked: int = 0
    first_seq: Optional[int] = None
    last_seq: Optional[int] = None
    head_hash: Optional[str] = None
    checkpoints_checked: int = 0
    signed_checkpoints: int = 0
    signatures_verified: int = 0
    last_checkpoint_seq: Optional[int] = None
    entries_after_last_checkpoint: int = 0
    key_ids: List[str] = field(default_factory=list)
    hash_schemes: List[str] = field(default_factory=list)
    first_broken_seq: Optional[int] = None
    missing_ranges: List[Tuple[int, int]] = field(default_factory=list)
    modified_seqs: List[int] = field(default_factory=list)
    anchored: bool = False
    problems: List[Problem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    source: str = ""
    format: str = ""
    records_checked: int = 0

    def problem(self, kind: str, seq: Optional[int], detail: str) -> None:
        if seq is not None:
            if kind in _BREAKING:
                self.first_broken_seq = seq if self.first_broken_seq is None else min(
                    self.first_broken_seq, seq
                )
            if kind in ("modified_entry", "snapshot_modified") and seq not in self.modified_seqs:
                self.modified_seqs.append(seq)
        self.problems.append(Problem(kind, seq, detail))

    def finish(self) -> "VerifyReport":
        self.missing_ranges.sort()
        self.modified_seqs.sort()
        self.intact = not self.problems
        return self

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["missing_ranges"] = [list(r) for r in self.missing_ranges]
        return out


class EvidenceFormatError(ValueError):
    """The path is not a chained store, chain JSONL or bundle this module reads."""


KeyPin = Optional[Union[str, Sequence[str]]]


def allowed_keys(expected: KeyPin) -> Optional[set]:
    """The set of acceptable key ids, or ``None`` when nothing is pinned."""
    if not expected:
        return None
    return {expected} if isinstance(expected, str) else set(expected)


def _short(value: Optional[str]) -> str:
    return (value or "")[:12]


def _entries(first: int, last: int, one: str, many: str) -> str:
    """``entry 4 was removed`` / ``entries 3-5 were removed``."""
    if first == last:
        return "entry %d %s" % (first, one)
    return "entries %d-%d %s" % (first, last, many)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_entry_hash(
    hash_scheme: str,
    seq: int,
    record_id: str,
    kind: str,
    prev_hash: str,
    record_hash: str,
    recorded_at: str,
) -> str:
    """``entry_hash`` of a chained-store entry (same bytes as the Rust core)."""
    if hash_scheme != HASH_SCHEME_V2:
        raise ValueError("chained-store entries use %s, not %r" % (HASH_SCHEME_V2, hash_scheme))
    body = {
        "v": 1,
        "hash_scheme": hash_scheme,
        "seq": seq,
        "record_id": record_id,
        "kind": kind,
        "prev_hash": prev_hash,
        "record_hash": record_hash,
        "recorded_at": recorded_at,
    }
    return sha256_hex(canonical_json_v2(body))


CHECKPOINT_BODY_FIELDS = (
    "hash_scheme",
    "checkpoint_no",
    "from_seq",
    "seq",
    "head_hash",
    "merkle_root",
    "prev_checkpoint_hash",
    "created_at",
    "key_id",
)


def checkpoint_hash(cp: Dict[str, Any]) -> str:
    """Recompute ``checkpoint_hash`` from a checkpoint's signed fields."""
    body: Dict[str, Any] = {"v": 1}
    for name in CHECKPOINT_BODY_FIELDS:
        body[name] = cp.get(name)
    return sha256_hex(canonical_json_v2(body))


def _record_text_ok(record_json: str, record_hash: str) -> bool:
    try:
        canonical = canonical_json_v2(parse_canonical_text(record_json)).decode("utf-8")
    except (ValueError, TypeError):
        return False
    return canonical == record_json and sha256_hex(record_json.encode("utf-8")) == record_hash


def record_hash(record: Any) -> str:
    """``record_hash`` of a record value (``jcs-1``)."""
    return sha256_hex(canonical_json_v2(record))


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


def _walk_entries(
    report: VerifyReport,
    entries: Iterable[Dict[str, Any]],
    snapshots: Optional[Dict[str, str]],
) -> Dict[int, str]:
    """Check linkage, hashes and (when given) snapshot rows; return seq->hash."""
    expected_seq = 1
    prev_hash = GENESIS_HASH
    by_seq: Dict[int, str] = {}
    chained_ids = set()
    for entry in entries:
        seq = int(entry["seq"])
        report.entries_checked += 1
        if report.first_seq is None:
            report.first_seq = seq
        report.last_seq = seq
        report.head_hash = entry["entry_hash"]
        chained_ids.add(entry["record_id"])
        by_seq[seq] = entry["entry_hash"]
        if entry["hash_scheme"] not in report.hash_schemes:
            report.hash_schemes.append(entry["hash_scheme"])
        if seq > expected_seq:
            report.missing_ranges.append((expected_seq, seq - 1))
            report.problem(
                "missing_entries",
                expected_seq,
                _entries(expected_seq, seq - 1, "is missing", "are missing") + " (deleted, or the log was cut)",
            )
        elif entry["prev_hash"] != prev_hash:
            report.problem(
                "broken_link",
                seq,
                "entry %d does not follow the entry before it (prev_hash %s expected %s): "
                "entries were reordered, inserted or replaced"
                % (seq, _short(entry["prev_hash"]), _short(prev_hash)),
            )
        expected_seq = seq + 1
        prev_hash = entry["entry_hash"]
        if entry["hash_scheme"] != HASH_SCHEME_V2:
            report.problem(
                "unknown_hash_scheme", seq, "entry %d names hash scheme %r" % (seq, entry["hash_scheme"])
            )
            continue
        record_ok = _record_text_ok(entry["record_json"], entry["record_hash"])
        recomputed = compute_entry_hash(
            entry["hash_scheme"],
            seq,
            entry["record_id"],
            entry["kind"],
            entry["prev_hash"],
            entry["record_hash"],
            entry["recorded_at"],
        )
        if not record_ok or recomputed != entry["entry_hash"]:
            report.problem(
                "modified_entry",
                seq,
                "entry %d (record %s) was changed after it was written" % (seq, entry["record_id"]),
            )
        if snapshots is not None and entry["kind"] in ("decision", "snapshot"):
            data_json = snapshots.get(entry["record_id"])
            if data_json is None:
                report.problem(
                    "snapshot_missing",
                    seq,
                    "snapshot %s (entry %d) was deleted" % (entry["record_id"], seq),
                )
            else:
                try:
                    same = (
                        canonical_json_v2(parse_canonical_text(data_json)).decode("utf-8")
                        == entry["record_json"]
                    )
                except (ValueError, TypeError):
                    same = False
                if not same:
                    report.problem(
                        "snapshot_modified",
                        seq,
                        "snapshot %s (entry %d) differs from what was recorded"
                        % (entry["record_id"], seq),
                    )
    if snapshots is not None:
        for snapshot_id in sorted(snapshots):
            if snapshot_id not in chained_ids:
                report.problem(
                    "unchained_record",
                    None,
                    "snapshot %s is not in the chain (written by a handle without the chain, "
                    "or inserted directly)" % snapshot_id,
                )
    return by_seq


def check_checkpoint(
    report: VerifyReport,
    cp: Dict[str, Any],
    by_seq: Optional[Dict[int, str]],
    expected_key_id: KeyPin,
    label: str = "checkpoint",
) -> bool:
    """Hash, signature and (with ``by_seq``) head/root checks. True if clean."""
    before = len(report.problems)
    no = cp.get("checkpoint_no")
    try:
        recomputed = checkpoint_hash(cp)
    except (ValueError, TypeError):
        recomputed = None
    if recomputed != cp.get("checkpoint_hash"):
        report.problem("checkpoint_modified", None, "%s %s was changed after it was written" % (label, no))
    sig, x, kid = cp.get("signature"), cp.get("public_key"), cp.get("key_id")
    if sig and x and kid:
        report.signed_checkpoints += 1
        if kid not in report.key_ids:
            report.key_ids.append(kid)
        if keys.key_id(x) != kid:
            report.problem(
                "checkpoint_key_mismatch",
                None,
                "%s %s names key %s but carries a different public key" % (label, no, _short(kid)),
            )
        elif keys.verify_digest(str(cp.get("checkpoint_hash")), sig, x):
            report.signatures_verified += 1
        else:
            report.problem(
                "checkpoint_signature_invalid", None, "%s %s has a signature that does not verify" % (label, no)
            )
        allowed = allowed_keys(expected_key_id)
        if allowed is not None and kid not in allowed:
            report.problem(
                "unexpected_key",
                None,
                "%s %s is signed by key %s, not the expected key %s"
                % (label, no, _short(kid), " or ".join(_short(k) for k in sorted(allowed))),
            )
    elif not sig and not x and not kid:
        if expected_key_id:
            report.problem(
                "unsigned_checkpoint", None, "%s %s is not signed but a signing key is required" % (label, no)
            )
    else:
        report.problem(
            "checkpoint_signature_invalid",
            None,
            "%s %s has an incomplete signature (signature, key or key id missing)" % (label, no),
        )
    if by_seq is not None:
        start, end = int(cp["from_seq"]), int(cp["seq"])
        hashes = [by_seq.get(s) for s in range(start, end + 1)]
        if all(h is not None for h in hashes):
            if by_seq.get(end) != cp.get("head_hash"):
                report.problem(
                    "checkpoint_mismatch",
                    end,
                    "%s %s recorded head %s at entry %d, the log now has a different entry there "
                    "(rewritten after the checkpoint)" % (label, no, _short(cp.get("head_hash")), end),
                )
            else:
                root = merkle.merkle_root([bytes.fromhex(h) for h in hashes])  # type: ignore[arg-type]
                if root.hex() != cp.get("merkle_root"):
                    report.problem(
                        "checkpoint_mismatch",
                        start,
                        "%s %s: %s its Merkle root (rewritten after the checkpoint)"
                        % (label, no, _entries(start, end, "no longer matches", "no longer match")),
                    )
    return len(report.problems) == before


def _walk_checkpoints(
    report: VerifyReport,
    checkpoints: Sequence[Dict[str, Any]],
    by_seq: Dict[int, str],
    against: Optional[Dict[str, Any]],
    expected_key_id: KeyPin,
) -> None:
    prev_hash, prev_seq = GENESIS_HASH, 0
    for index, cp in enumerate(checkpoints):
        report.checkpoints_checked += 1
        report.last_checkpoint_seq = int(cp["seq"])
        if int(cp["checkpoint_no"]) != index + 1:
            report.problem(
                "checkpoint_chain_broken",
                None,
                "checkpoint %s is out of sequence (expected %d)" % (cp["checkpoint_no"], index + 1),
            )
        if cp.get("prev_checkpoint_hash") != prev_hash or int(cp["from_seq"]) != prev_seq + 1:
            report.problem(
                "checkpoint_chain_broken",
                None,
                "checkpoint %s does not follow the previous checkpoint (a checkpoint was removed "
                "or replaced)" % cp["checkpoint_no"],
            )
        prev_hash, prev_seq = str(cp.get("checkpoint_hash")), int(cp["seq"])
        check_checkpoint(report, cp, by_seq, expected_key_id)
    head = report.last_seq or 0
    if report.last_checkpoint_seq is not None and report.last_checkpoint_seq > head:
        report.problem(
            "truncated",
            head + 1,
            "the log ends at entry %d but a checkpoint covers up to entry %d: %s"
            % (head, report.last_checkpoint_seq,
               _entries(head + 1, report.last_checkpoint_seq, "was removed", "were removed")),
        )
    report.entries_after_last_checkpoint = max(0, head - (report.last_checkpoint_seq or 0))
    if against is not None:
        report.anchored = True
        try:
            anchor_ok = checkpoint_hash(against) == against.get("checkpoint_hash")
        except (ValueError, TypeError, KeyError):
            anchor_ok = False
        if not anchor_ok:
            report.problem(
                "anchor_invalid", None, "the checkpoint given to verify against does not match its own hash"
            )
        else:
            if head < int(against["seq"]):
                report.problem(
                    "truncated",
                    head + 1,
                    "the log ends at entry %d but the anchored checkpoint %s covers up to entry %d: %s"
                    % (head, against["checkpoint_no"], against["seq"],
                       _entries(head + 1, int(against["seq"]), "was removed", "were removed")),
                )
            for cp in checkpoints:
                if cp["checkpoint_no"] == against["checkpoint_no"] and cp.get(
                    "checkpoint_hash"
                ) != against.get("checkpoint_hash"):
                    report.problem(
                        "checkpoint_replaced",
                        None,
                        "checkpoint %s in the database differs from the anchored copy"
                        % against["checkpoint_no"],
                    )
            check_checkpoint(report, against, by_seq, expected_key_id, "anchored checkpoint")
    else:
        report.notes.append(NOTE_TRUNCATION)
    if report.signed_checkpoints == 0 and report.checkpoints_checked > 0:
        report.notes.append(NOTE_UNSIGNED)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

_ENTRY_COLUMNS = (
    "seq, record_id, kind, hash_scheme, record_json, prev_hash, record_hash, entry_hash, recorded_at"
)
_CHECKPOINT_COLUMNS = (
    "checkpoint_no, from_seq, seq, head_hash, merkle_root, prev_checkpoint_hash, created_at, "
    "hash_scheme, key_id, public_key, signature, checkpoint_hash"
)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.resolve(), uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def is_sqlite(path: Union[str, Path]) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


class ChainedStore:
    """Read-only view of a chained SQLite store (no native code)."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        self._conn = _connect_readonly(self.path)
        tables = {
            row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "chain_entries" not in tables:
            self._conn.close()
            raise EvidenceFormatError(
                "%s is a SQLite database without a hash chain. Open it with "
                "SqliteBackend(path, chained=True) (or briefcase.observe('chain:%s')) to start one."
                % (self.path, self.path)
            )
        self.has_snapshots = "snapshots" in tables

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ChainedStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def entries(self, start: int = 1, end: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT %s FROM chain_entries WHERE seq >= ? AND seq <= ? ORDER BY seq" % _ENTRY_COLUMNS,
            (start, end if end is not None else 2**62),
        )
        return [dict(row) for row in rows]

    def checkpoints(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT %s FROM chain_checkpoints ORDER BY checkpoint_no" % _CHECKPOINT_COLUMNS
        )
        return [dict(row) for row in rows]

    def snapshots(self) -> Dict[str, str]:
        if not self.has_snapshots:
            return {}
        return {row[0]: row[1] for row in self._conn.execute("SELECT id, data_json FROM snapshots")}

    def meta(self) -> Dict[str, str]:
        try:
            return {row[0]: row[1] for row in self._conn.execute("SELECT key, value FROM chain_meta")}
        except sqlite3.Error:
            return {}


def verify_store(
    path: Union[str, Path],
    *,
    against: Optional[Dict[str, Any]] = None,
    expected_key_id: KeyPin = None,
) -> VerifyReport:
    """Verify a chained SQLite store end to end (read-only)."""
    report = VerifyReport(source=str(path), format="chained-store")
    with ChainedStore(path) as store:
        by_seq = _walk_entries(report, store.entries(), store.snapshots())
        report.records_checked = report.entries_checked
        _walk_checkpoints(report, store.checkpoints(), by_seq, against, expected_key_id)
    return report.finish()


def export_jsonl(path: Union[str, Path], out: Union[str, Path]) -> int:
    """Write a chained store as JSONL: one ``entry`` line per record (with the
    record inline) and one ``checkpoint`` line per checkpoint. Returns the
    number of lines. ``verify_jsonl`` checks the result without the database."""
    count = 0
    with ChainedStore(path) as store, open(out, "w", encoding="utf-8") as fh:
        for entry in store.entries():
            line = {key: entry[key] for key in entry if key != "record_json"}
            line["type"] = "entry"
            line["record"] = parse_canonical_text(entry["record_json"])
            fh.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
            count += 1
        for cp in store.checkpoints():
            fh.write(json.dumps(dict(cp, type="checkpoint")) + "\n")
            count += 1
    return count


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    lines = []
    with open(path, "r", encoding="utf-8") as fh:
        for number, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                value = parse_canonical_text(raw)
            except ValueError as exc:
                raise EvidenceFormatError("%s line %d is not JSON: %s" % (path, number, exc)) from None
            if not isinstance(value, dict):
                raise EvidenceFormatError("%s line %d is not a JSON object" % (path, number))
            lines.append(value)
    return lines


def verify_jsonl(
    path: Union[str, Path],
    *,
    against: Optional[Dict[str, Any]] = None,
    expected_key_id: KeyPin = None,
) -> VerifyReport:
    """Verify a JSONL file of records with chain fields.

    Two layouts are recognised: the chained-store export (:func:`export_jsonl`:
    ``seq``/``prev_hash``/``entry_hash`` lines plus checkpoint lines), and a
    ``JsonlHashChainStore`` file (``prior_hash``/``payload_hash`` entries,
    verified per stream with :func:`briefcase.integrity.verify_chain_segment`;
    those files hold hashes only, so row payloads are not re-checked).
    """
    path = Path(path)
    lines = _read_jsonl(path)
    if not lines:
        raise EvidenceFormatError("%s is empty: there is nothing to verify" % path)
    if any("entry_hash" in line and "seq" in line for line in lines):
        return _verify_export_lines(path, lines, against, expected_key_id)
    if all("prior_hash" in line and "payload_hash" in line for line in lines):
        return _verify_hash_chain_lines(path, lines)
    raise EvidenceFormatError(
        "%s has no chain fields (seq/prev_hash/entry_hash, or prior_hash/payload_hash). Plain "
        "record files (briefcase.observe('runs.jsonl')) carry no hashes; capture into a chained "
        "store with briefcase.observe('chain:evidence.db') instead." % path
    )


def _verify_export_lines(
    path: Path,
    lines: List[Dict[str, Any]],
    against: Optional[Dict[str, Any]],
    expected_key_id: KeyPin,
) -> VerifyReport:
    report = VerifyReport(source=str(path), format="chain-jsonl")
    entries, checkpoints = [], []
    for line in lines:
        if line.get("type") == "checkpoint":
            checkpoints.append(line)
            continue
        entry = dict(line)
        try:
            entry["record_json"] = canonical_json_v2(line.get("record")).decode("utf-8")
        except (ValueError, TypeError):
            entry["record_json"] = "\x00unencodable"
        entries.append(entry)
    entries.sort(key=lambda e: int(e["seq"]))
    checkpoints.sort(key=lambda c: int(c["checkpoint_no"]))
    by_seq = _walk_entries(report, entries, None)
    report.records_checked = report.entries_checked
    _walk_checkpoints(report, checkpoints, by_seq, against, expected_key_id)
    return report.finish()


def _verify_hash_chain_lines(path: Path, lines: List[Dict[str, Any]]) -> VerifyReport:
    from briefcase.integrity.chain import verify_chain_segment
    from briefcase.integrity.stores import _entry_from_dict

    report = VerifyReport(source=str(path), format="hash-chain-jsonl")
    segments: Dict[Tuple[str, Optional[str]], list] = {}
    for line in lines:
        entry = _entry_from_dict(line)
        segments.setdefault((entry.table, entry.entity_id), []).append(entry)
        if entry.hash_scheme not in report.hash_schemes:
            report.hash_schemes.append(entry.hash_scheme)
    for (table, entity), entries in segments.items():
        ok, row_id = verify_chain_segment(entries)
        report.entries_checked += len(entries)
        if not ok:
            report.problem(
                "broken_link",
                None,
                "stream %s%s breaks at row %s (edited, reordered, inserted or removed entry)"
                % (table, "" if entity is None else "/" + entity, row_id),
            )
    report.notes.append(
        "Hash-chain JSONL files hold hashes, not the rows themselves: this checks the chain's "
        "own consistency, not the row payloads, and it cannot see entries removed from the end."
    )
    return report.finish()


def load_checkpoint_file(path: Union[str, Path]) -> Dict[str, Any]:
    """A checkpoint saved with ``briefcase verify --save-checkpoint``."""
    data = json.loads(Path(path).read_text("utf-8"))
    if isinstance(data, dict) and "checkpoint" in data and isinstance(data["checkpoint"], dict):
        data = data["checkpoint"]
    if not isinstance(data, dict) or "checkpoint_hash" not in data:
        raise EvidenceFormatError("%s does not hold a checkpoint" % path)
    return data


__all__ = [
    "ChainedStore",
    "KeyPin",
    "allowed_keys",
    "EvidenceFormatError",
    "GENESIS_HASH",
    "Problem",
    "VerifyReport",
    "check_checkpoint",
    "checkpoint_hash",
    "compute_entry_hash",
    "export_jsonl",
    "is_sqlite",
    "load_checkpoint_file",
    "record_hash",
    "verify_jsonl",
    "verify_store",
]
