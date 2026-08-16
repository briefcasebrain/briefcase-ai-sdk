"""Unit tests for :class:`briefcase.compliance.signed_bundle.SignedExaminerBundle`.

Uses ``moto.mock_aws`` with an asymmetric RSA KMS key to exercise the
sign/verify roundtrip. Tampering with the bundle post-sign must flip
``verify_signature()`` from True to False.

Skips cleanly when boto3 or moto is not installed (dev extra).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from briefcase.compliance.signed_bundle import SignedExaminerBundle  # noqa: E402


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def mocked_kms(aws_credentials: None) -> Iterator[tuple[str, "boto3.Session"]]:
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        kms = session.client("kms")
        key = kms.create_key(
            KeyUsage="SIGN_VERIFY",
            KeySpec="RSA_2048",
        )
        yield key["KeyMetadata"]["KeyId"], session


def _build_unsigned_bundle() -> SignedExaminerBundle:
    bundle = SignedExaminerBundle(
        decision={"decision_id": "d1", "action": "route_to_compliance"},
        policy={"policy_id": "p1", "version": "1"},
        evidence=[{"record_id": "r1", "key": "USDC/USD", "value": 1.0001}],
        as_of_transaction_time="2026-01-01T00:00:00+00:00",
    )
    bundle.content_hash = bundle._compute_hash()
    return bundle


def _sign_bundle(
    bundle: SignedExaminerBundle, *, key_id: str, session: "boto3.Session"
) -> None:
    kms = session.client("kms")
    resp = kms.sign(
        KeyId=key_id,
        Message=bundle.content_hash.encode("utf-8"),
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PSS_SHA_256",
    )
    bundle.kms_key_id = key_id
    bundle.signature = bytes(resp["Signature"])


def test_sign_and_verify_roundtrip(
    mocked_kms: tuple[str, "boto3.Session"],
) -> None:
    key_id, session = mocked_kms
    bundle = _build_unsigned_bundle()
    _sign_bundle(bundle, key_id=key_id, session=session)

    assert (
        bundle.verify_signature(expected_key_id=key_id, boto_session=session)
        is True
    )


def test_json_roundtrip_preserves_signature(
    mocked_kms: tuple[str, "boto3.Session"],
) -> None:
    key_id, session = mocked_kms
    bundle = _build_unsigned_bundle()
    _sign_bundle(bundle, key_id=key_id, session=session)

    parsed = SignedExaminerBundle.from_json(bundle.to_json())
    assert parsed.signature == bundle.signature
    assert parsed.kms_key_id == bundle.kms_key_id
    assert parsed.content_hash == bundle.content_hash
    assert (
        parsed.verify_signature(expected_key_id=key_id, boto_session=session)
        is True
    )


def test_tampered_content_hash_fails_verify(
    mocked_kms: tuple[str, "boto3.Session"],
) -> None:
    key_id, session = mocked_kms
    bundle = _build_unsigned_bundle()
    _sign_bundle(bundle, key_id=key_id, session=session)

    bundle.content_hash = "sha256:" + "0" * 64
    assert (
        bundle.verify_signature(expected_key_id=key_id, boto_session=session)
        is False
    )


def test_build_and_sign_uses_parent_build(
    mocked_kms: tuple[str, "boto3.Session"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_and_sign`` should call ``ExaminerBundle.build`` and then sign."""
    key_id, session = mocked_kms
    parent = _build_unsigned_bundle()
    # Patch ExaminerBundle.build to return a pre-baked bundle so we do not
    # need to construct real AgentRoutingDecision / PolicyRegistry here.
    from briefcase.compliance import examiner as examiner_mod

    monkeypatch.setattr(
        examiner_mod.ExaminerBundle, "build", classmethod(lambda *_a, **_k: parent)
    )

    bundle = SignedExaminerBundle.build_and_sign(
        decision=object(),
        policy_registry=object(),
        evidence_store=object(),
        kms_key_id=key_id,
        boto_session=session,
    )
    assert bundle.content_hash == parent.content_hash
    assert (
        bundle.verify_signature(expected_key_id=key_id, boto_session=session)
        is True
    )


def test_key_substitution_bundle_fails_verify(
    mocked_kms: tuple[str, "boto3.Session"],
) -> None:
    """A bundle self-signed with a different key must not verify against the
    verifier's pinned key, even though its embedded kms_key_id matches its own
    signature."""
    key_id, session = mocked_kms
    kms = session.client("kms")
    attacker_key = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")[
        "KeyMetadata"
    ]["KeyId"]

    bundle = _build_unsigned_bundle()
    _sign_bundle(bundle, key_id=attacker_key, session=session)
    parsed = SignedExaminerBundle.from_json(bundle.to_json())

    assert (
        parsed.verify_signature(expected_key_id=key_id, boto_session=session)
        is False
    )


def test_algorithm_mismatch_fails_without_kms_call(
    mocked_kms: tuple[str, "boto3.Session"],
) -> None:
    key_id, session = mocked_kms
    bundle = _build_unsigned_bundle()
    _sign_bundle(bundle, key_id=key_id, session=session)
    bundle.signing_algorithm = "ECDSA_SHA_256"

    assert (
        bundle.verify_signature(expected_key_id=key_id, boto_session=session)
        is False
    )
