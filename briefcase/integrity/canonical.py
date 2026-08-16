"""Canonical JSON encodings used for hashing and signing.

Two profiles, chosen by byte-compatibility with data already in the wild:

``canonical_json``
    The recommended profile: UTF-8 bytes, sorted keys, tight separators,
    NaN/Infinity rejected. Strict by default (non-serializable values
    raise ``TypeError``); pass ``fallback=str`` to stringify them instead.

``canonical_json_compat``
    The legacy profile used by ``BitemporalRecord.content_hash`` and the
    examiner bundle: a ``str`` with ASCII-escaped non-ASCII characters and
    silent ``default=str`` coercion. It exists so those call sites keep
    producing byte-identical hashes; do not use it for new code.

Neither profile is full RFC 8785 (JCS): Python's float repr is not ES6
number-to-string, so JCS byte-compatibility holds only for values built
from strings, integers, booleans, ``None``, and containers of the same.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional


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


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


__all__ = ["canonical_json", "canonical_json_compat", "sha256_hex"]
