"""Ed25519 key files and digest signatures for the evidence layer.

Keys use the conventions of :mod:`briefcase.integrity.signing` (32-byte
seeds, RFC 8037 OKP JWKs, RFC 7638 thumbprints as key ids) and of the Rust
core's ``integrity::signing``, so a key file works on both sides:

* a key file holds a private JWK ``{"kty":"OKP","crv":"Ed25519","d":..,"x":..}``
  (what :func:`generate_key_file` writes), 64 hex characters, or 32 raw bytes;
* a digest signature is Ed25519 over the 32 raw bytes of a hex SHA-256.

Signing and verification use PyNaCl (the ``integrity`` extra) when it is
installed, else the compiled core that ships in the wheel, else the RFC 8032
section 6 reference algorithm in pure Python (slow and not constant time:
fine for checking public data, a last resort for signing). So
``briefcase bundle verify`` needs nothing beyond the SDK.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Optional, Union

from briefcase.integrity.signing import jwk_thumbprint

KeySource = Union[str, os.PathLike, bytes]


class KeyFileError(ValueError):
    """A key file could not be read or is not an Ed25519 key."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# RFC 8032 section 6 reference arithmetic (verification and key derivation).
# ---------------------------------------------------------------------------

_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_Q = 2**252 + 27742317777372353535851937790883648493
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    C, D = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _mul(s, point):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, point)
        point = _add(point, point)
        s >>= 1
    return q


