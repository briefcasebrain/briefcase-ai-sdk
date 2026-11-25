"""Canonical JSON encodings used for hashing and signing.

Three profiles. The first two are chosen by byte-compatibility with data
already in the wild; the third is RFC 8785 and agrees across languages:

``canonical_json``
    The recommended profile: UTF-8 bytes, sorted keys, tight separators,
    NaN/Infinity rejected. Strict by default (non-serializable values
    raise ``TypeError``); pass ``fallback=str`` to stringify them instead.

``canonical_json_compat``
    The legacy profile used by ``BitemporalRecord.content_hash`` and the
    examiner bundle: a ``str`` with ASCII-escaped non-ASCII characters and
    silent ``default=str`` coercion. It exists so those call sites keep
    producing byte-identical hashes; do not use it for new code.

``canonical_json_v2`` (hash scheme ``"jcs-1"``)
    RFC 8785 JSON Canonicalization Scheme: object members sorted by UTF-16
    code units, ECMAScript ``Number.prototype.toString`` number formatting,
    ECMAScript string escaping, UTF-8 bytes. Byte-identical to the
    TypeScript ``canonicalJsonV2`` and the Rust ``canonical_json_v2`` for
    every value all three accept (``tests/vectors/canonical_json_v2.json``).

The first two profiles are not RFC 8785: Python's float repr is not ES
number-to-string, so they agree with other languages only for values built
from strings, integers, booleans, ``None``, and containers of the same.
Every structure that carries a hash records the scheme that produced it
(``hash_scheme``): :data:`HASH_SCHEME_V1` (``"json-sorted-1"``, the
``canonical_json`` profile) or :data:`HASH_SCHEME_V2` (``"jcs-1"``).

Integers in ``jcs-1``. RFC 8785 numbers are IEEE-754 doubles. An integer
whose value a double holds exactly (every integer in +/-(2**53 - 1), and
larger ones such as ``2**60``) is serialized as that double; any other
integer raises ``ValueError`` instead of being silently rounded. Encode
large identifiers and exact decimals as strings. The rule depends only on
the value, so Python ``int``, Rust ``i64``/``u64`` and JavaScript ``bigint``
agree.

Reading canonical text back: ECMAScript writes the double ``2**60`` as
``1152921504606847000``, which is not that double's exact integer value, so
``json.loads`` would return an ``int`` this module then refuses. Verifiers
read canonical text with :func:`parse_canonical_text`, which treats every
number beyond +/-(2**53 - 1) as a double, as ``JSON.parse`` does.
"""

from __future__ import annotations

import hashlib
import json
import math
from json.encoder import encode_basestring as _encode_basestring
from typing import Any, Callable, Dict, Optional

HASH_SCHEME_V1 = "json-sorted-1"
HASH_SCHEME_V2 = "jcs-1"
HASH_SCHEMES = (HASH_SCHEME_V1, HASH_SCHEME_V2)
MAX_SAFE_INTEGER = 2**53 - 1


