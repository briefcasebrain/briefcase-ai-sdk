"""Self-verifying audit bundles cut from a chained evidence store.

A bundle is one ZIP file an auditor can check with only the SDK installed,
without the original database:

``manifest.json``
    Format, schema version, what was selected, counts, the SHA-256 of every
    other file, and ``content_hash`` (``sha256:`` over the RFC 8785 encoding
    of the manifest without ``content_hash``/``signature``), optionally
    signed with Ed25519 (``--sign-key``).
``records.jsonl``
    One line per selected record: the record exactly as stored (redaction
    applied at capture time stays applied; nothing new is captured), its
    chain entry (``seq``, ``prev_hash``, ``record_hash``, ``entry_hash``, ...)
    and a Merkle inclusion proof against a checkpoint.
``checkpoints.json``
    The checkpoints the proofs point at, with their Ed25519 signatures.
``keys.json``
    The public keys (JWK) named by those signatures, by key id.
``README.txt``
    What the bundle proves and what it does not, in plain language.

The conventions follow :class:`~briefcase.compliance.ExaminerBundle` and
:class:`~briefcase.compliance.SignedExaminerBundle`: a ``schema_version``, a
``sha256:``-prefixed ``content_hash`` over a canonical payload, signature
metadata next to it, and a verifier that pins the expected key instead of
trusting the key the bundle carries.

Records not yet covered by a checkpoint in the store get a bundle
checkpoint that extends the store's checkpoint chain (``origin: bundle``),
signed with ``--sign-key`` when given. It attests what the bundle creator
saw; it is not the store's own checkpoint.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from briefcase.integrity import keys, merkle
from briefcase.integrity.canonical import HASH_SCHEME_V2, canonical_json_v2, parse_canonical_text
from briefcase.integrity.evidence import (
    GENESIS_HASH,
    ChainedStore,
    EvidenceFormatError,
    KeyPin,
    VerifyReport,
    allowed_keys,
    check_checkpoint,
    checkpoint_hash,
    compute_entry_hash,
    is_sqlite,
    sha256_hex,
    verify_jsonl,
    verify_store,
)

BUNDLE_FORMAT = "briefcase-audit-bundle"
SCHEMA_VERSION = "1"
FILES = ("records.jsonl", "checkpoints.json", "keys.json", "README.txt")
SELECTOR_FIELDS = {"session": "session_id", "workflow": "workflow_id", "trace": "trace_id"}


class BundleError(ValueError):
    """The bundle could not be built (bad source, empty selection, ...)."""


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
    seed: Optional[bytes],
) -> Dict[str, Any]:
    start = int(previous["seq"]) + 1 if previous else 1
    covered = [e for e in entries if int(e["seq"]) >= start]
    leaves = [bytes.fromhex(e["entry_hash"]) for e in covered]
    jwk = keys.public_jwk(seed) if seed else None
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
    if seed:
        cp["signature"] = keys.sign_digest(seed, cp["checkpoint_hash"])
    return cp


def _readme(manifest: Dict[str, Any], checkpoints: List[Dict[str, Any]]) -> str:
    counts = manifest["counts"]
    signed = [c for c in checkpoints if c.get("signature")]
    key_ids = sorted({c["key_id"] for c in signed})
    bundle_made = [c for c in checkpoints if c.get("origin") == "bundle"]
    selection = ", ".join(
        "%s=%s" % (k, v) for k, v in manifest["selection"].items() if v
    ) or "every record in the source"
    lines = [
        "BRIEFCASE AUDIT BUNDLE",
        "======================",
        "",
        "Created %s from %s (%d entries in its chain)." % (
            manifest["created_at"], manifest["source"]["name"], manifest["source"]["entries"]),
        "Selection: %s." % selection,
        "Contents: %d record(s), %d checkpoint(s), %d public key(s)." % (
            counts["records"], counts["checkpoints"], len(manifest["keys"])),
        "",
        "HOW TO CHECK IT",
        "",
        "    pip install briefcase-ai",
        "    briefcase bundle verify THIS_FILE.zip%s" % "".join(
            " --key " + k for k in manifest["keys"]),
        "",
        "It needs no network and no access to the original database. Compare the key id it",
        "prints with the key id the record keeper published through another channel.",
        "",
        "WHAT A PASSING CHECK PROVES",
        "",
        "- Each record in records.jsonl is byte-for-byte the record that was written to the",
        "  hash chain (its hash, and the chain entry's hash over it, recompute exactly).",
        "- Each record was in the log when the checkpoint named in its proof was made: the",
        "  Merkle proof links its chain entry to that checkpoint's root.",
    ]
    if signed:
        lines += [
            "- Those checkpoints were signed by the Ed25519 key(s) %s, so they were not" % (
                ", ".join(k[:16] for k in key_ids)),
            "  altered after signing, and only a holder of that key could have made them.",
        ]
    else:
        lines += [
            "- NOTE: no checkpoint in this bundle is signed. The bundle is internally",
            "  consistent, but anyone could have produced a consistent one.",
        ]
    if manifest.get("signature"):
        lines += ["- The bundle as a whole (every file) is signed by key %s." % (
            manifest["signature"]["key_id"][:16])]
    if bundle_made:
        lines += [
            "",
            "%d checkpoint(s) were made when this bundle was created (origin: bundle), because" % (
                len(bundle_made)),
            "the store had not yet checkpointed the newest records. They attest what the bundle",
            "creator saw at that time, signed with the bundle key if one was given.",
        ]
    lines += [
        "",
        "WHAT IT DOES NOT PROVE",
        "",
        "- That the log is complete. Records outside the selection are not included, and the",
        "  log could have been cut or rebuilt before the checkpoint was made unless that",
        "  checkpoint was also stored somewhere the record keeper cannot change (an external",
        "  anchor: a WORM bucket, an email to the auditor, a ticket).",
        "- That the records are true. It proves they were not changed after they were",
        "  recorded, not that what was recorded was correct.",
        "- Anything about content that was redacted or hashed at capture time.",
        "- Who holds the signing key. A record keeper holding the key could have signed a",
        "  rewritten log before any checkpoint was anchored externally.",
        "",
        "FILES",
        "",
        "manifest.json     format, selection, counts, SHA-256 of each file, content_hash, signature",
        "records.jsonl     {entry, record, proof} per record (hash scheme %s, RFC 8785)" % HASH_SCHEME_V2,
        "checkpoints.json  checkpoints the proofs refer to, with signatures",
        "keys.json         public keys (RFC 8037 JWK) by key id (RFC 7638 thumbprint)",
        "",
        "Manual check, per record: record_hash = sha256(RFC 8785 JSON of record); entry_hash =",
        "sha256(RFC 8785 JSON of {v:1, hash_scheme, seq, record_id, kind, prev_hash,",
        "record_hash, recorded_at}); fold the audit_path with the RFC 6962 Merkle rules to the",
        "checkpoint's merkle_root; checkpoint_hash = sha256(RFC 8785 JSON of the checkpoint's",
        "v:1, hash_scheme, checkpoint_no, from_seq, seq, head_hash, merkle_root,",
        "prev_checkpoint_hash, created_at, key_id); the Ed25519 signature covers the 32 raw",
        "bytes of checkpoint_hash.",
        "",
    ]
    return "\n".join(lines)


def _manifest_hash(manifest: Dict[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k not in ("content_hash", "signature")}
    return sha256_hex(canonical_json_v2(body))


def create_bundle(
    source: Union[str, Path],
    out: Union[str, Path],
    *,
    selection: Optional[Selection] = None,
    sign_key: Optional[Union[str, Path, bytes]] = None,
    sdk_version: Optional[str] = None,
) -> BundleSummary:
    """Write a bundle of the selected records to ``out`` (a new ZIP file).

    The source (a chained store or its JSONL export) is verified first; a
    source that is not intact is refused, since a bundle from a broken log
    would prove nothing.
    """
    source = Path(source)
    out = Path(out)
    selection = selection or Selection()
    if out.exists():
        raise BundleError("%s already exists; choose another --out or remove it first" % out)
    seed = keys.load_seed(sign_key) if sign_key is not None else None
    entries, checkpoints, report = _load_source(source)
    if not report.intact:
        first = report.problems[0].detail if report.problems else "unknown problem"
        raise BundleError(
            "%s is not intact (%d problem(s), first: %s). Run `briefcase verify %s` for details; "
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
        extra = _bundle_checkpoint(entries, previous, seed)
        extra["origin"] = "bundle"
        all_checkpoints.append(extra)
        bundle_checkpoints = 1

    hashes_by_seq = {int(e["seq"]): e["entry_hash"] for e in entries}
    used: Dict[int, Dict[str, Any]] = {}
    lines = []
    for entry, record in chosen:
        seq = int(entry["seq"])
        cp = next(c for c in all_checkpoints if int(c["from_seq"]) <= seq <= int(c["seq"]))
        start, end = int(cp["from_seq"]), int(cp["seq"])
        leaves = [bytes.fromhex(hashes_by_seq[s]) for s in range(start, end + 1)]
        path = merkle.inclusion_proof(leaves, seq - start)
        used[int(cp["checkpoint_no"])] = cp
        lines.append(
            json.dumps(
                {
                    "entry": {k: entry[k] for k in entry if k != "record_json"},
                    "record": record,
                    "proof": {
                        "checkpoint_no": int(cp["checkpoint_no"]),
                        "leaf_index": seq - start,
                        "tree_size": end - start + 1,
                        "audit_path": [h.hex() for h in path],
                    },
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    bundle_cps = [used[n] for n in sorted(used)]
    key_map = {
        c["key_id"]: {"kty": "OKP", "crv": "Ed25519", "x": c["public_key"]}
        for c in bundle_cps
        if c.get("key_id") and c.get("public_key")
    }
    if seed is not None:
        jwk = keys.public_jwk(seed)
        key_map.setdefault(keys.key_id(jwk["x"]), jwk)

    files: Dict[str, bytes] = {
        "records.jsonl": ("\n".join(lines) + "\n").encode("utf-8"),
        "checkpoints.json": (json.dumps(bundle_cps, indent=2) + "\n").encode("utf-8"),
        "keys.json": (json.dumps(key_map, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    manifest: Dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "created_by": "briefcase-ai %s" % (sdk_version or _sdk_version()),
        "hash_scheme": HASH_SCHEME_V2,
        "source": {
            "name": source.name,
            "entries": len(entries),
            "head_seq": last_seq,
            "head_hash": entries[-1]["entry_hash"],
        },
        "selection": selection.to_dict(),
        "counts": {"records": len(lines), "checkpoints": len(bundle_cps)},
        "keys": sorted(key_map),
        "metadata": {},
    }
    files["README.txt"] = _readme(manifest, bundle_cps).encode("utf-8")
    manifest["files"] = {name: sha256_hex(data) for name, data in sorted(files.items())}
    digest = _manifest_hash(manifest)
    manifest["content_hash"] = "sha256:" + digest
    signed = False
    if seed is not None:
        jwk = keys.public_jwk(seed)
        manifest["signature"] = {
            "algorithm": "Ed25519",
            "key_id": keys.key_id(jwk["x"]),
            "public_key": jwk["x"],
            "value": keys.sign_digest(seed, digest),
            "signed_at": _now(),
        }
        signed = True

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for name in FILES:
            zf.writestr(name, files[name])
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "xb") as fh:
        fh.write(buffer.getvalue())
    return BundleSummary(
        path=str(out),
        records=len(lines),
        checkpoints=len(bundle_cps),
        bundle_checkpoints=bundle_checkpoints if any(
            c.get("origin") == "bundle" for c in bundle_cps) else 0,
        key_ids=sorted({c["key_id"] for c in bundle_cps if c.get("key_id")}),
        signed=signed,
        content_hash=manifest["content_hash"],
        source_entries=len(entries),
        all_key_ids=sorted(key_map),
        selection=selection.to_dict(),
    )


def _sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("briefcase-ai")
    except Exception:
        return "unknown"


def is_bundle(path: Union[str, Path]) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return "manifest.json" in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def verify_bundle(path: Union[str, Path], *, expected_key_id: KeyPin = None) -> VerifyReport:
    """Check a bundle with nothing but its own contents.

    ``expected_key_id`` pins the signer (checkpoints and manifest); without
    it the report lists the key ids found so a person can compare them with
    the ones the record keeper published.
    """
    path = Path(path)
    report = VerifyReport(source=str(path), format="bundle")
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise EvidenceFormatError("%s is not a bundle (not a ZIP file: %s)" % (path, exc)) from None
    with zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise EvidenceFormatError("%s is a ZIP file without manifest.json, not a bundle" % path)
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except ValueError as exc:
            raise EvidenceFormatError("%s: manifest.json is not JSON (%s)" % (path, exc)) from None
        if manifest.get("format") != BUNDLE_FORMAT:
            raise EvidenceFormatError("%s: unknown bundle format %r" % (path, manifest.get("format")))
        if str(manifest.get("schema_version")) != SCHEMA_VERSION:
            raise EvidenceFormatError(
                "%s: bundle schema %s is newer than this SDK understands (%s); upgrade briefcase-ai"
                % (path, manifest.get("schema_version"), SCHEMA_VERSION)
            )
        listed = manifest.get("files") or {}
        contents: Dict[str, bytes] = {}
        for name in sorted(set(listed) | (names - {"manifest.json"})):
            if name not in listed:
                report.problem("unexpected_file", None, "%s is in the bundle but not in the manifest" % name)
                continue
            if name not in names:
                report.problem("missing_file", None, "%s is listed in the manifest but missing" % name)
                continue
            data = zf.read(name)
            contents[name] = data
            if sha256_hex(data) != listed[name]:
                report.problem("file_modified", None, "%s was changed after the bundle was made" % name)

    if manifest.get("content_hash") != "sha256:" + _manifest_hash(manifest):
        report.problem("manifest_modified", None, "manifest.json was changed after the bundle was made")
    signature = manifest.get("signature")
    if signature:
        kid, x = signature.get("key_id"), signature.get("public_key")
        if not x or keys.key_id(x) != kid:
            report.problem("bundle_signature_invalid", None, "the bundle signature names a different key")
        elif not keys.verify_digest(str(manifest.get("content_hash", ""))[7:], signature.get("value", ""), x):
            report.problem("bundle_signature_invalid", None, "the bundle signature does not verify")
        else:
            report.signatures_verified += 1
        if kid and kid not in report.key_ids:
            report.key_ids.append(kid)
        allowed = allowed_keys(expected_key_id)
        if allowed is not None and kid not in allowed:
            report.problem(
                "unexpected_key", None,
                "the bundle is signed by key %s, not the expected key %s"
                % (str(kid)[:12], " or ".join(k[:12] for k in sorted(allowed))),
            )

    try:
        checkpoints = json.loads(contents.get("checkpoints.json", b"[]").decode("utf-8"))
        key_map = json.loads(contents.get("keys.json", b"{}").decode("utf-8"))
    except ValueError:
        report.problem("file_modified", None, "checkpoints.json or keys.json is not valid JSON")
        checkpoints, key_map = [], {}
    by_no: Dict[int, Dict[str, Any]] = {}
    for cp in checkpoints:
        report.checkpoints_checked += 1
        by_no[int(cp["checkpoint_no"])] = cp
        check_checkpoint(report, cp, None, expected_key_id)
        kid = cp.get("key_id")
        if kid and (kid not in key_map or key_map[kid].get("x") != cp.get("public_key")):
            report.problem(
                "checkpoint_key_mismatch", None,
                "checkpoint %s's key is not the one keys.json lists for %s" % (cp["checkpoint_no"], str(kid)[:12]),
            )
    for no, cp in by_no.items():
        nxt = by_no.get(no + 1)
        if nxt and (nxt.get("prev_checkpoint_hash") != cp.get("checkpoint_hash")
                    or int(nxt["from_seq"]) != int(cp["seq"]) + 1):
            report.problem(
                "checkpoint_chain_broken", None,
                "checkpoint %d does not follow checkpoint %d" % (no + 1, no),
            )
    if checkpoints:
        report.last_checkpoint_seq = max(int(c["seq"]) for c in checkpoints)
    if report.signed_checkpoints == 0 and checkpoints:
        report.notes.append(
            "No checkpoint in the bundle is signed: it is internally consistent, but anyone could "
            "have produced a consistent bundle."
        )

    raw_lines = contents.get("records.jsonl", b"").decode("utf-8").splitlines()
    for number, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        try:
            line = parse_canonical_text(raw)
            entry, record, proof = line["entry"], line["record"], line["proof"]
            seq = int(entry["seq"])
        except (ValueError, KeyError, TypeError):
            report.problem("malformed_record", None, "records.jsonl line %d is not a bundle record" % number)
            continue
        report.records_checked += 1
        report.entries_checked += 1
        if report.first_seq is None or seq < report.first_seq:
            report.first_seq = seq
        if report.last_seq is None or seq > report.last_seq:
            report.last_seq = seq
        if entry.get("hash_scheme") not in report.hash_schemes:
            report.hash_schemes.append(entry.get("hash_scheme"))
        try:
            ok = sha256_hex(canonical_json_v2(record)) == entry["record_hash"] and compute_entry_hash(
                entry["hash_scheme"], seq, entry["record_id"], entry["kind"], entry["prev_hash"],
                entry["record_hash"], entry["recorded_at"],
            ) == entry["entry_hash"]
        except (ValueError, TypeError, KeyError):
            ok = False
        if not ok:
            report.problem(
                "modified_entry", seq,
                "record %s (entry %d) does not match its chain entry: it was changed" % (
                    entry.get("record_id"), seq),
            )
            continue
        cp = by_no.get(int(proof.get("checkpoint_no", -1)))
        if cp is None:
            report.problem("proof_invalid", seq, "entry %d points at a checkpoint the bundle does not hold" % seq)
            continue
        start, end = int(cp["from_seq"]), int(cp["seq"])
        try:
            path_hashes = [bytes.fromhex(h) for h in proof["audit_path"]]
            fits = (
                start <= seq <= end
                and int(proof["leaf_index"]) == seq - start
                and int(proof["tree_size"]) == end - start + 1
                and merkle.verify_inclusion(
                    bytes.fromhex(entry["entry_hash"]), seq - start, end - start + 1,
                    path_hashes, bytes.fromhex(cp["merkle_root"]),
                )
            )
        except (ValueError, KeyError, TypeError):
            fits = False
        if not fits:
            report.problem(
                "proof_invalid", seq,
                "entry %d is not proven to be in checkpoint %s (its Merkle proof does not lead to "
                "the checkpoint root)" % (seq, cp["checkpoint_no"]),
            )
    counts = manifest.get("counts") or {}
    if counts.get("records") is not None and int(counts["records"]) != report.records_checked:
        report.problem(
            "record_count_mismatch", None,
            "the manifest lists %s record(s) but the bundle holds %d" % (counts["records"], report.records_checked),
        )
    if report.records_checked == 0:
        report.problem("empty_bundle", None, "the bundle holds no records")
    if not expected_key_id and report.key_ids:
        report.notes.append(
            "The bundle carries its own public key(s). Confirm that key id %s is the one the "
            "record keeper published, or re-run with --key to pin it." % ", ".join(report.key_ids)
        )
    report.notes.append(
        "A bundle proves the included records were in the log when its checkpoint was made and "
        "are unaltered since. It does not prove the log is complete unless that checkpoint was "
        "anchored outside the record keeper's control."
    )
    return report.finish()


__all__ = [
    "BUNDLE_FORMAT",
    "BundleError",
    "BundleSummary",
    "SCHEMA_VERSION",
    "Selection",
    "create_bundle",
    "is_bundle",
    "parse_time",
    "verify_bundle",
]
