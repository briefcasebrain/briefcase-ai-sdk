"""Pluggable bundle signers and the verifiers that check them.

A signer turns the bytes a bundle asks to have signed into a signature
record that is stored in the bundle's manifest::

    {"type": "ed25519", "key_id": ..., "algorithm": "Ed25519", "value": ...,
     "public_key": <JWK x>, "signed_at": ...}
    {"type": "aws-kms", "key_id": <KMS key id or ARN>,
     "algorithm": "RSASSA_PSS_SHA_256", "value": <base64>, "signed_at": ...}

Verification is dispatched on ``type`` through :func:`register_verifier`.
Every verifier pins the key on the verifier's side: a signature record
carries attacker-controlled ``key_id``/``algorithm`` fields, and checking a
signature against the key the bundle itself names would accept any bundle
an attacker self-signed. An Ed25519 signature can still be checked
cryptographically against its embedded public key (the result says so and
names the key, so a person can compare it with the published one); a KMS
signature is only checked against a pinned key id, through KMS.

Add a signer by implementing :class:`Signer` (``type`` plus ``sign``) and,
for verification, registering a :class:`SignatureVerifier` for that type.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Sequence, Union, runtime_checkable

from briefcase.integrity import keys

KMS_DEFAULT_ALGORITHM = "RSASSA_PSS_SHA_256"
_BOTO3_INSTALL_HINT = (
    "boto3 is required for AWS KMS signatures. Install with "
    "'pip install briefcase-ai[compliance-kms]' or 'pip install boto3'."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class Signer(Protocol):
    """Anything that can sign a bundle. ``type`` names its verifier."""

    type: str

    def sign(self, message: bytes) -> Dict[str, Any]:
        """Return the signature record (must include ``type`` and ``value``)."""
        ...


@dataclass
class Pins:
    """What the verifier trusts, supplied by the verifier, never the bundle.

    ``key_ids`` holds accepted Ed25519 key ids (RFC 7638 thumbprints) and/or
    KMS key ids/ARNs; ``kms_algorithms`` the accepted KMS algorithms.
    ``kms_client`` (or ``boto_session``) enables KMS verification, which is
    a network call to AWS: nothing reaches KMS unless one is given.
    """

    key_ids: Sequence[str] = ()
    kms_algorithms: Sequence[str] = (KMS_DEFAULT_ALGORITHM,)
    kms_client: Any = None
    boto_session: Any = None

    def kms(self) -> Any:
        if self.kms_client is not None:
            return self.kms_client
        if self.boto_session is not None:
            return self.boto_session.client("kms")
        return None


@dataclass
class SignatureCheck:
    """Outcome of checking one signature record."""

    status: str  # "valid" | "invalid" | "unchecked" | "untrusted" (valid, but not by a pinned key)
    type: str
    key_id: Optional[str]
    detail: str = ""
    pinned: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class SignatureVerifier(Protocol):
    def __call__(self, signature: Dict[str, Any], message: bytes, pins: Pins) -> SignatureCheck: ...


_VERIFIERS: Dict[str, SignatureVerifier] = {}


def register_verifier(signature_type: str, verifier: SignatureVerifier) -> None:
    """Make :func:`check_signature` understand signatures of ``signature_type``."""
    _VERIFIERS[signature_type] = verifier


def check_signature(signature: Dict[str, Any], message: bytes, pins: Pins) -> SignatureCheck:
    kind = str(signature.get("type", ""))
    verifier = _VERIFIERS.get(kind)
    if verifier is None:
        return SignatureCheck(
            "unchecked", kind, signature.get("key_id"),
            "signature type %r is not supported by this SDK; upgrade briefcase-ai" % kind,
        )
    return verifier(signature, message, pins)


# ---------------------------------------------------------------------------
# Ed25519 (key file / JWK)
# ---------------------------------------------------------------------------


class Ed25519Signer:
    """Ed25519 with a local key (path to a key file, or its bytes / a seed).

    Keys are those of :mod:`briefcase.integrity.keys` (``briefcase-ai bundle
    keygen``); the signature covers ``message`` as given.
    """

    type = "ed25519"

    def __init__(self, key: Union[str, bytes, Any]) -> None:
        self._seed = keys.load_seed(key)
        self.public_jwk = keys.public_jwk(self._seed)
        self.key_id = keys.key_id(self.public_jwk["x"])

    def sign(self, message: bytes) -> Dict[str, Any]:
        if len(message) == 32:
            value = keys.sign_digest(self._seed, message.hex())
        else:  # pragma: no cover - bundles always sign a 32-byte digest
            raise ValueError("Ed25519Signer signs 32-byte digests")
        return {
            "type": self.type,
            "algorithm": "Ed25519",
            "key_id": self.key_id,
            "public_key": self.public_jwk["x"],
            "value": value,
            "signed_at": _now(),
        }


def _verify_ed25519(signature: Dict[str, Any], message: bytes, pins: Pins) -> SignatureCheck:
    kid, x, value = signature.get("key_id"), signature.get("public_key"), signature.get("value")
    if not (kid and x and value) or len(message) != 32:
        return SignatureCheck("invalid", "ed25519", kid, "incomplete Ed25519 signature record")
    if keys.key_id(str(x)) != kid:
        return SignatureCheck("invalid", "ed25519", kid, "the signature names a different key than it carries")
    if not keys.verify_digest(message.hex(), str(value), str(x)):
        return SignatureCheck("invalid", "ed25519", kid, "Ed25519 signature does not verify")
    pinned = bool(pins.key_ids)
    if pinned and kid not in pins.key_ids:
        return SignatureCheck("untrusted", "ed25519", kid,
                              "valid, but by key %s, which is not one you pinned" % kid[:12], pinned=True)
    return SignatureCheck("valid", "ed25519", kid, "", pinned=pinned)


register_verifier("ed25519", _verify_ed25519)


# ---------------------------------------------------------------------------
# AWS KMS
# ---------------------------------------------------------------------------


def kms_client_from(boto_session: Any = None) -> Any:
    """``boto3`` KMS client (lazy import; boto3 is the ``compliance-kms`` extra)."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_BOTO3_INSTALL_HINT) from exc
    if boto_session is None:
        return boto3.client("kms")
    return boto_session.client("kms")


