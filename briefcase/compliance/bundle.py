"""The evidence bundle: one versioned format, one verifier.

A bundle is a ZIP file an auditor can check with only the SDK installed.
It holds one or many records; a sealed routing decision (what
:class:`~briefcase.compliance.ExaminerBundle` produces) is a bundle whose
records are the decision, its policy and its evidence.

Format ``briefcase-bundle``, ``format_version`` 2, hash scheme ``jcs-1``::

    manifest.json     format, format_version, hash_scheme, subject, context,
                      counts, files {name: sha256}, keys, content_hash,
                      signatures [...]
    records.jsonl     {"role", "record", "entry"?, "proof"?} per line
    checkpoints.json  chain checkpoints the proofs point at ([] if none)
    keys.json         Ed25519 public keys (RFC 8037 JWK) by key id
    README.txt        what the bundle proves and does not prove

``content_hash`` is ``"sha256:" + hex(sha256(jcs(manifest without
content_hash and signatures)))``; since the manifest lists the SHA-256 of
every other file, it covers the whole bundle. Each entry of
``signatures`` signs the 32 raw bytes of that digest; its ``type``
(``ed25519``, ``aws-kms``, or one registered with
:func:`briefcase.compliance.signers.register_verifier`) selects the
verifier.

``format_version`` 1 is the JSON document ``ExaminerBundle.to_json()`` and
``SignedExaminerBundle.to_json()`` have always written (content hash over
the legacy canonical profile, optional KMS signature over the UTF-8 of
``content_hash``). :func:`verify_bundle` accepts both versions.

The full specification, written so another implementation can verify
bundles, is in ``docs/features/audit-bundle.md``.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from briefcase.compliance.signers import (
    KMS_DEFAULT_ALGORITHM,
    Pins,
    Signer,
    check_signature,
)
from briefcase.integrity import merkle
from briefcase.integrity.canonical import HASH_SCHEME_V2, canonical_json_compat, canonical_json_v2, parse_canonical_text
from briefcase.integrity.evidence import (
    EvidenceFormatError,
    VerifyReport,
    check_checkpoint,
    compute_entry_hash,
    sha256_hex,
)

FORMAT = "briefcase-bundle"
FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1
LEGACY_HASH_SCHEME = "examiner-compat-1"
FILES = ("records.jsonl", "checkpoints.json", "keys.json", "README.txt")
SUBJECTS = ("records", "examiner")


class BundleError(ValueError):
    """The bundle could not be built (bad source, empty selection, ...)."""


@dataclass
class BundleRecord:
    """One line of ``records.jsonl``."""

    record: Any
    role: str = "record"
    entry: Optional[Dict[str, Any]] = None
    proof: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": self.role, "record": self.record}
        if self.entry is not None:
            out["entry"] = self.entry
        if self.proof is not None:
            out["proof"] = self.proof
        return out


@dataclass
class WrittenBundle:
    path: str
    manifest: Dict[str, Any]
    records: int
    checkpoints: int
    signers: List[Dict[str, Any]] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("briefcase-ai")
    except Exception:
        return "unknown"


def manifest_digest(manifest: Dict[str, Any]) -> str:
    """Hex SHA-256 over the RFC 8785 form of the manifest minus its seal."""
    body = {k: v for k, v in manifest.items() if k not in ("content_hash", "signatures")}
    return sha256_hex(canonical_json_v2(body))


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _describe_signers(signatures: Sequence[Dict[str, Any]]) -> List[str]:
    out = []
    for sig in signatures:
        if sig.get("type") == "aws-kms":
            out.append("AWS KMS key %s (%s)" % (sig.get("key_id"), sig.get("algorithm")))
        elif sig.get("type") == "ed25519":
            out.append("Ed25519 key %s" % sig.get("key_id"))
        else:
            out.append("%s key %s" % (sig.get("type"), sig.get("key_id")))
    return out


def _verify_command(manifest: Dict[str, Any]) -> str:
    keys = list(manifest.get("keys") or [])
    kms = [s["key_id"] for s in manifest.get("signatures") or [] if s.get("type") == "aws-kms"]
    flags = "".join(" --key " + k for k in keys) + "".join(" --key kms:" + k for k in kms)
    return "    briefcase bundle verify THIS_FILE.zip%s%s" % (flags, " --kms" if kms else "")


def render_readme(manifest: Dict[str, Any], checkpoints: Sequence[Dict[str, Any]]) -> str:
    counts = manifest["counts"]
    signatures = manifest.get("signatures") or []
    subject = manifest.get("subject")
    signed_cps = [c for c in checkpoints if c.get("signature")]
    cp_keys = sorted({c["key_id"] for c in signed_cps})
    bundle_made = [c for c in checkpoints if c.get("origin") == "bundle"]
    lines = ["BRIEFCASE EVIDENCE BUNDLE (format %s, version %s)" % (FORMAT, FORMAT_VERSION),
             "=" * 60, ""]
    if subject == "examiner":
        ctx = manifest.get("context") or {}
        lines += [
            "Created %s. A sealed routing decision with the policy in effect as of %s and" % (
                manifest["created_at"], ctx.get("as_of_transaction_time")),
            "the evidence records it cited: %d record(s) in total." % counts["records"],
        ]
    else:
        source = manifest.get("source") or {}
        selection = ", ".join(
            "%s=%s" % (k, v) for k, v in (manifest.get("selection") or {}).items() if v
        ) or "every record in the source"
        lines += [
            "Created %s from %s (%s entries in its chain)." % (
                manifest["created_at"], source.get("name"), source.get("entries")),
            "Selection: %s." % selection,
            "Contents: %d record(s), %d checkpoint(s)." % (counts["records"], counts["checkpoints"]),
        ]
    lines += ["", "HOW TO CHECK IT", "", "    pip install briefcase-ai", _verify_command(manifest), "",
              "Ed25519 checks need no network. Compare each key id with the one the record keeper",
              "published through another channel. AWS KMS signatures are checked by calling KMS",
              "(--kms) with the key id you were given; without that they are reported unchecked.",
              "", "WHAT A PASSING CHECK PROVES", ""]
    lines += [
        "- No file in the bundle changed after it was made: manifest.json lists the SHA-256 of",
        "  every file and content_hash covers the manifest.",
    ]
    if signatures:
        lines += ["- The bundle was sealed by: " + "; ".join(_describe_signers(signatures)) + "."]
    else:
        lines += ["- NOTE: the bundle itself is not signed; anyone could have produced a consistent one."]
    if any(c for c in checkpoints):
        lines += [
            "- Each chained record is byte-for-byte the record written to the hash chain, and was in",
            "  the log when the checkpoint named in its proof was made (Merkle inclusion proof).",
        ]
        if signed_cps:
            lines += ["- Those checkpoints were signed by Ed25519 key(s) %s." % ", ".join(k[:16] for k in cp_keys)]
        else:
            lines += ["- NOTE: no checkpoint is signed."]
    if bundle_made:
        lines += [
            "- %d checkpoint(s) were made when this bundle was created (origin: bundle) because the" % len(
                bundle_made),
            "  store had not yet checkpointed the newest records; they attest what the bundle creator saw.",
        ]
    if (manifest.get("context") or {}).get("legacy_signatures"):
        lines += ["- It carries the KMS signature of the original format-1 bundle, over its examiner",
                  "  content hash, which the verifier recomputes from the records here."]
    lines += [
        "", "WHAT IT DOES NOT PROVE", "",
        "- That the log or the evidence is complete: records outside the selection, or evidence a",
        "  decision relied on but did not cite, are not here. A log could have been cut or rebuilt",
        "  before a checkpoint was made unless that checkpoint was stored where the record keeper",
        "  cannot change it (a WORM bucket, an email to the auditor, a ticket).",
        "- That the records are true; only that they did not change after they were sealed.",
        "- Anything about content that was redacted or hashed at capture time.",
        "- Who controls the signing key. A key holder could have signed a rewritten history.",
        "", "FILES", "",
        "manifest.json     format, version, hash scheme, context, counts, file hashes, content_hash, signatures",
        "records.jsonl     {role, record, entry?, proof?} per line (roles: record, decision, policy, evidence)",
        "checkpoints.json  chain checkpoints the proofs refer to, with signatures",
        "keys.json         Ed25519 public keys (RFC 8037 JWK) by key id (RFC 7638 thumbprint)",
        "",
        "The format is specified in docs/features/audit-bundle.md of the briefcase-ai SDK.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_bundle(
    out: Union[str, Path],
    records: Sequence[BundleRecord],
    *,
    subject: str = "records",
    checkpoints: Sequence[Dict[str, Any]] = (),
    keys: Optional[Dict[str, Dict[str, str]]] = None,
    context: Optional[Dict[str, Any]] = None,
    source: Optional[Dict[str, Any]] = None,
    selection: Optional[Dict[str, Any]] = None,
    signers: Iterable[Signer] = (),
    sdk_version: Optional[str] = None,
) -> WrittenBundle:
    """Write a format-2 bundle to ``out`` (a new file; never overwritten)."""
    out = Path(out)
    if out.exists():
        raise BundleError("%s already exists; choose another --out or remove it first" % out)
    if subject not in SUBJECTS:
        raise BundleError("unknown bundle subject %r" % subject)
    if not records:
        raise BundleError("a bundle needs at least one record")
    signers = list(signers)
    key_map = dict(keys or {})
    for signer in signers:
        jwk = getattr(signer, "public_jwk", None)
        if jwk:
            key_map.setdefault(getattr(signer, "key_id"), jwk)
    lines = [json.dumps(r.to_dict(), ensure_ascii=False, allow_nan=False) for r in records]
    files: Dict[str, bytes] = {
        "records.jsonl": ("\n".join(lines) + "\n").encode("utf-8"),
        "checkpoints.json": (json.dumps(list(checkpoints), indent=2) + "\n").encode("utf-8"),
        "keys.json": (json.dumps(key_map, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    manifest: Dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "hash_scheme": HASH_SCHEME_V2,
        "subject": subject,
        "created_at": _now(),
        "created_by": "briefcase-ai %s" % (sdk_version or _sdk_version()),
        "source": source,
        "selection": selection,
        "context": dict(context or {}),
        "counts": {"records": len(records), "checkpoints": len(checkpoints)},
        "keys": sorted(key_map),
        # Signer identities are part of the sealed content, so a signature
        # cannot be stripped and replaced by another key's unnoticed.
        "signers": [
            {"type": s.type, "key_id": getattr(s, "key_id", None), "algorithm": getattr(s, "algorithm", None)}
            for s in signers
        ],
    }
    if any(s.type == "ed25519" for s in signers):
        for entry in manifest["signers"]:
            if entry["type"] == "ed25519":
                entry["algorithm"] = "Ed25519"
    preview = dict(manifest, signatures=[{"type": s["type"], "key_id": s["key_id"], "algorithm": s["algorithm"]}
                                         for s in manifest["signers"]])
    files["README.txt"] = render_readme(preview, list(checkpoints)).encode("utf-8")
    manifest["files"] = {name: sha256_hex(data) for name, data in sorted(files.items())}
    digest = manifest_digest(manifest)
    manifest["content_hash"] = "sha256:" + digest
    manifest["signatures"] = [signer.sign(bytes.fromhex(digest)) for signer in signers]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for name in FILES:
            zf.writestr(name, files[name])
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "xb") as fh:
        fh.write(buffer.getvalue())
    return WrittenBundle(str(out), manifest, len(records), len(checkpoints),
                         [{"type": s.get("type"), "key_id": s.get("key_id")} for s in manifest["signatures"]])


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def is_bundle(path: Union[str, Path]) -> bool:
    """A format-2 ZIP or a format-1 (examiner JSON) bundle."""
    return _zip_manifest(path) is not None or load_legacy(path) is not None


def _zip_manifest(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as zf:
            if "manifest.json" not in zf.namelist():
                return None
            return json.loads(zf.read("manifest.json").decode("utf-8"))
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeDecodeError):
        return None


def load_legacy(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """The format-1 document (``ExaminerBundle.to_json()``), or ``None``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1)
            if head not in (b"{", b" ", b"\n", b"\r", b"\t"):
                return None
            data = json.loads((head + fh.read()).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and "decision" in data and "content_hash" in data:
        return data
    return None


def examiner_content_hash(payload: Dict[str, Any]) -> str:
    """The format-1 content hash: ``ExaminerBundle._compute_hash`` exactly."""
    body = {
        "schema_version": payload.get("schema_version", "1"),
        "decision": payload.get("decision"),
        "policy": payload.get("policy"),
        "evidence": payload.get("evidence") or [],
        "as_of_transaction_time": payload.get("as_of_transaction_time"),
    }
    return "sha256:" + hashlib.sha256(canonical_json_compat(body).encode()).hexdigest()


def legacy_signature_record(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A format-1 KMS signature as a signature record (covers UTF-8 of content_hash)."""
    if not payload.get("signature"):
        return None
    return {
        "type": "aws-kms",
        "key_id": payload.get("kms_key_id", ""),
        "algorithm": payload.get("signing_algorithm", KMS_DEFAULT_ALGORITHM),
        "value": payload.get("signature"),
        "signed_at": payload.get("signed_at"),
        "covers": "examiner_content_hash",
    }


def inspect_bundle(path: Union[str, Path]) -> Dict[str, Any]:
    """Describe a bundle without verifying it."""
    manifest = _zip_manifest(path)
    if manifest is not None:
        roles: Dict[str, int] = {}
        with zipfile.ZipFile(path) as zf:
            names = sorted(zf.namelist())
            for raw in zf.read("records.jsonl").decode("utf-8").splitlines() if "records.jsonl" in names else []:
                if raw.strip():
                    role = json.loads(raw).get("role", "record")
                    roles[role] = roles.get(role, 0) + 1
        return {
            "path": str(path),
            "format": manifest.get("format"),
            "format_version": manifest.get("format_version"),
            "hash_scheme": manifest.get("hash_scheme"),
            "subject": manifest.get("subject"),
            "created_at": manifest.get("created_at"),
            "created_by": manifest.get("created_by"),
            "counts": manifest.get("counts"),
            "roles": roles,
            "context": manifest.get("context"),
            "selection": manifest.get("selection"),
            "content_hash": manifest.get("content_hash"),
            "signatures": [
                {k: s.get(k) for k in ("type", "key_id", "algorithm", "signed_at")}
                for s in manifest.get("signatures") or []
            ],
            "files": names,
        }
    legacy = load_legacy(path)
    if legacy is None:
        raise EvidenceFormatError("%s is not a bundle (neither a bundle ZIP nor an examiner bundle JSON)" % path)
    sig = legacy_signature_record(legacy)
    return {
        "path": str(path),
        "format": FORMAT,
        "format_version": LEGACY_FORMAT_VERSION,
        "hash_scheme": LEGACY_HASH_SCHEME,
        "subject": "examiner",
        "created_at": None,
        "counts": {"records": 2 + len(legacy.get("evidence") or []) - (legacy.get("policy") is None),
                   "checkpoints": 0},
        "roles": {"decision": 1, "policy": int(legacy.get("policy") is not None),
                  "evidence": len(legacy.get("evidence") or [])},
        "context": {"as_of_transaction_time": legacy.get("as_of_transaction_time"),
                    "metadata": legacy.get("metadata")},
        "content_hash": legacy.get("content_hash"),
        "signatures": [{k: sig.get(k) for k in ("type", "key_id", "algorithm", "signed_at")}] if sig else [],
        "files": [Path(path).name],
    }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _pins(expected_key_ids, kms_client, boto_session, kms_algorithms) -> Pins:
    ids: List[str] = []
    if expected_key_ids:
        for item in [expected_key_ids] if isinstance(expected_key_ids, str) else expected_key_ids:
            ids.append(item[4:] if item.startswith("kms:") else item)
    return Pins(key_ids=tuple(ids), kms_algorithms=tuple(kms_algorithms or (KMS_DEFAULT_ALGORITHM,)),
                kms_client=kms_client, boto_session=boto_session)


def _record_signature(report: VerifyReport, sig: Dict[str, Any], message: bytes, pins: Pins, label: str) -> None:
    check = check_signature(sig, message, pins)
    report.signers.append({"type": check.type, "key_id": check.key_id, "status": check.status,
                           "covers": label, "detail": check.detail})
    if check.key_id and check.key_id not in report.key_ids:
        report.key_ids.append(check.key_id)
    if check.status == "valid":
        report.signatures_verified += 1
    elif check.status == "unchecked":
        report.signatures_unchecked += 1
        report.notes.append(check.detail)
    elif check.status == "untrusted":
        report.notes.append("Signature on the %s: %s" % (label, check.detail))
    else:
        report.problem("bundle_signature_invalid", None, "%s signature: %s" % (label, check.detail))


def verify_bundle(
    path: Union[str, Path],
    *,
    expected_key_ids: Union[None, str, Sequence[str]] = None,
    expected_key_id: Union[None, str, Sequence[str]] = None,
    kms_client: Any = None,
    boto_session: Any = None,
    kms_algorithms: Optional[Sequence[str]] = None,
) -> VerifyReport:
    """Verify a bundle of any supported version; never trusts the bundle's own keys.

    ``expected_key_ids`` pins signers (Ed25519 key ids, KMS key ids; a
    ``kms:`` prefix is accepted). AWS KMS signatures are checked only when
    ``kms_client`` or ``boto_session`` is given, which calls AWS.
    """
    path = Path(path)
    pins = _pins(expected_key_ids or expected_key_id, kms_client, boto_session, kms_algorithms)
    manifest = _zip_manifest(path)
    if manifest is not None:
        fmt, version = manifest.get("format"), manifest.get("format_version")
        if fmt == "briefcase-audit-bundle":
            raise EvidenceFormatError(
                "%s is a pre-release audit bundle (never shipped in a release); re-create it with "
                "`briefcase bundle create`" % path
            )
        if fmt != FORMAT:
            raise EvidenceFormatError("%s: unknown bundle format %r" % (path, fmt))
        if version != FORMAT_VERSION:
            raise EvidenceFormatError(
                "%s: bundle format version %s is newer than this SDK understands (%d); upgrade briefcase-ai"
                % (path, version, FORMAT_VERSION)
            )
        if manifest.get("hash_scheme") != HASH_SCHEME_V2:
            raise EvidenceFormatError("%s: unknown hash scheme %r" % (path, manifest.get("hash_scheme")))
        return _verify_v2(path, manifest, pins)
    legacy = load_legacy(path)
    if legacy is not None:
        return _verify_v1(path, legacy, pins)
    raise EvidenceFormatError("%s is not a bundle (neither a bundle ZIP nor an examiner bundle JSON)" % path)


def _finish(report: VerifyReport, pins: Pins) -> VerifyReport:
    if pins.key_ids and report.signatures_verified == 0 and report.signatures_unchecked == 0 and not any(
        p.kind in ("unexpected_key", "bundle_signature_invalid") for p in report.problems
    ):
        untrusted = [s["key_id"] for s in report.signers if s["status"] == "untrusted"]
        if untrusted:
            report.problem("unexpected_key", None, "signed only by key(s) you did not pin: %s"
                           % ", ".join(str(k) for k in untrusted))
        else:
            report.problem("no_trusted_signature", None,
                           "no signature by an expected key was found (the bundle is unsigned)")
    if not pins.key_ids and report.key_ids:
        report.notes.append(
            "The bundle names its own signing key(s). Confirm that %s is what the record keeper "
            "published, or re-run with --key to pin it." % ", ".join(report.key_ids)
        )
    report.notes.append(
        "A bundle proves its records are unaltered since they were sealed (and, for chained records, "
        "that they were in the log at checkpoint time). It does not prove the log or the evidence is "
        "complete unless a checkpoint was anchored outside the record keeper's control."
    )
    return report.finish()


def _verify_v1(path: Path, payload: Dict[str, Any], pins: Pins) -> VerifyReport:
    report = VerifyReport(source=str(path), format="bundle", format_version=LEGACY_FORMAT_VERSION)
    report.hash_schemes.append(LEGACY_HASH_SCHEME)
    report.records_checked = 1 + int(payload.get("policy") is not None) + len(payload.get("evidence") or [])
    if examiner_content_hash(payload) != payload.get("content_hash"):
        report.problem("content_modified", None,
                       "the decision, policy or evidence was changed after the bundle was sealed")
    sig = legacy_signature_record(payload)
    if sig is not None:
        _record_signature(report, sig, str(payload.get("content_hash", "")).encode("utf-8"), pins, "bundle")
    else:
        report.notes.append(
            "Format-1 bundle without a signature: its content hash is unkeyed, so it proves only that "
            "the file is internally consistent."
        )
    return _finish(report, pins)


def _verify_v2(path: Path, manifest: Dict[str, Any], pins: Pins) -> VerifyReport:
    report = VerifyReport(source=str(path), format="bundle", format_version=FORMAT_VERSION)
    report.hash_schemes.append(HASH_SCHEME_V2)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
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

    digest = manifest_digest(manifest)
    if manifest.get("content_hash") != "sha256:" + digest:
        report.problem("manifest_modified", None, "manifest.json was changed after the bundle was made")
    signatures = manifest.get("signatures") or []
    declared = [(s.get("type"), s.get("key_id")) for s in manifest.get("signers") or []]
    actual = [(s.get("type"), s.get("key_id")) for s in signatures]
    if declared != actual:
        report.problem("bundle_signature_invalid", None,
                       "the signatures do not match the signers sealed in the manifest (added, removed or swapped)")
    for sig in signatures:
        _record_signature(report, sig, bytes.fromhex(digest), pins, "bundle")

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
        before = report.signatures_verified
        check_checkpoint(report, cp, None, list(pins.key_ids) or None)
        if cp.get("signature") and report.signatures_verified > before:
            report.signers.append({"type": "ed25519", "key_id": cp.get("key_id"), "status": "valid",
                                   "covers": "checkpoint %s" % cp.get("checkpoint_no"), "detail": ""})
        kid = cp.get("key_id")
        if kid and (kid not in key_map or key_map[kid].get("x") != cp.get("public_key")):
            report.problem("checkpoint_key_mismatch", None,
                           "checkpoint %s's key is not the one keys.json lists for %s"
                           % (cp["checkpoint_no"], str(kid)[:12]))
    for no, cp in by_no.items():
        nxt = by_no.get(no + 1)
        if nxt and (nxt.get("prev_checkpoint_hash") != cp.get("checkpoint_hash")
                    or int(nxt["from_seq"]) != int(cp["seq"]) + 1):
            report.problem("checkpoint_chain_broken", None,
                           "checkpoint %d does not follow checkpoint %d" % (no + 1, no))
    if checkpoints:
        report.last_checkpoint_seq = max(int(c["seq"]) for c in checkpoints)

    by_role: Dict[str, List[Any]] = {}
    for number, raw in enumerate(contents.get("records.jsonl", b"").decode("utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = parse_canonical_text(raw)
            record = line["record"]
            role = str(line.get("role", "record"))
        except (ValueError, KeyError, TypeError):
            report.problem("malformed_record", None, "records.jsonl line %d is not a bundle record" % number)
            continue
        report.records_checked += 1
        by_role.setdefault(role, []).append(record)
        entry = line.get("entry")
        if entry is None:
            continue
        _check_chained_line(report, entry, record, line.get("proof") or {}, by_no)
    counts = manifest.get("counts") or {}
    if counts.get("records") is not None and int(counts["records"]) != report.records_checked:
        report.problem("record_count_mismatch", None, "the manifest lists %s record(s) but the bundle holds %d"
                       % (counts["records"], report.records_checked))
    if report.records_checked == 0:
        report.problem("empty_bundle", None, "the bundle holds no records")

    context = manifest.get("context") or {}
    for legacy in context.get("legacy_signatures") or []:
        payload = {
            "schema_version": context.get("examiner_schema_version", "1"),
            "decision": (by_role.get("decision") or [None])[0],
            "policy": (by_role.get("policy") or [None])[0],
            "evidence": by_role.get("evidence") or [],
            "as_of_transaction_time": context.get("as_of_transaction_time"),
        }
        recomputed = examiner_content_hash(payload)
        if recomputed != context.get("examiner_content_hash"):
            report.problem("content_modified", None,
                           "the records no longer match the examiner content hash the original signature covers")
            continue
        _record_signature(report, legacy, recomputed.encode("utf-8"), pins, "original format-1 content")
    if report.signed_checkpoints == 0 and checkpoints:
        report.notes.append("No checkpoint in the bundle is signed.")
    return _finish(report, pins)


def _check_chained_line(report: VerifyReport, entry: Dict[str, Any], record: Any, proof: Dict[str, Any],
                        by_no: Dict[int, Dict[str, Any]]) -> None:
    try:
        seq = int(entry["seq"])
    except (KeyError, TypeError, ValueError):
        report.problem("malformed_record", None, "a chain entry has no seq")
        return
    report.entries_checked += 1
    report.first_seq = seq if report.first_seq is None else min(report.first_seq, seq)
    report.last_seq = seq if report.last_seq is None else max(report.last_seq, seq)
    try:
        ok = sha256_hex(canonical_json_v2(record)) == entry["record_hash"] and compute_entry_hash(
            entry["hash_scheme"], seq, entry["record_id"], entry["kind"], entry["prev_hash"],
            entry["record_hash"], entry["recorded_at"],
        ) == entry["entry_hash"]
    except (ValueError, TypeError, KeyError):
        ok = False
    if not ok:
        report.problem("modified_entry", seq, "record %s (entry %d) does not match its chain entry: it was changed"
                       % (entry.get("record_id"), seq))
        return
    cp = by_no.get(int(proof.get("checkpoint_no", -1)))
    if cp is None:
        report.problem("proof_invalid", seq, "entry %d points at a checkpoint the bundle does not hold" % seq)
        return
    start, end = int(cp["from_seq"]), int(cp["seq"])
    try:
        fits = (
            start <= seq <= end
            and int(proof["leaf_index"]) == seq - start
            and int(proof["tree_size"]) == end - start + 1
            and merkle.verify_inclusion(
                bytes.fromhex(entry["entry_hash"]), seq - start, end - start + 1,
                [bytes.fromhex(h) for h in proof["audit_path"]], bytes.fromhex(cp["merkle_root"]),
            )
        )
    except (ValueError, KeyError, TypeError):
        fits = False
    if not fits:
        report.problem("proof_invalid", seq, "entry %d is not proven to be in checkpoint %s (its Merkle proof does "
                       "not lead to the checkpoint root)" % (seq, cp["checkpoint_no"]))


def legacy_to_records(payload: Dict[str, Any]) -> List[BundleRecord]:
    records = [BundleRecord(payload["decision"], role="decision")]
    if payload.get("policy") is not None:
        records.append(BundleRecord(payload["policy"], role="policy"))
    records += [BundleRecord(ev, role="evidence") for ev in payload.get("evidence") or []]
    return records


def examiner_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    context: Dict[str, Any] = {
        "as_of_transaction_time": payload.get("as_of_transaction_time"),
        "examiner_schema_version": payload.get("schema_version", "1"),
        "examiner_content_hash": payload.get("content_hash"),
        "metadata": payload.get("metadata") or {},
    }
    sig = legacy_signature_record(payload)
    if sig is not None:
        context["legacy_signatures"] = [sig]
    return context


def migrate_legacy(
    path: Union[str, Path], out: Union[str, Path], *, signers: Iterable[Signer] = ()
) -> WrittenBundle:
    """Re-package a format-1 (examiner JSON) bundle as format 2.

    The original KMS signature travels along under
    ``context.legacy_signatures`` and is still checked (over the examiner
    content hash, recomputed from the records), so no KMS access is needed
    to migrate. A source whose content hash does not match is refused.
    """
    payload = load_legacy(path)
    if payload is None:
        raise BundleError("%s is not an examiner bundle JSON" % path)
    if examiner_content_hash(payload) != payload.get("content_hash"):
        raise BundleError("%s is not intact (its content hash does not match); refusing to re-package it" % path)
    return write_bundle(out, legacy_to_records(payload), subject="examiner", context=examiner_context(payload),
                        source={"name": Path(path).name, "format_version": LEGACY_FORMAT_VERSION},
                        signers=signers)


__all__ = [
    "BundleError",
    "BundleRecord",
    "FORMAT",
    "FORMAT_VERSION",
    "LEGACY_FORMAT_VERSION",
    "LEGACY_HASH_SCHEME",
    "WrittenBundle",
    "examiner_content_hash",
    "inspect_bundle",
    "is_bundle",
    "load_legacy",
    "manifest_digest",
    "migrate_legacy",
    "render_readme",
    "verify_bundle",
    "write_bundle",
]