def _equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
assert _GX is not None
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(point) -> bytes:
    zinv = pow(point[2], _P - 2, _P)
    x, y = point[0] * zinv % _P, point[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _sha512_modq(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def _pure_public_key(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _compress(_mul(a, _G))


def _pure_sign(seed: bytes, message: bytes) -> bytes:
    """RFC 8032 section 6 ``sign``. Last-resort fallback only: it is slow and
    not constant time, so real installs sign with PyNaCl or the core."""
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    public = _compress(_mul(a, _G))
    r = _sha512_modq(h[32:] + message)
    r_bytes = _compress(_mul(r, _G))
    k = _sha512_modq(r_bytes + public + message)
    return r_bytes + int.to_bytes((r + k * a) % _Q, 32, "little")


def _pure_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = _decompress(public)
    r_point = _decompress(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = _sha512_modq(signature[:32] + public + message)
    return _equal(_mul(s, _G), _add(r_point, _mul(h, a_point)))


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _nacl():
    try:
        from nacl import signing

        return signing
    except ImportError:
        return None


def _native():
    try:
        from briefcase import _native

        fn = getattr(_native, "ed25519_verify_digest", None)
        # The real extension exposes builtins; anything else (a test double,
        # an older wheel) is not trusted to verify signatures.
        if type(fn).__name__ == "builtin_function_or_method":
            return _native
    except Exception:
        pass
    return None


def public_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise KeyFileError("an Ed25519 seed is 32 bytes, got %d" % len(seed))
    nacl = _nacl()
    if nacl is not None:
        return bytes(nacl.SigningKey(seed).verify_key)
    return _pure_public_key(seed)


def public_jwk(seed: bytes) -> Dict[str, str]:
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(public_key_from_seed(seed))}


def key_id(public_x: str) -> str:
    """RFC 7638 thumbprint (hex) of the Ed25519 key whose JWK ``x`` is given."""
    return jwk_thumbprint({"kty": "OKP", "crv": "Ed25519", "x": public_x})


def parse_key_bytes(data: bytes) -> bytes:
    """Seed from key-file contents: private JWK, 64 hex chars, or 32 raw bytes."""
    text: Optional[str]
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = None
    if text and text.startswith("{"):
        try:
            jwk = json.loads(text)
        except ValueError as exc:
            raise KeyFileError("not valid JSON: %s" % exc) from None
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            raise KeyFileError("JWK is not an OKP Ed25519 key")
        if "d" not in jwk:
            raise KeyFileError('JWK has no private part ("d"); this is a public key')
        seed = _unb64url(str(jwk["d"]))
        if len(seed) != 32:
            raise KeyFileError('JWK "d" is not a 32-byte seed')
        if "x" in jwk and str(jwk["x"]).rstrip("=") != public_jwk(seed)["x"]:
            raise KeyFileError('JWK "x" does not match its "d"')
        return seed
    if text is not None and len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    if len(data) == 32:
        return data
    raise KeyFileError("not a JWK, hex seed or 32-byte seed")


def load_seed(source: KeySource) -> bytes:
    """Seed from a key file path or the file's bytes."""
    if isinstance(source, bytes):
        return parse_key_bytes(source)
    path = Path(source)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise KeyFileError("cannot read key file %s: %s" % (path, exc.strerror or exc)) from None
    try:
        return parse_key_bytes(data)
    except KeyFileError as exc:
        raise KeyFileError("key file %s: %s" % (path, exc)) from None


def load_public_key(source: Union[str, os.PathLike]) -> Dict[str, str]:
    """A public JWK from a file holding a public or private JWK or seed."""
    data = Path(source).read_bytes()
    try:
        jwk = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        jwk = None
    if isinstance(jwk, dict) and "d" not in jwk:
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "x" not in jwk:
            raise KeyFileError("%s is not an Ed25519 public JWK" % source)
        return {"kty": "OKP", "crv": "Ed25519", "x": str(jwk["x"]).rstrip("=")}
    return public_jwk(parse_key_bytes(data))


def generate_key_file(path: Union[str, os.PathLike]) -> Dict[str, str]:
    """Write a new private JWK to ``path`` (owner-only, never overwrites).

    Returns the public JWK. Keep the file private; hand out the public JWK
    or its key id so verifiers can pin it.
    """
    path = Path(path)
    seed = os.urandom(32)
    jwk = public_jwk(seed)
    private = {"kty": "OKP", "crv": "Ed25519", "d": _b64url(seed), "x": jwk["x"]}
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(private) + "\n")
    return jwk


def sign_digest(seed: bytes, digest_hex: str) -> str:
    """Ed25519 over the raw digest bytes, base64url (PyNaCl or the core)."""
    nacl = _nacl()
    if nacl is not None:
        return _b64url(nacl.SigningKey(seed).sign(bytes.fromhex(digest_hex)).signature)
    native = _native()
    if native is not None:
        return native.ed25519_sign_digest(seed, digest_hex)
    return _b64url(_pure_sign(seed, bytes.fromhex(digest_hex)))


def verify_digest(digest_hex: str, signature: str, public_x: str) -> bool:
    """True iff ``signature`` signs the raw digest bytes under key ``x``.

    Never raises on malformed input (verifiers read untrusted data).
    """
    try:
        raw = bytes.fromhex(digest_hex)
        sig = _unb64url(signature)
        public = _unb64url(public_x)
    except (ValueError, TypeError):
        return False
    if len(raw) != 32 or len(sig) != 64 or len(public) != 32:
        return False
    nacl = _nacl()
    if nacl is not None:
        try:
            nacl.VerifyKey(public).verify(raw, sig)
            return True
        except Exception:
            return False
    native = _native()
    if native is not None:
        return bool(native.ed25519_verify_digest(digest_hex, signature, public_x))
    return _pure_verify(public, raw, sig)


def describe_backend() -> str:
    if _nacl() is not None:
        return "PyNaCl"
    if _native() is not None:
        return "briefcase compiled core"
    return "pure-Python RFC 8032 reference"


__all__ = [
    "KeyFileError",
    "describe_backend",
    "generate_key_file",
    "key_id",
    "load_public_key",
    "load_seed",
    "parse_key_bytes",
    "public_jwk",
    "public_key_from_seed",
    "sign_digest",
    "verify_digest",
]