class KmsSigner:
    """AWS KMS asymmetric signing (``MessageType="RAW"``).

    Same metadata as :class:`~briefcase.compliance.SignedExaminerBundle`:
    the KMS key id/ARN and the signing algorithm (default
    ``RSASSA_PSS_SHA_256``). Pass ``kms_client`` (any object with boto3's
    ``sign``/``verify``) or ``boto_session``; otherwise a default boto3
    client is created on first use.
    """

    type = "aws-kms"

    def __init__(
        self,
        key_id: str,
        *,
        algorithm: str = KMS_DEFAULT_ALGORITHM,
        kms_client: Any = None,
        boto_session: Any = None,
    ) -> None:
        self.key_id = key_id
        self.algorithm = algorithm
        self._client = kms_client
        self._session = boto_session

    def _kms(self) -> Any:
        if self._client is None:
            self._client = kms_client_from(self._session)
        return self._client

    def sign_raw(self, message: bytes) -> bytes:
        resp = self._kms().sign(
            KeyId=self.key_id, Message=message, MessageType="RAW", SigningAlgorithm=self.algorithm,
        )
        return bytes(resp["Signature"])

    def sign(self, message: bytes) -> Dict[str, Any]:
        return {
            "type": self.type,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "value": base64.b64encode(self.sign_raw(message)).decode("ascii"),
            "signed_at": _now(),
        }


def kms_verify_raw(
    kms: Any, *, key_id: str, algorithm: str, message: bytes, signature: bytes
) -> bool:
    """``True``/``False`` for a KMS signature; other KMS errors re-raise."""
    try:
        resp = kms.verify(
            KeyId=key_id, Message=message, MessageType="RAW", Signature=signature, SigningAlgorithm=algorithm,
        )
    except kms.exceptions.KMSInvalidSignatureException:
        return False
    return bool(resp.get("SignatureValid", False))


def _verify_kms(signature: Dict[str, Any], message: bytes, pins: Pins) -> SignatureCheck:
    kid, algorithm = signature.get("key_id"), signature.get("algorithm")
    if not pins.key_ids:
        return SignatureCheck(
            "unchecked", "aws-kms", kid,
            "AWS KMS signature by %s not checked: pass the expected KMS key id (--key) and allow KMS calls (--kms)"
            % kid,
        )
    if kid not in pins.key_ids:
        return SignatureCheck("untrusted", "aws-kms", kid,
                              "not checked: KMS key %s is not one you pinned" % kid, pinned=True)
    if algorithm not in pins.kms_algorithms:
        return SignatureCheck("invalid", "aws-kms", kid, "unexpected KMS signing algorithm %s" % algorithm,
                              pinned=True)
    kms = pins.kms()
    if kms is None:
        return SignatureCheck("unchecked", "aws-kms", kid,
                              "AWS KMS signature not checked: KMS calls were not allowed (--kms)", pinned=True)
    try:
        raw = base64.b64decode(str(signature.get("value", "")), validate=True)
    except (ValueError, TypeError):
        return SignatureCheck("invalid", "aws-kms", kid, "signature is not base64", pinned=True)
    ok = kms_verify_raw(kms, key_id=str(kid), algorithm=str(algorithm), message=message, signature=raw)
    return SignatureCheck("valid" if ok else "invalid", "aws-kms", kid,
                          "" if ok else "AWS KMS reports the signature invalid", pinned=True)


register_verifier("aws-kms", _verify_kms)


def signer_from_spec(spec: str, *, kms_client: Any = None) -> Signer:
    """CLI helper: ``kms:<key id or ARN>`` is a KMS signer, anything else a key file."""
    if spec.startswith("kms:"):
        return KmsSigner(spec[4:], kms_client=kms_client)
    return Ed25519Signer(spec)


__all__ = [
    "Ed25519Signer",
    "KMS_DEFAULT_ALGORITHM",
    "KmsSigner",
    "Pins",
    "SignatureCheck",
    "SignatureVerifier",
    "Signer",
    "check_signature",
    "kms_client_from",
    "kms_verify_raw",
    "register_verifier",
    "signer_from_spec",
]