def canonical_json(obj: Any, *, fallback: Optional[Callable[[Any], Any]] = None) -> bytes:
    """Deterministic JSON encoding: sorted keys, no whitespace, UTF-8 bytes.

    Raises ``ValueError`` on NaN/Infinity and ``TypeError`` on values JSON
    cannot represent, unless ``fallback`` supplies a serializer for them.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=fallback,
    ).encode("utf-8")


def canonical_json_compat(obj: Any) -> str:
    """Legacy canonical encoding: ASCII-escaped ``str`` with ``default=str``.

    Byte-compatible with hashes computed by earlier releases; kept only for
    those call sites. New code should use :func:`canonical_json`.
    """
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


# --------------------------------------------------------------------------
# RFC 8785 (JCS)
# --------------------------------------------------------------------------

def _jcs_string(value: str) -> str:
    """RFC 8785 section 3.2.2.2 (ECMA-262 ``QuoteJSONString``).

    The stdlib's ``encode_basestring`` applies exactly these rules: ``"``
    and ``\\`` escaped, U+0008/9/A/C/D as ``\\b \\t \\n \\f \\r``, every
    other U+0000..U+001F as lowercase ``\\u00hh``, and everything else
    (DEL, U+2028, astral characters) as is. Lone surrogates pass through it
    and are rejected when the result is UTF-8 encoded.
    """
    return _encode_basestring(value)


def es_number_to_string(value: float) -> str:
    """ECMAScript ``Number::toString(x)`` (ECMA-262 7.1.12.1, with Note 2).

    ``repr`` gives the shortest digit string that round-trips to the same
    double, choosing the closest such string (David Gay's algorithm), which
    is exactly the ``k``/``s`` of the ECMAScript algorithm. Only the layout
    rules (plain, fraction, leading zeros, exponent) differ, and they are
    applied here.
    """
    if math.isnan(value) or math.isinf(value):
        raise ValueError("jcs-1 numbers must be finite (NaN and Infinity are not JSON)")
    if value == 0:
        return "0"  # covers -0.0 too
    if value < 0:
        return "-" + es_number_to_string(-value)
    text = repr(float(value))
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    int_part, _, frac_part = mantissa.partition(".")
    raw = int_part + frac_part
    stripped = raw.lstrip("0")
    # value == 0.<digits> * 10**n
    n = len(int_part) + exponent - (len(raw) - len(stripped))
    digits = stripped.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    sign = "+" if e >= 0 else "-"
    if k == 1:
        return digits + "e" + sign + str(abs(e))
    return digits[0] + "." + digits[1:] + "e" + sign + str(abs(e))


def _jcs_integer(value: int) -> str:
    if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        return es_number_to_string(float(value))
    try:
        as_double = float(value)
    except OverflowError:
        as_double = math.inf
    if math.isinf(as_double) or int(as_double) != value:
        raise ValueError(
            "jcs-1 cannot represent the integer %d exactly (RFC 8785 numbers are IEEE-754 "
            "doubles); encode it as a string" % value
        )
    return es_number_to_string(as_double)


def _utf16_key(key: str) -> bytes:
    # Big-endian UTF-16 bytes compare exactly like sequences of UTF-16 code
    # units, the order RFC 8785 section 3.2.3 requires.
    return key.encode("utf-16-be", "surrogatepass")


def _jcs_encode(obj: Any, fallback: Optional[Callable[[Any], Any]], seen: Dict[int, None]) -> str:
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, str):
        return _jcs_string(obj)
    if isinstance(obj, int):
        return _jcs_integer(int(obj))
    if isinstance(obj, float):
        return es_number_to_string(float(obj))
    if isinstance(obj, (list, tuple, dict)):
        marker = id(obj)
        if marker in seen:
            raise ValueError("jcs-1 values must not be circular")
        seen[marker] = None
        try:
            if isinstance(obj, dict):
                items = []
                for key in obj:
                    if not isinstance(key, str):
                        raise TypeError(
                            "jcs-1 object keys must be strings, got %s" % type(key).__name__
                        )
                    items.append(key)
                items.sort(key=_utf16_key)
                return "{" + ",".join(
                    _jcs_string(key) + ":" + _jcs_encode(obj[key], fallback, seen) for key in items
                ) + "}"
            return "[" + ",".join(_jcs_encode(item, fallback, seen) for item in obj) + "]"
        finally:
            del seen[marker]
    if fallback is not None:
        return _jcs_encode(fallback(obj), None, seen)
    raise TypeError("Object of type %s is not JSON serializable" % type(obj).__name__)


def canonical_json_v2(obj: Any, *, fallback: Optional[Callable[[Any], Any]] = None) -> bytes:
    """RFC 8785 (JCS) canonical encoding, hash scheme ``"jcs-1"``.

    Raises ``ValueError`` on NaN/Infinity, lone surrogates, circular
    containers and integers a double cannot hold exactly, and ``TypeError``
    on non-string keys and values JSON cannot represent unless ``fallback``
    supplies a serializer for them (its result is encoded instead).
    """
    text = _jcs_encode(obj, fallback, {})
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "jcs-1 strings must be well-formed Unicode (lone surrogate at offset %d)" % exc.start
        ) from None


def _int_or_double(text: str) -> Any:
    value = int(text)
    return value if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else float(text)


def parse_canonical_text(text: Any) -> Any:
    """``json.loads`` with numbers beyond +/-(2**53 - 1) read as doubles.

    For re-canonicalizing stored ``jcs-1`` text only; on fresh data it would
    hide exactly the rounding ``canonical_json_v2`` refuses.
    """
    return json.loads(text, parse_int=_int_or_double)


def canonical_bytes(
    obj: Any, scheme: str, *, fallback: Optional[Callable[[Any], Any]] = None
) -> bytes:
    """Encode ``obj`` with the canonical profile named by ``scheme``.

    Verifiers dispatch on a structure's recorded ``hash_scheme`` through
    this; an unknown scheme raises ``ValueError`` rather than guessing.
    """
    if scheme == HASH_SCHEME_V1:
        return canonical_json(obj, fallback=fallback)
    if scheme == HASH_SCHEME_V2:
        return canonical_json_v2(obj, fallback=fallback)
    raise ValueError("unknown hash scheme %r (known: %s)" % (scheme, ", ".join(HASH_SCHEMES)))


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "HASH_SCHEMES",
    "HASH_SCHEME_V1",
    "HASH_SCHEME_V2",
    "MAX_SAFE_INTEGER",
    "canonical_bytes",
    "canonical_json",
    "canonical_json_compat",
    "canonical_json_v2",
    "es_number_to_string",
    "parse_canonical_text",
    "sha256_hex",
]
