"""Ed25519 signing helpers: round trips, golden vectors, lazy dependency."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from briefcase.integrity import (
    jwk_thumbprint,
    public_key_jwk,
    sign_digest,
    sign_json,
    verify_digest,
    verify_json_signature,
)

GOLDEN = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "integrity_golden.json").read_text()
)["manifest"]

_HAS_NACL = True
try:
    import nacl  # noqa: F401
except ImportError:
    _HAS_NACL = False

requires_nacl = pytest.mark.skipif(not _HAS_NACL, reason="PyNaCl not installed")


@requires_nacl
def test_golden_manifest_signature_verifies() -> None:
    assert verify_json_signature(GOLDEN["manifest"], GOLDEN["signature_b64url"], GOLDEN["jwk"])


@requires_nacl
def test_golden_manifest_signature_is_reproduced() -> None:
    seed = bytes.fromhex(GOLDEN["signature_seed_hex"])
    assert sign_json(GOLDEN["manifest"], seed) == GOLDEN["signature_b64url"]
    assert public_key_jwk(seed) == GOLDEN["jwk"]


def test_golden_thumbprint() -> None:
    assert jwk_thumbprint(GOLDEN["jwk"]) == GOLDEN["thumbprint"]


def test_thumbprint_ignores_extra_members_and_needs_no_nacl() -> None:
    jwk = dict(GOLDEN["jwk"], use="sig", kid="ignored")
    assert jwk_thumbprint(jwk) == GOLDEN["thumbprint"]


@requires_nacl
def test_json_sign_verify_round_trip_and_rejection() -> None:
    seed = b"\x07" * 32
    jwk = public_key_jwk(seed)
    obj = {"n": 1, "s": "café", "nested": {"b": 2, "a": 1}}
    sig = sign_json(obj, seed)
    assert verify_json_signature(obj, sig, jwk)
    assert verify_json_signature({"n": 2, "s": "café", "nested": {"a": 1, "b": 2}}, sig, jwk) is False
    assert verify_json_signature(obj, sig, public_key_jwk(b"\x08" * 32)) is False


@requires_nacl
def test_verify_is_false_not_raising_on_malformed_input() -> None:
    assert verify_json_signature({"a": 1}, "!!not-base64!!", GOLDEN["jwk"]) is False
    assert verify_json_signature({"a": 1}, GOLDEN["signature_b64url"], {"x": "short"}) is False
    assert verify_json_signature({"a": 1}, GOLDEN["signature_b64url"], {}) is False


@requires_nacl
def test_digest_sign_verify_round_trip() -> None:
    seed = b"\x09" * 32
    digest = "ab" * 32
    sig = sign_digest(seed, digest)
    jwk = public_key_jwk(seed)
    assert verify_digest(digest, sig, jwk)
    assert verify_digest("cd" * 32, sig, jwk) is False


@requires_nacl
def test_sign_json_is_strict_about_content() -> None:
    from datetime import datetime, timezone

    with pytest.raises(TypeError):
        sign_json({"when": datetime(2026, 1, 1, tzinfo=timezone.utc)}, b"\x07" * 32)


@requires_nacl
def test_seed_length_is_validated() -> None:
    with pytest.raises(ValueError):
        sign_digest(b"\x01" * 16, "ab" * 32)
    with pytest.raises(ValueError):
        public_key_jwk(b"\x01" * 31)


def test_missing_nacl_raises_actionable_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object):
        if name == "nacl" or name.startswith("nacl."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ImportError, match="briefcase-ai\\[integrity\\]"):
        sign_digest(b"\x01" * 32, "ab" * 32)
