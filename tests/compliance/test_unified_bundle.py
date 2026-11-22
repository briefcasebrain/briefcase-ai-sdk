"""One evidence bundle format, one verifier: format 2 plus the released format 1.

The two ``*_v1.json`` fixtures were written by the released
``ExaminerBundle`` / ``SignedExaminerBundle`` code (commit b36b230, before
the formats were unified) with the offline fake KMS in ``fake_kms.py``.
They must keep verifying. Only the public half of that KMS key is kept;
tests that sign use a fresh key under ``SIGNING_KEY_ID``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from briefcase.cli import bundle_commands, verify_commands  # noqa: E402
from briefcase.compliance import (  # noqa: E402
    BundleRecord,
    Ed25519Signer,
    ExaminerBundle,
    KmsSigner,
    SignedExaminerBundle,
    inspect_bundle,
    migrate_legacy,
    register_verifier,
    verify_bundle,
    write_bundle,
)
from briefcase.compliance import signers as signer_mod  # noqa: E402
from briefcase.compliance.signers import SignatureCheck  # noqa: E402
from briefcase.integrity.evidence import EvidenceFormatError  # noqa: E402

from .fake_kms import FIXTURE_KEY_ID, SIGNING_KEY_ID, FakeKms, FakeSession  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "compliance"
UNSIGNED_V1 = FIXTURES / "examiner_bundle_v1.json"
SIGNED_V1 = FIXTURES / "signed_examiner_bundle_v1.json"
SEED = bytes(range(32))


def kinds(report):
    return [p.kind for p in report.problems]


@pytest.fixture
def kms():
    return FakeKms.with_fixture_key()


@pytest.fixture
def ed_signer():
    return Ed25519Signer(SEED)


# --------------------------------------------------------------------------- format 1 (released)


def test_released_unsigned_bundle_still_verifies():
    report = verify_bundle(UNSIGNED_V1)
    assert report.intact and report.format_version == 1
    assert report.records_checked == 3 and report.hash_schemes == ["examiner-compat-1"]
    assert any("unkeyed" in note for note in report.notes)
    ExaminerBundle.from_json(UNSIGNED_V1.read_text()).verify()


def test_released_kms_bundle_verifies_only_against_a_pinned_key(kms):
    unchecked = verify_bundle(SIGNED_V1)
    assert unchecked.intact and unchecked.signatures_unchecked == 1 and unchecked.signatures_verified == 0
    checked = verify_bundle(SIGNED_V1, expected_key_ids=[FIXTURE_KEY_ID], kms_client=kms)
    assert checked.intact and checked.signatures_verified == 1
    assert checked.signers[0]["type"] == "aws-kms" and checked.signers[0]["status"] == "valid"
    wrong = verify_bundle(SIGNED_V1, expected_key_ids=["kms:arn:aws:kms:us-east-1:1:key/other"], kms_client=kms)
    assert kinds(wrong) == ["unexpected_key"]
    algorithm = verify_bundle(SIGNED_V1, expected_key_ids=[FIXTURE_KEY_ID], kms_client=kms,
                              kms_algorithms=["RSASSA_PKCS1_V1_5_SHA_256"])
    assert kinds(algorithm) == ["bundle_signature_invalid"]


def test_released_kms_bundle_tampering(tmp_path, kms):
    data = json.loads(SIGNED_V1.read_text())
    data["decision"]["selected"] = "USDT"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(data))
    report = verify_bundle(edited, expected_key_ids=[FIXTURE_KEY_ID], kms_client=kms)
    assert "content_modified" in kinds(report)
    # Re-sealing the content hash does not help: the KMS signature covers the old one.
    data["content_hash"] = ExaminerBundle.from_dict(data)._compute_hash()
    edited.write_text(json.dumps(data))
    report = verify_bundle(edited, expected_key_ids=[FIXTURE_KEY_ID], kms_client=kms)
    assert kinds(report) == ["bundle_signature_invalid"]


def test_old_python_api_behaves_as_before_on_the_released_fixture(kms):
    text = SIGNED_V1.read_text()
    bundle = SignedExaminerBundle.from_json(text)
    assert bundle.to_json(indent=2) + "\n" == text  # format 1 is written byte for byte as before
    assert bundle.verify_signature(expected_key_id=FIXTURE_KEY_ID, kms_client=kms) is True
    assert bundle.verify_signature(expected_key_id=FIXTURE_KEY_ID, boto_session=FakeSession(kms)) is True
    assert bundle.verify_signature(expected_key_id="other", kms_client=kms) is False
    assert bundle.verify_signature(expected_key_id=FIXTURE_KEY_ID, expected_algorithm="ECDSA_SHA_256",
                                   kms_client=kms) is False
    bundle.content_hash = "sha256:" + "0" * 64
    assert bundle.verify_signature(expected_key_id=FIXTURE_KEY_ID, kms_client=kms) is False
    unsigned = ExaminerBundle.from_json(UNSIGNED_V1.read_text())
    assert unsigned.to_json(indent=2) + "\n" == UNSIGNED_V1.read_text()


def test_build_and_sign_goes_through_the_kms_signer(monkeypatch, kms):
    from briefcase.compliance import examiner as examiner_mod

    parent = ExaminerBundle.from_json(UNSIGNED_V1.read_text())
    monkeypatch.setattr(examiner_mod.ExaminerBundle, "build", classmethod(lambda *_a, **_k: parent))
    signed = SignedExaminerBundle.build_and_sign(decision=object(), policy_registry=object(),
                                                 evidence_store=object(), kms_key_id=SIGNING_KEY_ID,
                                                 kms_client=kms)
    assert signed.content_hash == parent.content_hash
    assert signed.verify_signature(expected_key_id=SIGNING_KEY_ID, kms_client=kms)
    assert ("sign", SIGNING_KEY_ID) in kms.calls


# --------------------------------------------------------------------------- migration and format 2


def test_migrating_a_released_bundle_keeps_its_kms_signature(tmp_path, kms, ed_signer):
    out = tmp_path / "decision.zip"
    written = migrate_legacy(SIGNED_V1, out, signers=[ed_signer])
    assert written.manifest["format_version"] == 2 and written.manifest["subject"] == "examiner"
    report = verify_bundle(out, expected_key_ids=[ed_signer.key_id, "kms:" + FIXTURE_KEY_ID], kms_client=kms)
    assert report.intact, report.problems
    assert report.signatures_verified == 2  # the new Ed25519 seal and the original KMS signature
    # Reading it back gives the same signed examiner bundle.
    revived = SignedExaminerBundle.from_bundle(out)
    revived.verify()
    assert revived.verify_signature(expected_key_id=FIXTURE_KEY_ID, kms_client=kms)


def test_examiner_bundle_round_trips_through_format_2(tmp_path, kms, ed_signer):
    original = ExaminerBundle.from_json(UNSIGNED_V1.read_text())
    out = tmp_path / "decision.zip"
    original.to_bundle(out, signers=[ed_signer, KmsSigner(SIGNING_KEY_ID, kms_client=kms)])
    info = inspect_bundle(out)
    assert info["roles"] == {"decision": 1, "policy": 1, "evidence": 1}
    assert [s["type"] for s in info["signatures"]] == ["ed25519", "aws-kms"]
    report = verify_bundle(out, expected_key_ids=[ed_signer.key_id, SIGNING_KEY_ID], kms_client=kms)
    assert report.intact and report.signatures_verified == 2
    revived = ExaminerBundle.from_bundle(out)
    assert revived.to_dict() == original.to_dict()
    revived.verify()


def _rewrite(path, name, transform):
    with zipfile.ZipFile(path) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    items[name] = transform(items[name])
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in items.items():
            zf.writestr(n, data)


def test_format_2_tampering_and_signature_swaps(tmp_path, kms, ed_signer):
    records = [BundleRecord({"decision_id": "d1", "score": 0.93}, role="decision")]
    out = tmp_path / "one.zip"
    write_bundle(out, records, subject="examiner", signers=[ed_signer])
    assert verify_bundle(out, expected_key_ids=ed_signer.key_id).intact

    edited = tmp_path / "edited.zip"
    edited.write_bytes(out.read_bytes())
    _rewrite(edited, "records.jsonl", lambda b: b.replace(b"0.93", b"0.13"))
    assert kinds(verify_bundle(edited)) == ["file_modified"]

    # Replace the Ed25519 seal with one by another key: the sealed signer list catches it.
    swapped = tmp_path / "swapped.zip"
    swapped.write_bytes(out.read_bytes())
    attacker = Ed25519Signer(bytes(range(1, 33)))

    def resign(raw: bytes) -> bytes:
        manifest = json.loads(raw)
        manifest["signatures"] = [attacker.sign(bytes.fromhex(manifest["content_hash"][7:]))]
        return json.dumps(manifest).encode()

    _rewrite(swapped, "manifest.json", resign)
    assert "bundle_signature_invalid" in kinds(verify_bundle(swapped))
    stripped = tmp_path / "stripped.zip"
    stripped.write_bytes(out.read_bytes())
    _rewrite(stripped, "manifest.json", lambda raw: json.dumps(dict(json.loads(raw), signatures=[])).encode())
    assert "bundle_signature_invalid" in kinds(verify_bundle(stripped))
    unsigned = tmp_path / "unsigned.zip"
    write_bundle(unsigned, records)
    assert kinds(verify_bundle(unsigned, expected_key_ids=ed_signer.key_id)) == ["no_trusted_signature"]


def test_kms_seal_needs_a_pin_and_kms_access(tmp_path, kms):
    out = tmp_path / "kms.zip"
    write_bundle(out, [BundleRecord({"x": 1})], signers=[KmsSigner(SIGNING_KEY_ID, kms_client=kms)])
    assert verify_bundle(out).signatures_unchecked == 1
    assert verify_bundle(out, expected_key_ids=SIGNING_KEY_ID).signatures_unchecked == 1
    assert verify_bundle(out, expected_key_ids=SIGNING_KEY_ID, kms_client=kms).signatures_verified == 1
    other = FakeKms()
    other.create_key(SIGNING_KEY_ID)  # same id, different key material
    assert kinds(verify_bundle(out, expected_key_ids=SIGNING_KEY_ID, kms_client=other)) == [
        "bundle_signature_invalid"]


class HmacSigner:
    """A third-party signer, to show the Signer protocol is open."""

    type = "test-hmac"
    key_id = "shared-secret-1"

    def sign(self, message: bytes) -> dict:
        mac = hmac.new(b"secret", message, hashlib.sha256).digest()
        return {"type": self.type, "key_id": self.key_id, "algorithm": "HMAC-SHA256",
                "value": base64.b64encode(mac).decode()}


def test_custom_signers_plug_in(tmp_path):
    out = tmp_path / "hmac.zip"
    write_bundle(out, [BundleRecord({"x": 1})], signers=[HmacSigner()])
    assert verify_bundle(out).signatures_unchecked == 1  # no verifier registered yet

    def check(sig, message, pins):
        ok = hmac.compare_digest(base64.b64decode(sig["value"]), hmac.new(b"secret", message, hashlib.sha256).digest())
        return SignatureCheck("valid" if ok else "invalid", sig["type"], sig["key_id"])

    register_verifier("test-hmac", check)
    try:
        assert verify_bundle(out).signatures_verified == 1
    finally:
        signer_mod._VERIFIERS.pop("test-hmac")


def test_dispatch_refuses_unknown_versions_and_schemes(tmp_path):
    out = tmp_path / "b.zip"
    write_bundle(out, [BundleRecord({"x": 1})])
    for field, value, message in (("format_version", 3, "newer than this SDK"),
                                  ("hash_scheme", "sha1-json", "unknown hash scheme"),
                                  ("format", "briefcase-audit-bundle", "pre-release")):
        copy = tmp_path / ("%s.zip" % field)
        copy.write_bytes(out.read_bytes())
        _rewrite(copy, "manifest.json", lambda raw, f=field, v=value: json.dumps(dict(json.loads(raw), **{f: v}))
                 .encode())
        with pytest.raises(EvidenceFormatError, match=message):
            verify_bundle(copy)


# --------------------------------------------------------------------------- CLI


def run_cli(argv, capsys):
    parser = argparse.ArgumentParser(prog="briefcase")
    sub = parser.add_subparsers(dest="command")
    verify_commands.register(sub)
    bundle_commands.register(sub)
    args = parser.parse_args(argv)
    code = args.func(args, None, None, None)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_handles_both_formats(tmp_path, capsys, monkeypatch, kms):
    monkeypatch.setattr(signer_mod, "kms_client_from", lambda session=None: kms)
    code, out, _ = run_cli(["verify", str(SIGNED_V1)], capsys)
    assert code == 0 and "evidence bundle, format 1" in out and "not checked" in out
    code, out, _ = run_cli(["bundle", "verify", str(SIGNED_V1), "--key", "kms:" + FIXTURE_KEY_ID, "--kms"], capsys)
    assert code == 0 and "aws-kms %s on the bundle: valid" % FIXTURE_KEY_ID in out
    code, out, _ = run_cli(["bundle", "inspect", str(SIGNED_V1)], capsys)
    assert code == 0 and "format 1" in out and "aws-kms" in out
    key = tmp_path / "k.jwk"
    key.write_bytes(SEED.hex().encode())
    out_zip = tmp_path / "decision.zip"
    code, out, _ = run_cli(["bundle", "create", "--from", str(SIGNED_V1), "--out", str(out_zip),
                            "--sign-key", str(key)], capsys)
    assert code == 0 and "Kept the original AWS KMS signature" in out
    code, out, _ = run_cli(["bundle", "inspect", str(out_zip), "--json"], capsys)
    assert json.loads(out)["format_version"] == 2
    code, out, _ = run_cli(["verify", str(out_zip), "--key", str(key), "--key", "kms:" + FIXTURE_KEY_ID, "--kms"],
                           capsys)
    assert code == 0 and out.startswith("INTACT") and "format 2" in out
    kms_zip = tmp_path / "kms.zip"
    code, out, _ = run_cli(["bundle", "create", "--from", str(UNSIGNED_V1), "--out", str(kms_zip),
                            "--sign-key", "kms:" + SIGNING_KEY_ID], capsys)
    assert code == 0, out
    code, out, _ = run_cli(["bundle", "verify", str(kms_zip), "--key", SIGNING_KEY_ID, "--kms", "--json"], capsys)
    assert code == 0 and json.loads(out)["signatures_verified"] == 1


@pytest.mark.parametrize("name, verified, unchecked", [
    ("records-v2.zip", 3, 0), ("examiner-v2.zip", 1, 1), ("kms-v2.zip", 0, 1),
])
def test_committed_format_2_fixtures_verify(name, verified, unchecked):
    # The TypeScript verifier is tested on the same files (js/runtime/test/bundle.test.ts).
    report = verify_bundle(FIXTURES.parent / "bundles" / name)
    assert report.intact and report.signatures_verified == verified and report.signatures_unchecked == unchecked
