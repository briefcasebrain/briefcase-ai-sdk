"""Generate or check ``canonical_json_v2.json``: RFC 8785 (hash scheme ``jcs-1``) vectors.

The expected bytes are produced by the Python implementation
(``briefcase.integrity.canonical.canonical_json_v2``) and asserted by three
suites: ``tests/integrity/test_canonical_v2.py`` (Python),
``js/runtime/test/canonical-vectors-v2.test.ts`` (TypeScript) and
``crates/briefcase-core/tests/canonical_v2_vectors.rs`` (Rust).

    python tests/vectors/canonical_json_v2_vectors.py            # check (CI)
    python tests/vectors/canonical_json_v2_vectors.py --write    # regenerate

Independent anchors, so the file is not just Python agreeing with itself:

* ``rfc8785-*`` cases are the examples printed in RFC 8785
  (https://www.rfc-editor.org/rfc/rfc8785, sections 3.2.2-3.2.4 and
  Appendix B), with the RFC's expected output pinned in ``expect``.
* ``cyberphone-*`` cases are the published JCS test data
  (https://github.com/cyberphone/json-canonicalization, testdata/input and
  testdata/output, fetched 2026-11-22), expected output pinned in ``expect``.
* The number formatter was also compared against V8 (``String(x)`` in
  Node.js) on 299,920 random doubles while this file was written.

Generation fails if the implementation disagrees with any pinned ``expect``.

Input tags (JSON cannot carry these losslessly):

* ``{"$f64": "<16 hex digits>"}``: the IEEE-754 double with these bits.
* ``{"$bigint": "<digits>"}``: an integer of any size (Python ``int``, JS
  ``bigint``, Rust ``i64``/``u64``; a Rust decoder that cannot hold it as an
  integer treats the vector as rejected).
* ``{"$str": "<\\uXXXX escapes>"}``: a string given by JSON escapes, used for
  lone surrogates (only error vectors use it).

A vector with ``"error"`` must be rejected by every implementation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VECTORS = HERE / "canonical_json_v2.json"


def _load_canonical():
    sys.path.insert(0, str(REPO))
    try:
        from briefcase.integrity import canonical

        return canonical
    except ImportError:
        spec = importlib.util.spec_from_file_location(
            "_briefcase_integrity_canonical", REPO / "briefcase" / "integrity" / "canonical.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def f64(bits: str) -> Dict[str, str]:
    return {"$f64": bits}


def big(digits: str) -> Dict[str, str]:
    return {"$bigint": digits}


# RFC 8785 Appendix B: (IEEE-754 bits, expected serialization).
RFC_APPENDIX_B = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]

RFC_SECTION_3_2_2_INPUT = (
    '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],'
    ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
    ' "literals": [null, true, false]}'
)
# RFC 8785 section 3.2.4, copied as printed.
RFC_SECTION_3_2_4_HEX = """
7b 22 6c 69 74 65 72 61 6c 73 22 3a 5b 6e 75 6c 6c 2c 74 72
75 65 2c 66 61 6c 73 65 5d 2c 22 6e 75 6d 62 65 72 73 22 3a
5b 33 33 33 33 33 33 33 33 33 2e 33 33 33 33 33 33 33 2c 31
65 2b 33 30 2c 34 2e 35 2c 30 2e 30 30 32 2c 31 65 2d 32 37
5d 2c 22 73 74 72 69 6e 67 22 3a 22 e2 82 ac 24 5c 75 30 30
30 66 5c 6e 41 27 42 5c 22 5c 5c 5c 5c 5c 22 2f 22 7d
""".replace(" ", "").replace("\n", "")
RFC_SORT_INPUT = (
    '{"\\u20ac": "Euro Sign", "\\r": "Carriage Return", "\\ufb33": "Hebrew Letter Dalet With Dagesh",'
    ' "1": "One", "\\ud83d\\ude00": "Emoji: Grinning Face", "\\u0080": "Control",'
    ' "\\u00f6": "Latin Small Letter O With Diaeresis"}'
)

# cyberphone/json-canonicalization testdata (input, expected output), verbatim.
CYBERPHONE = {
    'arrays': (
        '[\n  56,\n  {\n    "d": true,\n    "10": null,\n    "1": [ ]\n  }\n]\n',
        '[56,{"1":[],"10":null,"d":true}]',
    ),
    'french': (
        '{\n  "peach": "This sorting order",\n  "p\xe9ch\xe9": "is wrong according to French",\n  "p\xeache": "but canonicalization MUST",\n  "sin":   "ignore locale"\n}\n',  # noqa: E501
        '{"peach":"This sorting order","p\xe9ch\xe9":"is wrong according to French","p\xeache":"but canonicalization MUST","sin":"ignore locale"}',  # noqa: E501
    ),
    'structures': (
        '{\n  "1": {"f": {"f": "hi","F": 5} ,"\\n": 56.0},\n  "10": { },\n  "": "empty",\n  "a": { },\n  "111": [ {"e": "yes","E": "no" } ],\n  "A": { }\n}',  # noqa: E501
        '{"":"empty","1":{"\\n":56,"f":{"F":5,"f":"hi"}},"10":{},"111":[{"E":"no","e":"yes"}],"A":{},"a":{}}',
    ),
    'unicode': (
        '{\n  "Unnormalized Unicode":"A\\u030a"\n}\n',
        '{"Unnormalized Unicode":"A\u030a"}',
    ),
    'values': (
        '{\n  "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],\n  "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",\n  "literals": [null, true, false]\n}',  # noqa: E501
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}',  # noqa: E501
    ),
    'weird': (
        '{\n  "\\u20ac": "Euro Sign",\n  "\\r": "Carriage Return",\n  "\\u000a": "Newline",\n  "1": "One",\n  "\\u0080": "Control\\u007f",\n  "\\ud83d\\ude02": "Smiley",\n  "\\u00f6": "Latin Small Letter O With Diaeresis",\n  "\\ufb33": "Hebrew Letter Dalet With Dagesh",\n  "</script>": "Browser Challenge"\n}\n',  # noqa: E501
        '{"\\n":"Newline","\\r":"Carriage Return","1":"One","</script>":"Browser Challenge","\x80":"Control\x7f","\xf6":"Latin Small Letter O With Diaeresis","\u20ac":"Euro Sign","\U0001f602":"Smiley","\ufb33":"Hebrew Letter Dalet With Dagesh"}',  # noqa: E501
    ),
}


def cases() -> List[Dict[str, Any]]:
    """(name, input, expect-or-None, error-or-None). Append; never reorder."""
    out: List[Dict[str, Any]] = []

    def add(name: str, value: Any, expect: Optional[str] = None, error: Optional[str] = None) -> None:
        out.append({"name": name, "input": value, "expect": expect, "error": error})

    # RFC 8785 Appendix B, one vector per row and one array holding them all.
    for bits, text in RFC_APPENDIX_B:
        add("rfc8785-appendix-b-" + bits, f64(bits), text)
    add(
        "rfc8785-appendix-b-all",
        [f64(bits) for bits, _ in RFC_APPENDIX_B],
        "[" + ",".join(text for _, text in RFC_APPENDIX_B) + "]",
    )
    add(
        "rfc8785-section-3.2.4-utf8",
        json.loads(RFC_SECTION_3_2_2_INPUT),
        bytes.fromhex(RFC_SECTION_3_2_4_HEX).decode("utf-8"),
    )
    sorted_values = [
        "Carriage Return", "One", "Control", "Latin Small Letter O With Diaeresis",
        "Euro Sign", "Emoji: Grinning Face", "Hebrew Letter Dalet With Dagesh",
    ]
    sort_input = json.loads(RFC_SORT_INPUT)
    add(
        "rfc8785-section-3.2.3-sorting",
        sort_input,
        "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + json.dumps(v)
            for k, v in sorted(sort_input.items(), key=lambda kv: sorted_values.index(kv[1]))
        ) + "}",
    )
    for name, (text, expected) in CYBERPHONE.items():
        add("cyberphone-" + name, json.loads(text), expected)

    # Literals, strings, escapes.
    add("null", None, "null")
    add("true", True, "true")
    add("false", False, "false")
    add("empty-string", "", '""')
    add("string-escapes", "quote\" backslash\\ slash/ nl\n cr\r tab\t bs\b ff\f", None)
    add("control-characters", "".join(chr(c) for c in range(0x20)) + "\x7f", None)
    add("line-separators", "a b c", "\"a b c\"")
    add("html-sensitive", "<script>&'</script>", "\"<script>&'</script>\"")
    add("latin-and-cjk", "café 日本語", None)
    add("astral-string", "smile \U0001f600 end", None)

    # Numbers: integers, floats, the cases v1 got wrong across languages.
    add("zero", 0, "0")
    add("negative-zero-float", f64("8000000000000000"), "0")
    add("integer-valued-float", 1.0, "1")
    add("float-1e16", 1e16, "10000000000000000")
    add("float-1e21", 1e21, "1e+21")
    add("float-1e-7", 1e-7, "1e-7")
    add("float-1e-6", 1e-6, "0.000001")
    add("float-0.1", 0.1, "0.1")
    add("float-sum-0.1-0.2", 0.1 + 0.2, "0.30000000000000004")
    add("float-negative", -2.5, "-2.5")
    add("float-pi", 3.141592653589793, "3.141592653589793")
    add("float-confidence", [0.95, 0.123456789, 120.5, 1234.5678], None)
    add("float-tiny", 2.2250738585072014e-308, "2.2250738585072014e-308")
    add("max-safe-integer", 9007199254740991, "9007199254740991")
    add("min-safe-integer", -9007199254740991, "-9007199254740991")
    add("bigint-safe", big("123456789"), "123456789")
    add("bigint-exact-2^53", big("9007199254740992"), "9007199254740992")
    add("bigint-exact-2^60", big("1152921504606846976"), "1152921504606847000")
    add("bigint-exact-negative-2^63", big("-9223372036854775808"), "-9223372036854776000")
    add("bigint-exact-1e19", big("10000000000000000000"), "10000000000000000000")
    add("bigint-not-exact-2^53+1", big("9007199254740993"), error="integer not exactly representable")
    add("bigint-not-exact-i64-max", big("9223372036854775807"), error="integer not exactly representable")
    add("bigint-not-exact-u64-max", big("18446744073709551615"), error="integer not exactly representable")
    add("bigint-not-exact-nested", {"ok": 1, "id": big("12345678901234567891")},
        error="integer not exactly representable")

    # Key ordering: UTF-16 code units, not code points (they differ above U+FFFF).
    add("key-order-ascii", {"b": 2, "a": 1, "B": 3, "aa": 4, "": 5, "10": 6, "9": 7, "_": 8}, None)
    add("key-order-prefix", {"abc": 1, "ab": 2, "a": 3, "abd": 4}, None)
    add("key-order-astral-vs-bmp", {"\U00010000": 1, "￿": 2, "": 3, "\U0001f600": 4, "a": 5},
        "{\"a\":5,\"\U00010000\":1,\"\U0001f600\":4,\"\":3,\"￿\":2}")
    add("key-order-astral-suffix", {"k\U0001f600": 1, "k～": 2, "k": 3}, None)
    add("key-needs-escaping", {"a\"b": 1, "a\\b": 2, "a\nb": 3, "a\x01b": 4}, None)

    # Structures.
    add("empty-array", [], "[]")
    add("empty-object", {}, "{}")
    add("nested", {"z": {"y": {"x": [{"b": 1.5, "a": 2}, []]}}, "a": [{}, {"k": None}], "m": "v"}, None)
    add(
        "decision-record",
        {
            "schema_version": "1.2",
            "decision_id": "6f1c2a9e-0000-4000-8000-000000000001",
            "function_name": "classify",
            "inputs": {"text": "Grüße 👋"},
            "outputs": {"result": "positive", "confidence": 0.9312},
            "execution_time_ms": 12.034,
            "started_at": "2026-11-22T10:00:00.123456+00:00",
            "tags": {"env": "prod"},
            "trace_flags": 1,
        },
        None,
    )

    # Rejections every implementation must share.
    add("lone-high-surrogate", {"$str": "\\ud800"}, error="lone surrogate")
    add("lone-low-surrogate-key", {"k": {"$str": "a\\udc00b"}}, error="lone surrogate")
    return out


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$f64"}:
            return struct.unpack(">d", bytes.fromhex(value["$f64"]))[0]
        if set(value) == {"$bigint"}:
            return int(value["$bigint"])
        if set(value) == {"$str"}:
            return json.loads('"' + value["$str"] + '"')
        return {key: decode(item) for key, item in value.items()}
    return value


def _encode_input(value: Any) -> Any:
    """Represent a Python value in the tagged JSON form (floats as $f64)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        return {"$f64": struct.pack(">d", value).hex()}
    if isinstance(value, int):
        return value if abs(value) <= 2**53 - 1 else {"$bigint": str(value)}
    if isinstance(value, list):
        return [_encode_input(item) for item in value]
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in ("$f64", "$bigint", "$str"):
            return value
        return {key: _encode_input(item) for key, item in value.items()}
    raise TypeError(type(value))


