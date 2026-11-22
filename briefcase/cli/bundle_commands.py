"""``briefcase bundle``: hand an auditor a self-verifying evidence bundle.

    briefcase bundle keygen ~/.briefcase/signing-key.jwk
    briefcase bundle create --from evidence.db --workflow wf-42 --out wf-42.zip --sign-key KEY
    briefcase bundle verify wf-42.zip --key KEYID

A bundle holds the selected records, their chain entries, Merkle inclusion
proofs against signed checkpoints, the public keys and a README.txt saying
what it proves. ``bundle verify`` needs only the SDK, not the database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from briefcase.cli.verify_commands import render, resolve_key_id, verdict

GROUP = "verify"
_RAW = argparse.RawDescriptionHelpFormatter


def register(sub) -> None:
    if "bundle" in sub.choices:
        return
    parser = sub.add_parser(
        "bundle",
        help="Create or check a self-verifying audit bundle of recorded decisions.",
        description="Export selected records with their chain entries, Merkle proofs and signed "
                    "checkpoints into one ZIP an auditor can check offline.",
        epilog="example:\n  briefcase bundle create --from evidence.db --out evidence-bundle.zip",
        formatter_class=_RAW,
    )
    bs = parser.add_subparsers(dest="sub")

    p = bs.add_parser(
        "create", help="Write a bundle of selected records from a chained store.", formatter_class=_RAW,
        epilog="example:\n  briefcase bundle create --from evidence.db --session s-123 --out s-123.zip",
    )
    p.add_argument("--from", dest="source", required=True, metavar="PATH",
                   help="Chained store (.db) or its JSONL export.")
    p.add_argument("--out", required=True, metavar="FILE", help="New bundle file (.zip); never overwritten.")
    p.add_argument("--session", help="Only records with this session_id.")
    p.add_argument("--workflow", help="Only records with this workflow_id.")
    p.add_argument("--trace", help="Only records with this trace_id.")
    p.add_argument("--since", metavar="TIME", help="Only records chained at or after TIME (ISO 8601, UTC).")
    p.add_argument("--until", metavar="TIME", help="Only records chained at or before TIME (ISO 8601, UTC).")
    p.add_argument("--sign-key", metavar="KEY",
                   help="Ed25519 key file that signs the bundle and any checkpoint the bundle adds "
                        "(see `briefcase bundle keygen`).")
    p.add_argument("--json", action="store_true", help="Machine-readable summary.")
    p.set_defaults(func=cmd_bundle_create)

    p = bs.add_parser(
        "verify", help="Check a bundle offline (exit 0 intact, 1 tampered, 2 usage).", formatter_class=_RAW,
        epilog="example:\n  briefcase bundle verify evidence-bundle.zip --key KEYID",
    )
    p.add_argument("path", metavar="FILE")
    p.add_argument("--key", metavar="KEY", action="append",
                   help="Require this signer: a 64-hex key id or a key file (repeatable).")
    p.add_argument("--json", action="store_true", help="Machine-readable report.")
    p.set_defaults(func=cmd_bundle_verify)

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
    try:
        summary = create_bundle(source, args.out, selection=selection, sign_key=args.sign_key)
    except (KeyFileError, OSError) as exc:
        return _fail(str(exc), "make a key with: briefcase bundle keygen KEYFILE", as_json, 2)
    except EvidenceFormatError as exc:
        return _fail(str(exc), None, as_json, 2)
    except BundleError as exc:
        code = 1 if "not intact" in str(exc) else 2
        return _fail(str(exc), None, as_json, code)
    except ImportError as exc:
        return _fail(str(exc), "pip install 'briefcase-ai[integrity]'", as_json, 2)
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
    print("  Bundle %s; content hash %s." % ("signed" if summary.signed else "not signed", summary.content_hash))
    key_hint = "".join(" --key " + k for k in summary.all_key_ids)
    print("Next: briefcase bundle verify %s%s" % (summary.path, key_hint))
    return 0


def cmd_bundle_verify(args, store=None, engine=None, stack=None) -> int:
    from briefcase.compliance.audit_bundle import verify_bundle
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
        report = verify_bundle(path, expected_key_id=key_id)
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
        if not key_id and report.key_ids:
            print("Next: briefcase bundle verify %s%s   (pin the keys the record keeper published)"
                  % (path, "".join(" --key " + k for k in report.key_ids)))
        else:
            print("Next: keep this bundle with the audit file; anyone can re-run this check offline.")
    else:
        print("Next: ask the record keeper for a fresh bundle; do not rely on this one.")
    return 0 if report.intact else 1


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
