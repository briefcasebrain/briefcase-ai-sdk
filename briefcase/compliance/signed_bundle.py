"""AWS KMS-signed :class:`briefcase.compliance.examiner.ExaminerBundle`.

Scope of the integrity guarantee
--------------------------------
The parent ``ExaminerBundle.content_hash`` proves the bundle is
internally consistent: the decision, policy, and evidence referenced
in the bundle match the hash it carries. The KMS signature added here
proves that the hash was signed by a key under your control at the
time ``signed_at`` was recorded. It does NOT prove that the bundle
reflects what the production system actually did, only that an
internally consistent bundle was signed by a known KMS key.

Binding the bundle to production truth requires pairing this signature
with an independently witnessed WORM commit.

Install
-------
Requires ``boto3``: ``pip install briefcase-ai[compliance-kms]`` or
``pip install boto3``. The import is lazy, so this module loads without
boto3 installed and fails with a clear error on first sign/verify.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from briefcase.compliance.examiner import ExaminerBundle


_BOTO3_INSTALL_HINT = (
    "boto3 is required. Install with "
    "'pip install briefcase-ai[compliance-kms]' "
    "or 'pip install boto3'."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SignedExaminerBundle(ExaminerBundle):
    """Examiner bundle whose ``content_hash`` is signed by AWS KMS.

    The signature covers the UTF-8 encoding of the parent bundle's
    ``content_hash`` (which itself is a canonical-JSON SHA-256 of the
    decision + policy + evidence). It does not re-hash the bundle
    payload; verifying the signature AND recomputing the content hash
    together prove tamper-evidence for both layers.
    """

    kms_key_id: str = ""
    signature: bytes = b""
    signing_algorithm: str = "RSASSA_PSS_SHA_256"
    signed_at: datetime = field(default_factory=_utcnow)

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    @classmethod
    def build_and_sign(
        cls,
        *,
        decision: Any,
        policy_registry: Any,
        evidence_store: Any,
        kms_key_id: str,
        boto_session: Optional[Any] = None,
        signing_algorithm: str = "RSASSA_PSS_SHA_256",
        as_of_transaction_time: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "SignedExaminerBundle":
        """Build the parent bundle, then KMS-sign its ``content_hash``."""
        parent = ExaminerBundle.build(
            decision=decision,
            evidence_store=evidence_store,
            policy_registry=policy_registry,
            as_of_transaction_time=as_of_transaction_time,
            metadata=metadata,
        )
        kms = _kms_client(boto_session)
        resp = kms.sign(
            KeyId=kms_key_id,
            Message=parent.content_hash.encode("utf-8"),
            MessageType="RAW",
            SigningAlgorithm=signing_algorithm,
        )
        signature = resp["Signature"]
        return cls(
            decision=parent.decision,
            policy=parent.policy,
            evidence=parent.evidence,
            as_of_transaction_time=parent.as_of_transaction_time,
            content_hash=parent.content_hash,
            schema_version=parent.schema_version,
            metadata=dict(parent.metadata),
            kms_key_id=kms_key_id,
            signature=bytes(signature),
            signing_algorithm=signing_algorithm,
            signed_at=_utcnow(),
        )

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_signature(
        self,
        *,
        expected_key_id: str,
        expected_algorithm: str = "RSASSA_PSS_SHA_256",
        boto_session: Optional[Any] = None,
    ) -> bool:
        """Verify the KMS signature over ``content_hash`` against a caller-pinned key.

        ``expected_key_id`` and ``expected_algorithm`` are the verifier's own
        expectations, never read from the bundle: a deserialized bundle carries
        attacker-controlled ``kms_key_id``/``signing_algorithm`` fields, and
        verifying against those would accept any bundle self-signed with a key
        the attacker owns. A mismatch between the expectations and the embedded
        fields returns ``False`` without calling KMS.

        Returns ``True`` on a valid signature, ``False`` when KMS reports
        ``KMSInvalidSignatureException`` (either a tampered ``content_hash``
        or a signature produced by a different key). Any other KMS error
        re-raises; callers should distinguish "invalid signature" from
        "could not reach KMS".

        Caveat: verifying the signature proves internal consistency and that
        the hash was signed by the expected key; it does NOT prove the bundle
        reflects production state. See the class docstring.
        """
        if self.kms_key_id != expected_key_id:
            return False
        if self.signing_algorithm != expected_algorithm:
            return False
        kms = _kms_client(boto_session)
        try:
            resp = kms.verify(
                KeyId=expected_key_id,
                Message=self.content_hash.encode("utf-8"),
                MessageType="RAW",
                Signature=self.signature,
                SigningAlgorithm=expected_algorithm,
            )
        except kms.exceptions.KMSInvalidSignatureException:
            return False
        return bool(resp.get("SignatureValid", False))

    # ------------------------------------------------------------------
    # Serialization (parent's to_json/from_json use to_dict/from_dict)
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        base = super().to_dict()
        base["kms_key_id"] = self.kms_key_id
        base["signature"] = base64.b64encode(self.signature).decode("ascii")
        base["signing_algorithm"] = self.signing_algorithm
        base["signed_at"] = self.signed_at.isoformat()
        return base

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignedExaminerBundle":
        signature_b64 = d.get("signature", "")
        signature = base64.b64decode(signature_b64) if signature_b64 else b""
        signed_at_iso = d.get("signed_at")
        signed_at = datetime.fromisoformat(signed_at_iso) if signed_at_iso else _utcnow()
        return cls(
            decision=d["decision"],
            policy=d.get("policy"),
            evidence=list(d.get("evidence") or []),
            as_of_transaction_time=d.get("as_of_transaction_time"),
            content_hash=d.get("content_hash", ""),
            schema_version=d.get("schema_version", "1"),
            metadata=dict(d.get("metadata") or {}),
            kms_key_id=d.get("kms_key_id", ""),
            signature=signature,
            signing_algorithm=d.get("signing_algorithm", "RSASSA_PSS_SHA_256"),
            signed_at=signed_at,
        )


def _kms_client(boto_session: Optional[Any]) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_BOTO3_INSTALL_HINT) from exc

    if boto_session is None:
        return boto3.client("kms")
    return boto_session.client("kms")


__all__ = ["SignedExaminerBundle"]