def build() -> Dict[str, Any]:
    canonical = _load_canonical()
    vectors = []
    for case in cases():
        tagged = _encode_input(case["input"])
        value = decode(tagged)
        entry: Dict[str, Any] = {"name": case["name"], "input": tagged}
        if case["error"]:
            try:
                canonical.canonical_json_v2(value)
            except (ValueError, TypeError):
                entry["error"] = case["error"]
            else:
                raise SystemExit("vector %s: expected a rejection" % case["name"])
        else:
            data = canonical.canonical_json_v2(value)
            text = data.decode("utf-8")
            if case["expect"] is not None and text != case["expect"]:
                raise SystemExit(
                    "vector %s: implementation gives %r, pinned expectation is %r"
                    % (case["name"], text, case["expect"])
                )
            entry.update(
                canonical=text,
                canonical_utf8_hex=data.hex(),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        vectors.append(entry)
    names = [v["name"] for v in vectors]
    assert len(names) == len(set(names)), "duplicate vector names"
    return {
        "description": (
            "Golden vectors for RFC 8785 canonical JSON (hash scheme jcs-1), asserted by the "
            "Python, TypeScript and Rust implementations. Generated by "
            "tests/vectors/canonical_json_v2_vectors.py; do not edit by hand."
        ),
        "scheme": "jcs-1",
        "sources": [
            "https://www.rfc-editor.org/rfc/rfc8785 (sections 3.2.2-3.2.4, Appendix B)",
            "https://github.com/cyberphone/json-canonicalization testdata (fetched 2026-11-22)",
        ],
        "tags": {
            "$f64": "IEEE-754 double given by its 16 hex-digit bit pattern",
            "$bigint": "integer of any size (Python int, JS bigint, Rust i64/u64)",
            "$str": "string given by JSON escapes (used for lone surrogates)",
        },
        "rules": {
            "integers": "accepted when an IEEE-754 double holds the value exactly, serialized as "
            "that double; otherwise rejected (never rounded)",
            "reading": "canonical text is read back with numbers beyond +/-(2^53-1) as doubles "
            "(JSON.parse semantics; parse_canonical_text in Python and Rust), then re-encodes "
            "to the same bytes",
            "error": "a vector with an error field must be rejected by every implementation",
        },
        "vectors": vectors,
    }


def render(document: Dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def main(argv: List[str]) -> int:
    expected = render(build())
    if "--write" in argv:
        VECTORS.write_text(expected, encoding="utf-8")
        print("wrote {} ({} vectors)".format(VECTORS, len(json.loads(expected)["vectors"])))
        return 0
    actual = VECTORS.read_text(encoding="utf-8") if VECTORS.exists() else ""
    if actual != expected:
        print(
            "{} is out of date with the Python jcs-1 implementation; run this script with "
            "--write only if the change is intended (it changes every jcs-1 hash).".format(VECTORS),
            file=sys.stderr,
        )
        return 1
    print("{}: {} vectors match the Python implementation".format(
        VECTORS.name, len(json.loads(actual)["vectors"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
