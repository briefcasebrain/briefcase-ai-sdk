"""Cut an evidence bundle from a chained store (``briefcase-ai bundle create --from evidence.db``).

The bundle format, its writer and its single verifier live in
:mod:`briefcase.compliance.bundle`; this module selects records from a
chained store (or its JSONL export), builds their Merkle inclusion proofs
against the store's checkpoints, and writes a format-2 bundle with subject
``records``. Records newer than the store's last checkpoint get a bundle
checkpoint (``origin: bundle``) that extends the store's checkpoint chain,
signed by the first Ed25519 signer when there is one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from briefcase.compliance.bundle import (
    BundleError,
    BundleRecord,
    FORMAT,
    FORMAT_VERSION,
    inspect_bundle,
    is_bundle,
    verify_bundle,
    write_bundle,
)
from briefcase.compliance.signers import Ed25519Signer, Signer
from briefcase.integrity import keys, merkle
from briefcase.integrity.canonical import HASH_SCHEME_V2, canonical_json_v2, parse_canonical_text
from briefcase.integrity.evidence import (
    GENESIS_HASH,
    ChainedStore,
    VerifyReport,
    checkpoint_hash,
    is_sqlite,
    verify_jsonl,
    verify_store,
)

SELECTOR_FIELDS = {"session": "session_id", "workflow": "workflow_id", "trace": "trace_id"}


@dataclass
class Selection:
    session: Optional[str] = None
    workflow: Optional[str] = None
    trace: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "session": self.session,
            "workflow": self.workflow,
            "trace": self.trace,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
        }

    def is_empty(self) -> bool:
        return not any(self.to_dict().values())


@dataclass
class BundleSummary:
    path: str
    records: int
    checkpoints: int
    bundle_checkpoints: int
    key_ids: List[str]
    signed: bool
    content_hash: str
    source_entries: int
    all_key_ids: List[str] = field(default_factory=list)
    signer_ids: List[str] = field(default_factory=list)
    selection: Dict[str, Optional[str]] = field(default_factory=dict)


def parse_time(text: str) -> datetime:
    """ISO 8601 date or datetime; naive values are UTC."""
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _field_values(record: Any, name: str) -> List[Any]:
    """Values of ``name`` at the places records carry correlation ids."""
    if not isinstance(record, dict):
        return []
    found = []
    for container in (record, record.get("tags"), record.get("attributes"), record.get("metadata")):
        if isinstance(container, dict) and name in container:
            found.append(container[name])
    for decision in record.get("decisions") or []:
        found.extend(_field_values(decision, name))
    return found


def _matches(entry: Dict[str, Any], record: Any, selection: Selection) -> bool:
    for attr, name in SELECTOR_FIELDS.items():
        wanted = getattr(selection, attr)
        if wanted is not None and wanted not in [str(v) for v in _field_values(record, name)]:
            return False
    if selection.since or selection.until:
        at = parse_time(entry["recorded_at"])
        if selection.since and at < selection.since:
            return False
        if selection.until and at > selection.until:
            return False
    return True


def _load_source(source: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], VerifyReport]:
    """Entries (with ``record_json``) and checkpoints, after verifying the source."""
    if is_sqlite(source):
        report = verify_store(source)
        with ChainedStore(source) as store:
            return store.entries(), store.checkpoints(), report
    report = verify_jsonl(source)
    if report.format != "chain-jsonl":
        raise BundleError(
            "%s is a hash-chain file without the records themselves; bundle from the chained "
            "store instead" % source
        )
    entries, checkpoints = [], []
    with open(source, "r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            line = parse_canonical_text(raw)
            if line.get("type") == "checkpoint":
                checkpoints.append({k: v for k, v in line.items() if k != "type"})
            else:
                entry = {k: v for k, v in line.items() if k not in ("type", "record")}
                entry["record_json"] = canonical_json_v2(line["record"]).decode("utf-8")
                entries.append(entry)
    entries.sort(key=lambda e: int(e["seq"]))
    checkpoints.sort(key=lambda c: int(c["checkpoint_no"]))
    return entries, checkpoints, report


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _bundle_checkpoint(
    entries: Sequence[Dict[str, Any]],
    previous: Optional[Dict[str, Any]],
    signer: Optional[Ed25519Signer],
) -> Dict[str, Any]:
    start = int(previous["seq"]) + 1 if previous else 1
    covered = [e for e in entries if int(e["seq"]) >= start]
    leaves = [bytes.fromhex(e["entry_hash"]) for e in covered]
    jwk = signer.public_jwk if signer else None
    cp: Dict[str, Any] = {
        "checkpoint_no": int(previous["checkpoint_no"]) + 1 if previous else 1,
        "from_seq": start,
        "seq": int(covered[-1]["seq"]),
        "head_hash": covered[-1]["entry_hash"],
        "merkle_root": merkle.merkle_root(leaves).hex(),
        "prev_checkpoint_hash": previous["checkpoint_hash"] if previous else GENESIS_HASH,
        "created_at": _now(),
        "hash_scheme": HASH_SCHEME_V2,
        "key_id": keys.key_id(jwk["x"]) if jwk else None,
        "public_key": jwk["x"] if jwk else None,
        "signature": None,
    }
    cp["checkpoint_hash"] = checkpoint_hash(cp)
    if signer:
        cp["signature"] = signer.sign(bytes.fromhex(cp["checkpoint_hash"]))["value"]
    return cp


def create_bundle(
    source: Union[str, Path],
    out: Union[str, Path],
    *,
    selection: Optional[Selection] = None,
    sign_key: Optional[Union[str, Path, bytes]] = None,
    signers: Sequence[Signer] = (),
    sdk_version: Optional[str] = None,
) -> BundleSummary:
    """Write a bundle of the selected records to ``out`` (a new ZIP file).

    The source (a chained store or its JSONL export) is verified first; a
    source that is not intact is refused, since a bundle from a broken log
    would prove nothing.

    ``signers`` seal the bundle (Ed25519, AWS KMS, or any
    :class:`~briefcase.compliance.signers.Signer`); ``sign_key`` is
    shorthand for one Ed25519 key file. The first Ed25519 signer also signs
    the bundle checkpoint made for records the store has not checkpointed.
    """
    source = Path(source)
    out = Path(out)
    selection = selection or Selection()
    if out.exists():
        raise BundleError("%s already exists; choose another --out or remove it first" % out)
    signers = list(signers)
    if sign_key is not None:
        signers.insert(0, Ed25519Signer(sign_key))
    ed_signer = next((s for s in signers if isinstance(s, Ed25519Signer)), None)
    entries, checkpoints, report = _load_source(source)
    if not report.intact:
        first = report.problems[0].detail if report.problems else "unknown problem"
        raise BundleError(
            "%s is not intact (%d problem(s), first: %s). Run `briefcase-ai verify %s` for details; "
            "a bundle from a broken log would prove nothing." % (source, len(report.problems), first, source)
        )
    if not entries:
        raise BundleError("%s has no chain entries yet" % source)

    chosen = []
    for entry in entries:
        record = parse_canonical_text(entry["record_json"])
        if _matches(entry, record, selection):
            chosen.append((entry, record))
    if not chosen:
        raise BundleError(
            "no record in %s matches the selection (%s)"
            % (source, ", ".join("%s=%s" % kv for kv in selection.to_dict().items() if kv[1]))
        )

    all_checkpoints = [dict(c, origin="store") for c in checkpoints]
    last_seq = int(entries[-1]["seq"])
    covered_to = int(checkpoints[-1]["seq"]) if checkpoints else 0
    bundle_checkpoints = 0
    if any(int(e["seq"]) > covered_to for e, _ in chosen) and covered_to < last_seq:
        previous = checkpoints[-1] if checkpoints else None
        extra = _bundle_checkpoint(entries, previous, ed_signer)
        extra["origin"] = "bundle"
        all_checkpoints.append(extra)
        bundle_checkpoints = 1

    hashes_by_seq = {int(e["seq"]): e["entry_hash"] for e in entries}
    used: Dict[int, Dict[str, Any]] = {}
    lines: List[BundleRecord] = []
    for entry, record in chosen:
        seq = int(entry["seq"])
        cp = next(c for c in all_checkpoints if int(c["from_seq"]) <= seq <= int(c["seq"]))
        start, end = int(cp["from_seq"]), int(cp["seq"])
        leaves = [bytes.fromhex(hashes_by_seq[s]) for s in range(start, end + 1)]
        path = merkle.inclusion_proof(leaves, seq - start)
        used[int(cp["checkpoint_no"])] = cp
        lines.append(
            BundleRecord(
                record=record,
                entry={k: entry[k] for k in entry if k != "record_json"},
                proof={
                    "checkpoint_no": int(cp["checkpoint_no"]),
                    "leaf_index": seq - start,
                    "tree_size": end - start + 1,
                    "audit_path": [h.hex() for h in path],
                },
            )
        )
    bundle_cps = [used[n] for n in sorted(used)]
    key_map = {
        c["key_id"]: {"kty": "OKP", "crv": "Ed25519", "x": c["public_key"]}
        for c in bundle_cps
        if c.get("key_id") and c.get("public_key")
    }
    written = write_bundle(
        out,
        lines,
        subject="records",
        checkpoints=bundle_cps,
        keys=key_map,
        source={
            "name": source.name,
            "entries": len(entries),
            "head_seq": last_seq,
            "head_hash": entries[-1]["entry_hash"],
        },
        selection=selection.to_dict(),
        signers=signers,
        sdk_version=sdk_version,
    )
    return BundleSummary(
        path=str(out),
        records=len(lines),
        checkpoints=len(bundle_cps),
        bundle_checkpoints=bundle_checkpoints if any(
            c.get("origin") == "bundle" for c in bundle_cps) else 0,
        key_ids=sorted({c["key_id"] for c in bundle_cps if c.get("key_id")}),
        signed=bool(signers),
        content_hash=written.manifest["content_hash"],
        source_entries=len(entries),
        all_key_ids=sorted(written.manifest["keys"]),
        signer_ids=[str(s.get("key_id")) for s in written.signers],
        selection=selection.to_dict(),
    )


__all__ = [
    "BundleError",
    "BundleSummary",
    "FORMAT",
    "FORMAT_VERSION",
    "Selection",
    "create_bundle",
    "inspect_bundle",
    "is_bundle",
    "parse_time",
    "verify_bundle",
]
