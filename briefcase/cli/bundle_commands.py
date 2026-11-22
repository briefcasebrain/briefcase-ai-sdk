"""``briefcase bundle``: hand an auditor a self-verifying evidence bundle.

    briefcase bundle keygen ~/.briefcase/signing-key.jwk
    briefcase bundle create --from evidence.db --workflow wf-42 --out wf-42.zip --sign-key KEY
    briefcase bundle create --from examiner-bundle.json --out decision.zip --sign-key KEY
    briefcase bundle verify wf-42.zip --key KEYID
    briefcase bundle inspect wf-42.zip

One format (``briefcase.compliance.bundle``, format 2) for record sets cut
from a chained store and for single sealed routing decisions; ``verify``
also accepts the format-1 JSON that ``ExaminerBundle.to_json()`` writes.
``bundle verify`` needs only the SDK, not the database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from briefcase.cli.verify_commands import add_kms_flags, kms_options, render, resolve_key_id, verdict

GROUP = "verify"
_RAW = argparse.RawDescriptionHelpFormatter


def register(sub) -> None:
    if "bundle" in sub.choices:
        return
    parser = sub.add_parser(
        "bundle",
        help="Create, check or inspect a self-verifying evidence bundle (records or one sealed decision).",
        description="One bundle format for sets of chained records (with Merkle proofs against signed "
                    "checkpoints) and for single sealed routing decisions, signed with Ed25519 key files "
                    "or AWS KMS. An auditor checks it offline with `briefcase bundle verify`.",
        epilog="example:\n  briefcase bundle create --from evidence.db --out evidence-bundle.zip",
        formatter_class=_RAW,
    )
    bs = parser.add_subparsers(dest="sub")

    p = bs.add_parser(
        "create", help="Write a bundle from a chained store, its JSONL export, or an examiner bundle JSON.",
        formatter_class=_RAW,
        epilog="example:\n  briefcase bundle create --from evidence.db --session s-123 --out s-123.zip",
    )
    p.add_argument("--from", dest="source", required=True, metavar="PATH",
                   help="Chained store (.db), its JSONL export, or a format-1 examiner bundle (.json, "
                        "re-packaged as format 2 with its KMS signature kept).")
    p.add_argument("--out", required=True, metavar="FILE", help="New bundle file (.zip); never overwritten.")
    p.add_argument("--session", help="Only records with this session_id.")
    p.add_argument("--workflow", help="Only records with this workflow_id.")
    p.add_argument("--trace", help="Only records with this trace_id.")
    p.add_argument("--since", metavar="TIME", help="Only records chained at or after TIME (ISO 8601, UTC).")
    p.add_argument("--until", metavar="TIME", help="Only records chained at or before TIME (ISO 8601, UTC).")
    p.add_argument("--sign-key", metavar="KEY", action="append",
                   help="Signer (repeatable): an Ed25519 key file (see `briefcase bundle keygen`), which "
                        "also signs any checkpoint the bundle adds, or kms:KEY_ID to sign with AWS KMS "
                        "(calls AWS with your credentials).")
    p.add_argument("--kms-algorithm", default="RSASSA_PSS_SHA_256", metavar="ALG",
                   help="Signing algorithm for kms: signers (default RSASSA_PSS_SHA_256).")
    p.add_argument("--json", action="store_true", help="Machine-readable summary.")
    p.set_defaults(func=cmd_bundle_create)

    p = bs.add_parser(
        "verify", help="Check a bundle of any format version (exit 0 intact, 1 tampered, 2 usage).",
        formatter_class=_RAW,
        epilog="example:\n  briefcase bundle verify evidence-bundle.zip --key KEYID",
    )
    p.add_argument("path", metavar="FILE")
    p.add_argument("--key", metavar="KEY", action="append",
                   help="Require this signer: a 64-hex Ed25519 key id, a key file, or kms:KEY_ID / a KMS "
                        "key ARN (repeatable).")
    add_kms_flags(p)
    p.add_argument("--json", action="store_true", help="Machine-readable report.")
    p.set_defaults(func=cmd_bundle_verify)

    p = bs.add_parser(
        "inspect", help="Show a bundle's format, signers and contents without verifying it.",
        formatter_class=_RAW, epilog="example:\n  briefcase bundle inspect evidence-bundle.zip",
    )
    p.add_argument("path", metavar="FILE")
    p.add_argument("--json", action="store_true", help="Machine-readable description.")
    p.set_defaults(func=cmd_bundle_inspect)

    p = bs.add_parser(
        "keygen", help="Create an Ed25519 signing key file (private JWK, mode 0600).", formatter_class=_RAW,
        epilog="example:\n  briefcase bundle keygen ~/.briefcase/signing-key.jwk",
    )
    p.add_argument("path", metavar="FILE", help="New key file; never overwritten.")
    p.add_argument("--json", action="store_true", help="Print the public key and key id as JSON.")
    p.set_defaults(func=cmd_bundle_keygen)

    parser.set_defaults(func=_bundle_help(parser))


def _bundle_help(parser):
    def show(args, store=None, engine=None, stack=None) -> int:
        parser.print_help(sys.stderr)
        return 2
    return show


def _fail(message: str, fix: Optional[str], as_json: bool, code: int) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message, "fix": fix}))
    else:
        print("error: " + message, file=sys.stderr)
        if fix:
            print("fix: " + fix, file=sys.stderr)
    return code


def cmd_bundle_create(args, store=None, engine=None, stack=None) -> int:
    from briefcase.compliance.audit_bundle import BundleError, Selection, create_bundle, parse_time
    from briefcase.integrity.evidence import EvidenceFormatError
    from briefcase.integrity.keys import KeyFileError

    as_json = bool(args.json)
    source = Path(args.source)
    if not source.exists():
        return _fail("%s does not exist" % source, "pass the chained store, e.g. --from ./evidence.db", as_json, 2)
    try:
        selection = Selection(
            session=args.session, workflow=args.workflow, trace=args.trace,
            since=parse_time(args.since) if args.since else None,
            until=parse_time(args.until) if args.until else None,
        )
    except ValueError as exc:
        return _fail("bad --since/--until: %s" % exc, "use ISO 8601, e.g. 2026-11-22T09:00:00Z", as_json, 2)
    from briefcase.compliance import signers as signer_mod
    from briefcase.compliance.bundle import load_legacy, migrate_legacy

    try:
        chosen: list = []
        for spec in args.sign_key or []:
            if spec.startswith("kms:"):
                chosen.append(signer_mod.KmsSigner(spec[4:], algorithm=args.kms_algorithm,
                                                   kms_client=signer_mod.kms_client_from(None)))
            else:
                chosen.append(signer_mod.Ed25519Signer(spec))
        if load_legacy(source) is not None:
            if not selection.is_empty():
                return _fail("--session/--workflow/--trace/--since/--until select from a chained store; "
                             "an examiner bundle is re-packaged whole", None, as_json, 2)
            written = migrate_legacy(source, args.out, signers=chosen)
            return _print_migrated(written, source, as_json)
        summary = create_bundle(source, args.out, selection=selection, signers=chosen)
    except (KeyFileError, OSError) as exc:
        return _fail(str(exc), "make a key with: briefcase bundle keygen KEYFILE", as_json, 2)
    except EvidenceFormatError as exc:
        return _fail(str(exc), None, as_json, 2)
    except BundleError as exc:
        code = 1 if "not intact" in str(exc) else 2
        return _fail(str(exc), None, as_json, code)
    except ImportError as exc:
        return _fail(str(exc), "pip install 'briefcase-ai[compliance-kms]'", as_json, 2)
    if as_json:
        print(json.dumps({"ok": True, **summary.__dict__}, indent=2))
        return 0
    print("Wrote %s" % summary.path)
    print("  %d record(s) of %d in the source chain, %d checkpoint(s)%s."
          % (summary.records, summary.source_entries, summary.checkpoints,
             " (%d made for this bundle)" % summary.bundle_checkpoints if summary.bundle_checkpoints else ""))
    if summary.key_ids:
        print("  Checkpoints signed by key %s." % ", ".join(summary.key_ids))
    else:
        print("  Checkpoints are NOT signed: the bundle proves consistency, not authorship. "
              "Pass --sign-key next time.")
    print("  Bundle %s; content hash %s." % (
        "signed by " + ", ".join(summary.signer_ids) if summary.signed else "not signed", summary.content_hash))
    print("Next: briefcase bundle verify %s%s" % (summary.path, _key_flags(summary.all_key_ids + [
        i for i in summary.signer_ids if i not in summary.all_key_ids])))
    return 0


def _key_flags(ids) -> str:
    out = ""
    for key_id in ids:
        is_ed25519 = len(key_id) == 64 and all(c in "0123456789abcdef" for c in key_id)
        out += " --key " + (key_id if is_ed25519 else "kms:" + key_id)
    if any(not (len(k) == 64 and all(c in "0123456789abcdef" for c in k)) for k in ids):
        out += " --kms"
    return out


def _print_migrated(written, source: Path, as_json: bool) -> int:
    manifest = written.manifest
    legacy = (manifest.get("context") or {}).get("legacy_signatures") or []
    if as_json:
        print(json.dumps({"ok": True, "path": written.path, "records": written.records,
                          "content_hash": manifest["content_hash"], "signers": written.signers,
                          "legacy_signatures": [{"type": s["type"], "key_id": s["key_id"]} for s in legacy]},
                         indent=2))
        return 0
    print("Wrote %s (format 2) from the format-1 examiner bundle %s" % (written.path, source.name))
    print("  %d record(s): the decision, its policy and the evidence it cited." % written.records)
    if legacy:
        print("  Kept the original AWS KMS signature by %s (it still covers the same content)." % legacy[0]["key_id"])
    print("  Bundle %s; content hash %s." % (
        "signed by " + ", ".join(str(s["key_id"]) for s in written.signers) if written.signers else "not signed",
        manifest["content_hash"]))
    ids = [str(s["key_id"]) for s in written.signers] + [str(s["key_id"]) for s in legacy]
    print("Next: briefcase bundle verify %s%s" % (written.path, _key_flags(ids)))
    return 0


def cmd_bundle_verify(args, store=None, engine=None, stack=None) -> int:
    from briefcase.compliance.bundle import verify_bundle
    from briefcase.integrity.evidence import EvidenceFormatError
    from briefcase.integrity.keys import KeyFileError

    as_json = bool(args.json)
    path = Path(args.path)
    if not path.is_file():
        return _fail("%s does not exist" % path, "pass the bundle .zip file", as_json, 2)
    try:
        key_id = resolve_key_id(args.key)
    except (OSError, KeyFileError, ValueError) as exc:
        return _fail("cannot use --key %s: %s" % (" ".join(args.key or []), exc),
                     "pass a 64-hex key id or a key file", as_json, 2)
    try:
        kms = kms_options(args)
    except ImportError as exc:
        return _fail(str(exc), "pip install 'briefcase-ai[compliance-kms]'", as_json, 2)
    try:
        report = verify_bundle(path, expected_key_ids=key_id, **kms)
    except EvidenceFormatError as exc:
        return _fail(str(exc), None, as_json, 2)
    if as_json:
        out = report.to_dict()
        out["verdict"] = verdict(report)
        print(json.dumps(out, indent=2))
        return 0 if report.intact else 1
    for line in render(report, path):
        print(line)
    if report.intact:
        if report.signatures_unchecked:
            print("Next: briefcase bundle verify %s --key kms:KEY_ID --kms   (check the KMS signature)" % path)
        elif not key_id and report.key_ids:
            print("Next: briefcase bundle verify %s%s   (pin the keys the record keeper published)"
                  % (path, _key_flags(report.key_ids)))
        else:
            print("Next: keep this bundle with the audit file; anyone can re-run this check offline.")
    else:
        print("Next: ask the record keeper for a fresh bundle; do not rely on this one.")
    return 0 if report.intact else 1


def cmd_bundle_inspect(args, store=None, engine=None, stack=None) -> int:
    from briefcase.compliance.bundle import inspect_bundle
    from briefcase.integrity.evidence import EvidenceFormatError

    as_json = bool(args.json)
    path = Path(args.path)
    if not path.is_file():
        return _fail("%s does not exist" % path, "pass the bundle file", as_json, 2)
    try:
        info = inspect_bundle(path)
    except EvidenceFormatError as exc:
        return _fail(str(exc), None, as_json, 2)
    if as_json:
        print(json.dumps(info, indent=2))
        return 0
    print("%s: %s format %s, hash scheme %s, subject %s" % (
        path.name, info["format"], info["format_version"], info["hash_scheme"], info["subject"]))
    if info.get("created_at"):
        print("  Created %s by %s" % (info["created_at"], info.get("created_by")))
    roles = ", ".join("%d %s%s" % (n, role, "" if n == 1 else "s") for role, n in sorted(info["roles"].items()) if n)
    print("  Records: %s; checkpoints: %s" % (roles or "none", (info.get("counts") or {}).get("checkpoints", 0)))
    selection = ", ".join("%s=%s" % kv for kv in (info.get("selection") or {}).items() if kv[1])
    if selection:
        print("  Selection: " + selection)
    ctx = info.get("context") or {}
    if ctx.get("as_of_transaction_time"):
        print("  Policy as of: %s" % ctx["as_of_transaction_time"])
    for sig in ctx.get("legacy_signatures") or []:
        print("  Original format-1 signature: %s %s (%s)" % (sig["type"], sig["key_id"], sig["algorithm"]))
    if info["signatures"]:
        for sig in info["signatures"]:
            print("  Signed: %s %s (%s) at %s" % (sig["type"], sig["key_id"], sig["algorithm"], sig["signed_at"]))
    else:
        print("  Signed: no")
    print("  Content hash: %s" % info["content_hash"])
    print("Next: briefcase bundle verify %s" % path)
    return 0


def cmd_bundle_keygen(args, store=None, engine=None, stack=None) -> int:
    from briefcase.integrity import keys

    as_json = bool(args.json)
    path = Path(args.path).expanduser()
    if path.exists():
        return _fail("%s already exists; keys are never overwritten" % path, "choose a new file name", as_json, 2)
    try:
        jwk = keys.generate_key_file(path)
    except OSError as exc:
        return _fail("cannot write %s: %s" % (path, exc.strerror or exc), None, as_json, 1)
    key_id = keys.key_id(jwk["x"])
    if as_json:
        print(json.dumps({"ok": True, "path": str(path), "public_jwk": jwk, "key_id": key_id}, indent=2))
        return 0
    print("Wrote private key %s (mode 0600). Keep it private; back it up." % path)
    print("  Key id (publish this so verifiers can pin it): %s" % key_id)
    print("  Public key: %s" % json.dumps(jwk))
    print("Next: export BRIEFCASE_SIGNING_KEY=%s   (then briefcase.observe('chain:evidence.db'))" % path)
    return 0


__all__ = ["GROUP", "cmd_bundle_create", "cmd_bundle_keygen", "cmd_bundle_verify", "register"]
