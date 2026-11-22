"""``briefcase verify PATH``: is this evidence intact? One answer, in plain language.

    briefcase verify evidence.db                         # a chained store
    briefcase verify evidence-bundle.zip --key KEYID     # an evidence bundle (any version)
    briefcase verify chain-export.jsonl --json           # records with chain fields

Exit codes: 0 intact, 1 tampered or incomplete, 2 usage (missing file,
unknown format). Reads only; never writes the evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

GROUP = "verify"
_RAW = argparse.RawDescriptionHelpFormatter
_KEY_ID = re.compile(r"^[0-9a-f]{64}$")

FORMAT_LABELS = {
    "chained-store": "chained store",
    "chain-jsonl": "chain export (JSONL)",
    "hash-chain-jsonl": "hash-chain file (JSONL)",
    "bundle": "evidence bundle",
}


def register(sub) -> None:
    if "verify" in sub.choices:
        return
    p = sub.add_parser(
        "verify",
        help="Check that recorded evidence is intact: a chained store, a chain JSONL, or a bundle.",
        description="Checks every hash, link, checkpoint signature and Merkle proof, then says in "
                    "plain language whether the evidence is intact, how much was checked, and what "
                    "exactly is wrong if not. Exit 0 intact, 1 tampered or incomplete, 2 usage.",
        epilog="example:\n  briefcase verify evidence.db",
        formatter_class=_RAW,
    )
    p.add_argument("path", metavar="PATH", help="Chained SQLite store, chain JSONL, or bundle .zip.")
    p.add_argument("--against", metavar="CHECKPOINT.json",
                   help="A checkpoint kept outside the store (from --save-checkpoint). The only way to "
                        "detect removal of the newest entries.")
    p.add_argument("--key", metavar="KEY", action="append",
                   help="Require this signer: a 64-hex Ed25519 key id, a public/private key file, or "
                        "kms:KEY_ID / an AWS KMS key ARN. Repeat to allow several keys.")
    add_kms_flags(p)
    p.add_argument("--save-checkpoint", metavar="FILE",
                   help="Also write the store's latest checkpoint to FILE (a new file) so it can be "
                        "kept elsewhere and used with --against later.")
    p.add_argument("--json", action="store_true", help="Machine-readable report.")
    p.set_defaults(func=cmd_verify)


_KMS_PREFIXES = ("kms:", "arn:aws:kms:", "arn:aws-us-gov:kms:", "arn:aws-cn:kms:", "alias/")


def add_kms_flags(p) -> None:
    p.add_argument("--kms", action="store_true",
                   help="Check AWS KMS signatures by calling KMS with your AWS credentials (needs boto3 "
                        "and a pinned --key kms:KEY_ID). Off by default: nothing is sent to AWS.")
    p.add_argument("--kms-algorithm", action="append", metavar="ALG",
                   help="Accepted KMS signing algorithm (default RSASSA_PSS_SHA_256; repeatable).")


def resolve_key_id(value: Union[None, str, Sequence[str]]) -> Optional[List[str]]:
    """Key ids from ``--key``: a 64-hex Ed25519 key id, a key file (public or
    private), or an AWS KMS key (``kms:ID``, a KMS key ARN, or ``alias/...``),
    returned as ``kms:ID``."""
    if not value:
        return None
    from briefcase.integrity import keys

    out = []
    for item in [value] if isinstance(value, str) else value:
        item = item.strip()
        if _KEY_ID.match(item.lower()):
            out.append(item.lower())
        elif item.startswith(_KMS_PREFIXES):
            out.append(item if item.startswith("kms:") else "kms:" + item)
        else:
            out.append(keys.key_id(keys.load_public_key(item)["x"]))
    return out


def kms_options(args) -> Dict[str, Any]:
    """``verify_bundle`` keyword arguments for ``--kms`` / ``--kms-algorithm``."""
    from briefcase.compliance import signers

    out: Dict[str, Any] = {}
    if getattr(args, "kms", False):
        out["kms_client"] = signers.kms_client_from(None)
    if getattr(args, "kms_algorithm", None):
        out["kms_algorithms"] = list(args.kms_algorithm)
    return out


def _usage_error(message: str, fix: Optional[str], as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message, "fix": fix}))
    else:
        print("error: " + message, file=sys.stderr)
        if fix:
            print("fix: " + fix, file=sys.stderr)
    return 2


def run_verification(path: Path, against: Optional[Dict[str, Any]], key_id: Optional[List[str]],
                     kms: Optional[Dict[str, Any]] = None):
    """Dispatch on what ``path`` is; returns a ``VerifyReport``."""
    from briefcase.compliance.bundle import is_bundle, verify_bundle
    from briefcase.integrity.evidence import is_sqlite, verify_jsonl, verify_store

    if is_bundle(path):
        return verify_bundle(path, expected_key_ids=key_id, **(kms or {}))
    if is_sqlite(path):
        return verify_store(path, against=against, expected_key_id=key_id)
    return verify_jsonl(path, against=against, expected_key_id=key_id)


def _range(report) -> str:
    if report.first_seq is None:
        return ""
    if report.first_seq == report.last_seq:
        return " (seq %d)" % report.first_seq
    return " (seq %d-%d)" % (report.first_seq, report.last_seq)


_PLURALS = {"entry": "entries"}


def _plural(n: int, word: str) -> str:
    return "%d %s" % (n, word if n == 1 else _PLURALS.get(word, word + "s"))


def verdict(report) -> str:
    """``intact``, ``incomplete`` (only entries missing from the end), or ``tampered``."""
    if report.intact:
        return "intact"
    if all(p.kind == "truncated" for p in report.problems):
        return "incomplete"
    return "tampered"


def render(report, path: Path) -> List[str]:
    label = FORMAT_LABELS.get(report.format, report.format)
    if report.format == "bundle" and getattr(report, "format_version", None):
        label = "evidence bundle, format %d" % report.format_version
    lines = ["%s: %s (%s)" % (verdict(report).upper(), path.name, label)]
    if report.format == "bundle":
        lines.append(
            "  Checked %s%s against %s."
            % (_plural(report.records_checked, "record"), _range(report),
               _plural(report.checkpoints_checked, "checkpoint"))
        )
    else:
        lines.append(
            "  Checked %s%s and %s."
            % (_plural(report.entries_checked, "entry"), _range(report),
               _plural(report.checkpoints_checked, "checkpoint"))
        )
    signers = getattr(report, "signers", None) or []
    if signers:
        status_text = {"valid": "valid", "invalid": "INVALID", "unchecked": "not checked",
                       "untrusted": "not trusted (key not pinned)"}
        lines.append("  Signatures: " + "; ".join(
            "%s %s on the %s: %s" % (s["type"], str(s["key_id"])[:12] if s["type"] == "ed25519" else s["key_id"],
                                     s["covers"], status_text.get(s["status"], s["status"]))
            for s in signers) + ".")
    elif report.signed_checkpoints or report.signatures_verified:
        keys_text = ", ".join(k[:12] for k in report.key_ids) or "-"
        lines.append(
            "  Signatures: %d verified (key %s)." % (report.signatures_verified, keys_text)
        )
    elif report.format == "bundle":
        lines.append("  Signatures: none (the bundle is not signed).")
    elif report.checkpoints_checked:
        lines.append("  Signatures: none (checkpoints are not signed).")
    if report.intact:
        if report.format in ("chained-store", "chain-jsonl"):
            lines.append("  Every entry links to the one before it and matches its record.")
            pending = report.entries_after_last_checkpoint
            if pending:
                where = "after the last checkpoint " if report.checkpoints_checked else ""
                lines.append(
                    "  %s %s%s chained but not yet covered by a checkpoint."
                    % (_plural(pending, "entry"), where, "is" if pending == 1 else "are")
                )
            if report.anchored:
                lines.append("  Checked against your saved checkpoint: nothing was cut from the end.")
        elif report.format == "bundle" and report.format_version == 1:
            lines.append("  The content hash matches the decision, policy and evidence.")
        elif report.format == "bundle":
            lines.append("  Every file matches the sealed manifest.")
            if report.entries_checked:
                lines.append("  Every chained record matches its chain entry and its Merkle proof leads to a "
                             "checkpoint.")
    else:
        lines.append("  %s:" % _plural(len(report.problems), "problem"))
        for problem in report.problems[:20]:
            lines.append("    - " + problem.detail)
        if len(report.problems) > 20:
            lines.append("    ... and %d more (use --json for all)" % (len(report.problems) - 20))
        facts = []
        if report.first_broken_seq is not None:
            facts.append("first broken entry: %d" % report.first_broken_seq)
        if report.missing_ranges:
            facts.append("missing: " + ", ".join(
                "%d-%d" % r if r[0] != r[1] else str(r[0]) for r in report.missing_ranges))
        if report.modified_seqs:
            facts.append("changed: " + ", ".join(str(s) for s in report.modified_seqs[:10]))
        if facts:
            lines.append("  " + "; ".join(facts).capitalize() + ".")
    for note in report.notes:
        lines.append("  Note: " + note.replace("(against_checkpoint)", "(--against FILE)"))
    return lines


def _next_line(report, path: Path, args, saved: bool = False) -> str:
    if not report.intact:
        return "Next: briefcase verify %s --json > verify-report.json   (keep the full report)" % path
    if report.format == "bundle":
        if report.signatures_unchecked:
            return "Next: briefcase bundle verify %s --key kms:KEY_ID --kms   (check the KMS signature)" % path
        if not args.key and report.key_ids:
            return "Next: briefcase bundle verify %s%s" % (path, "".join(" --key " + k for k in report.key_ids))
        return "Next: keep this bundle with the audit file; it verifies offline."
    if report.format == "chained-store" and saved:
        return "Next: copy %s off this machine; later run: briefcase verify %s --against %s" % (
            args.save_checkpoint, path, args.save_checkpoint)
    if report.format == "chained-store" and not report.anchored:
        return "Next: briefcase verify %s --save-checkpoint anchor.json   (keep it off this machine)" % path
    return "Next: briefcase bundle create --from %s --out evidence-bundle.zip" % path


def _save_checkpoint(path: Path, out: Path) -> Optional[str]:
    from briefcase.integrity.evidence import ChainedStore

    with ChainedStore(path) as store:
        checkpoints = store.checkpoints()
    if not checkpoints:
        return "the store has no checkpoint yet (they are written every N records and on shutdown)"
    if out.exists():
        return "%s already exists; choose a new file name" % out
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"checkpoint": checkpoints[-1], "source": path.name}, indent=2) + "\n")
    return None


def cmd_verify(args, store=None, engine=None, stack=None) -> int:
    from briefcase.integrity.evidence import EvidenceFormatError, load_checkpoint_file
    from briefcase.integrity.keys import KeyFileError

    path = Path(args.path)
    as_json = bool(getattr(args, "json", False))
    if not path.exists():
        return _usage_error("%s does not exist" % path, "check the path, e.g. briefcase verify ./evidence.db", as_json)
    if path.is_dir():
        return _usage_error("%s is a directory" % path, "pass the .db, .jsonl or .zip file itself", as_json)
    against = None
    if getattr(args, "against", None):
        try:
            against = load_checkpoint_file(args.against)
        except (OSError, ValueError) as exc:
            return _usage_error("cannot read --against %s: %s" % (args.against, exc),
                                "pass a file written by briefcase verify --save-checkpoint", as_json)
    try:
        key_id = resolve_key_id(getattr(args, "key", None))
    except (OSError, KeyFileError, ValueError) as exc:
        return _usage_error("cannot use --key %s: %s" % (" ".join(args.key or []), exc),
                            "pass a 64-hex key id or a key file", as_json)
    try:
        kms = kms_options(args)
    except ImportError as exc:
        return _usage_error(str(exc), "pip install 'briefcase-ai[compliance-kms]'", as_json)
    try:
        report = run_verification(path, against, key_id, kms)
    except EvidenceFormatError as exc:
        return _usage_error(str(exc), None, as_json)
    saved_error = None
    if getattr(args, "save_checkpoint", None):
        if report.format != "chained-store":
            saved_error = "--save-checkpoint works on a chained store only"
        else:
            saved_error = _save_checkpoint(path, Path(args.save_checkpoint))
    if as_json:
        out = report.to_dict()
        out["verdict"] = verdict(report)
        if getattr(args, "save_checkpoint", None):
            out["saved_checkpoint"] = None if saved_error else args.save_checkpoint
            out["save_checkpoint_error"] = saved_error
        print(json.dumps(out, indent=2))
    else:
        for line in render(report, path):
            print(line)
        if getattr(args, "save_checkpoint", None):
            if saved_error:
                print("  Checkpoint not saved: " + saved_error)
            else:
                print("  Saved the latest checkpoint to %s. Keep a copy where the writer of %s cannot "
                      "change it." % (args.save_checkpoint, path.name))
        saved = bool(getattr(args, "save_checkpoint", None)) and not saved_error
        print(_next_line(report, path, args, saved))
    return 0 if report.intact else 1


def _status_path() -> Optional[Path]:
    choice = (os.environ.get("BRIEFCASE_EXPORTER") or "").strip()
    if choice.startswith("chain:") and choice[6:].strip():
        return Path(choice[6:].strip()).expanduser()
    return None


def status() -> Dict[str, Any]:
    """For ``briefcase status``: is capture going into a chained store?"""
    path = _status_path()
    if path is None:
        return {
            "id": "chained-store",
            "state": "off",
            "detail": "records are not written to a tamper-evident chain",
            "next": "briefcase.observe('chain:evidence.db')  (or BRIEFCASE_EXPORTER=chain:evidence.db)",
        }
    if not path.exists():
        return {
            "id": "chained-store",
            "state": "on",
            "detail": "chain at %s (no records yet)" % path,
            "next": "briefcase verify %s" % path,
        }
    entries = None
    try:
        import sqlite3

        conn = sqlite3.connect("file:%s?mode=ro" % path.resolve(), uri=True)
        try:
            entries = conn.execute("SELECT COUNT(*) FROM chain_entries").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pass
    if not os.environ.get("BRIEFCASE_SIGNING_KEY"):
        return {
            "id": "chained-store",
            "state": "attention",
            "detail": "chain at %s (%s entries); checkpoints are unsigned"
                      % (path, entries if entries is not None else "?"),
            "next": "briefcase bundle keygen ~/.briefcase/signing-key.jwk, then set BRIEFCASE_SIGNING_KEY",
        }
    return {
        "id": "chained-store",
        "state": "on",
        "detail": "chain at %s (%s entries), signed checkpoints" % (path, entries if entries is not None else "?"),
        "next": "briefcase verify %s" % path,
    }


__all__ = [
    "GROUP", "cmd_verify", "register", "render", "resolve_key_id", "run_verification", "status", "verdict",
]
