"""Generate or check ``canonical_json.json``, the cross-language golden vectors.

The vectors pin the subset of values on which the Python and TypeScript
canonical-JSON implementations agree byte for byte. The expected output is
always produced by the Python implementation (``briefcase.integrity``); the
TypeScript suite (``js/runtime/test/canonical-vectors.test.ts``) asserts the
same bytes and hashes.

    python tests/vectors/canonical_json_vectors.py            # check (CI)
    python tests/vectors/canonical_json_vectors.py --write    # regenerate

Vector inputs are plain JSON, except for two tagged forms that JSON cannot
carry losslessly: ``{"$bigint": "<digits>"}`` is an integer of any size and
``{"$date": "<RFC 3339 UTC>"}`` is an instant (a timezone-aware UTC
``datetime`` here, a ``Date`` in TypeScript).

Profiles: ``strict`` is ``canonical_json(obj)``; ``payload`` is the encoding
``compute_payload_hash`` hashes, ``canonical_json(obj, fallback=str)``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VECTORS = HERE / "canonical_json.json"


def _load_canonical_json():
    """Return ``canonical_json``, without the native extension if need be."""
    sys.path.insert(0, str(REPO))
    try:
        from briefcase.integrity.canonical import canonical_json

        return canonical_json
    except ImportError:
        # ``import briefcase`` needs the compiled extension; the canonical
        # module itself is pure Python, so load it straight from its file.
        spec = importlib.util.spec_from_file_location(
            "_briefcase_integrity_canonical", REPO / "briefcase" / "integrity" / "canonical.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.canonical_json


def big(digits: str) -> Dict[str, str]:
    return {"$bigint": digits}


def date(text: str) -> Dict[str, str]:
    return {"$date": text}


# (name, profile, input). Keep names unique; append rather than reorder.
CASES: List[Any] = [
    ("null", "strict", None),
    ("true", "strict", True),
    ("false", "strict", False),
    ("zero", "strict", 0),
    ("negative-integer", "strict", -42),
    ("max-safe-integer", "strict", 9007199254740991),
    ("min-safe-integer", "strict", -9007199254740991),
    ("empty-string", "strict", ""),
    ("ascii-string", "strict", "hello world"),
    ("string-escapes", "strict", "quote\" backslash\\ slash/ nl\n cr\r tab\t bs\b ff\f"),
    ("control-characters", "strict", "\x00\x01\x1f\x7f"),
    ("latin-and-cjk", "strict", "café 日本語"),
    ("line-separators", "strict", "a b c"),
    ("astral-string", "strict", "smile \U0001f600 end"),
    ("html-sensitive", "strict", "<script>&'</script>"),
    ("empty-array", "strict", []),
    ("empty-object", "strict", {}),
    ("mixed-array", "strict", [1, "two", None, True, [3, [4]], {"five": 5}]),
    ("key-order-ascii", "strict", {"b": 2, "a": 1, "B": 3, "aa": 4, "": 5, "10": 6, "9": 7, "_": 8}),
    ("key-order-prefix", "strict", {"abc": 1, "ab": 2, "a": 3, "abd": 4}),
    ("key-order-bmp", "strict", {"é": 1, "z": 2, "日": 3, "à": 4, "퟿": 5, "": 6, "￿": 7}),
    # U+FFFF sorts before U+10000 by code point but after it by UTF-16 unit.
    ("key-order-astral-vs-bmp", "strict", {"\U00010000": 1, "￿": 2, "": 3, "\U0001f600": 4, "a": 5}),
    ("key-order-astral-suffix", "strict", {"k\U0001f600": 1, "k～": 2, "k": 3}),
    ("key-needs-escaping", "strict", {"a\"b": 1, "a\\b": 2, "a\nb": 3}),
    (
        "nested",
        "strict",
        {"z": {"y": {"x": [{"b": 1, "a": 2}, []]}}, "a": [{}, {"k": None}], "m": "v"},
    ),
    ("numeric-strings", "strict", {"amount": "19.99", "rate": "1e-7", "big": "12345678901234567890"}),
    ("existing-fixture-payload", "strict", {"a": 1, "b": "two", "nested": {"x": 1, "y": 2}, "unicode": "café"}),
    ("bigint-beyond-safe", "strict", {"n": big("9007199254740993")}),
    ("bigint-negative", "strict", [big("-123456789012345678901234567890")]),
    ("bigint-small", "strict", big("7")),
    ("payload-plain", "payload", {"id": "row-1", "count": 3, "tags": ["a", "b"], "ok": True, "none": None}),
    ("payload-date-whole-second", "payload", {"at": date("2026-01-01T00:00:00Z")}),
    ("payload-date-milliseconds", "payload", {"at": date("2026-01-01T00:00:01.001Z")}),
    ("payload-date-nested", "payload", {"events": [date("1999-12-31T23:59:59.999Z"), date("0999-03-04T05:06:07Z")]}),
]


def decode(value: Any) -> Any:
    """Turn the tagged JSON form into the Python value it stands for."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$bigint"}:
            return int(value["$bigint"])
        if set(value) == {"$date"}:
            return datetime.fromisoformat(value["$date"].replace("Z", "+00:00")).astimezone(timezone.utc)
        return {key: decode(item) for key, item in value.items()}
    return value


def build() -> Dict[str, Any]:
    canonical_json = _load_canonical_json()
    vectors = []
    for name, profile, raw in CASES:
        obj = decode(raw)
        blob = canonical_json(obj, fallback=str) if profile == "payload" else canonical_json(obj)
        vectors.append(
            {
                "name": name,
                "profile": profile,
                "input": raw,
                "canonical": blob.decode("utf-8"),
                "canonical_utf8_hex": blob.hex(),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
    assert len({vector["name"] for vector in vectors}) == len(vectors), "duplicate vector name"
    return {
        "description": (
            "Golden vectors for the canonical-JSON subset on which briefcase.integrity (Python) and "
            "@briefcase-ai/runtime/integrity (TypeScript) agree. Generated by "
            "tests/vectors/canonical_json_vectors.py from the Python implementation; do not edit by hand."
        ),
        "tags": {
            "$bigint": "decimal digits of an integer of any size (Python int, JS bigint)",
            "$date": "RFC 3339 UTC instant (timezone-aware UTC datetime, JS Date)",
        },
        "profiles": {
            "strict": "canonical_json(obj) / canonicalJson(value)",
            "payload": "canonical_json(obj, fallback=str) / canonicalJson(value, { fallback: pythonStrFallback })",
        },
        "vectors": vectors,
    }


def render(document: Dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def main(argv: List[str]) -> int:
    expected = render(build())
    if "--write" in argv:
        VECTORS.write_text(expected, encoding="utf-8")
        print("wrote {} ({} vectors)".format(VECTORS, len(CASES)))
        return 0
    actual = VECTORS.read_text(encoding="utf-8") if VECTORS.exists() else ""
    if actual != expected:
        print(
            "{} is out of date with the Python canonical JSON implementation; "
            "run this script with --write only if the change is intended "
            "(it changes every stored hash).".format(VECTORS),
            file=sys.stderr,
        )
        return 1
    print("{}: {} vectors match the Python implementation".format(VECTORS.name, len(CASES)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
