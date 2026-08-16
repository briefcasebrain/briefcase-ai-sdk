"""Local Ed25519 signing over canonical JSON and raw digests.

Complements the KMS-backed bundle signing in ``briefcase.compliance``:
these helpers need no cloud credentials, so they suit air-gapped
verification, per-writer chain attestation, and signed authorization
manifests exchanged as JSON.

Keys are 32-byte Ed25519 seeds; public keys travel as RFC 8037 OKP JWKs
(``{"kty": "OKP", "crv": "Ed25519", "x": <base64url>}``). Signatures are
base64url without padding. Signing is deterministic (RFC 8032), so a
given seed and message always produce the same bytes.

Requires the ``integrity`` extra (PyNaCl); imports are lazy so the module
loads on a bare install and fails with an actionable error on first use.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict

from briefcase.integrity.canonical import canonical_json

_INSTALL_HINT = (
    "briefcase.integrity signing requires PyNaCl. "
    "Install it with: pip install briefcase-ai[integrity]"
)


def _nacl_signing():
    try:
        from nacl import exceptions, signing
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return signing, exceptions


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    # base64url without padding is the RFC 7515 convention for JWK/JWS
    # fields; pad back to a multiple of 4 for the stdlib decoder.
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def public_key_jwk(seed: bytes) -> Dict[str, str]:
    """The OKP JWK for the verify key of a 32-byte Ed25519 seed."""
    signing, _ = _nacl_signing()
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    verify_key = signing.SigningKey(seed).verify_key
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url_encode(bytes(verify_key))}


def sign_digest(seed: bytes, digest_hex: str) -> str:
    """Sign the raw bytes of a hex digest; return base64url-nopad."""
    signing, _ = _nacl_signing()
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    raw = bytes.fromhex(digest_hex)
    return _b64url_encode(signing.SigningKey(seed).sign(raw).signature)


def verify_digest(digest_hex: str, signature_b64: str, public_key_jwk: Dict[str, Any]) -> bool:
    """True iff ``signature_b64`` is a valid signature over the digest bytes."""
    signing, _ = _nacl_signing()
    try:
        signing.VerifyKey(_b64url_decode(str(public_key_jwk["x"]))).verify(
            bytes.fromhex(digest_hex), _b64url_decode(signature_b64)
        )
        return True
    except Exception:
        return False


def sign_json(obj: Any, seed: bytes) -> str:
    """Sign the strict canonical JSON encoding of ``obj``.

    Strict means non-serializable values raise rather than being coerced:
    a signer must never silently alter what it attests to.
    """
    signing, _ = _nacl_signing()
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    return _b64url_encode(signing.SigningKey(seed).sign(canonical_json(obj)).signature)


def verify_json_signature(obj: Any, signature_b64: str, public_key_jwk: Dict[str, Any]) -> bool:
    """True iff the signature covers the canonical JSON encoding of ``obj``.

    Returns False on any failure (bad signature, malformed key or
    encoding): verifiers run against untrusted input and must not raise.
    """
    signing, _ = _nacl_signing()
    try:
        canonical = canonical_json(obj)
        sig = _b64url_decode(signature_b64)
        key_bytes = _b64url_decode(str(public_key_jwk["x"]))
        signing.VerifyKey(key_bytes).verify(canonical, sig)
        return True
    except Exception:
        return False


def jwk_thumbprint(jwk: Dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, hex) over the required members.

    For Ed25519 OKP keys the required members are ``crv``, ``kty``, ``x``
    (sorted lexicographically, no whitespace).
    """
    required = ("crv", "kty", "x")
    canonical = json.dumps(
        {k: jwk[k] for k in required if k in jwk},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "jwk_thumbprint",
    "public_key_jwk",
    "sign_digest",
    "sign_json",
    "verify_digest",
    "verify_json_signature",
]
