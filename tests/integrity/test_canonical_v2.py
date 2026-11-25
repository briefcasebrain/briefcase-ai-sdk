"""RFC 8785 (hash scheme ``jcs-1``): shared vectors, rules, and chain dispatch."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from briefcase.integrity import (
    HASH_SCHEME_V1,
    HASH_SCHEME_V2,
    HashChainAppender,
    InMemoryHashChainStore,
    JsonlHashChainStore,
    canonical_bytes,
    canonical_json,
    canonical_json_v2,
    compute_payload_hash,
    es_number_to_string,
    parse_canonical_text,
    verify_chain_segment,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors" / "canonical_json_v2.json").read_text("utf-8")
)


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$f64"}:
            return struct.unpack(">d", bytes.fromhex(value["$f64"]))[0]
        if set(value) == {"$bigint"}:
            return int(value["$bigint"])
        if set(value) == {"$str"}:
            return json.loads('"' + value["$str"] + '"')
        return {key: _decode(item) for key, item in value.items()}
    return value


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["name"])
def test_python_matches_the_shared_vectors(vector):
    value = _decode(vector["input"])
    if "error" in vector:
        with pytest.raises((ValueError, TypeError)):
            canonical_json_v2(value)
        return
    data = canonical_json_v2(value)
    assert data.decode("utf-8") == vector["canonical"]
    assert data.hex() == vector["canonical_utf8_hex"]
    assert hashlib.sha256(data).hexdigest() == vector["sha256"]
    # Read back the way verifiers do, it re-encodes to the same bytes.
    assert canonical_json_v2(parse_canonical_text(data)) == data


def test_the_vector_file_is_current():
    from tests.vectors import canonical_json_v2_vectors as gen

    assert gen.render(gen.build()) == (
        Path(gen.__file__).with_name("canonical_json_v2.json").read_text("utf-8")
    )


def test_rfc8785_appendix_b_and_v8_corner_cases():
    assert es_number_to_string(struct.unpack(">d", bytes.fromhex("43143ff3c1cb0959"))[0]) == (
        "1424953923781206.2"
    )
    assert es_number_to_string(-0.0) == "0"
    assert es_number_to_string(1e21) == "1e+21"
    assert es_number_to_string(123e-20) == "1.23e-18"
    with pytest.raises(ValueError):
        es_number_to_string(float("nan"))


def test_floats_are_where_v1_and_v2_differ():
    value = {"confidence": 1.0, "big": 1e16, "small": 1e-7}
    assert canonical_json(value) == b'{"big":1e+16,"confidence":1.0,"small":1e-07}'
    assert canonical_json_v2(value) == b'{"big":10000000000000000,"confidence":1,"small":1e-7}'


def test_integers_a_double_cannot_hold_are_refused_not_rounded():
    assert canonical_json_v2(2**53 - 1) == b"9007199254740991"
    assert canonical_json_v2(2**60) == b"1152921504606847000"
    for bad in (2**53 + 1, 2**63 - 1, 10**400):
        with pytest.raises(ValueError, match="encode it as a string"):
            canonical_json_v2({"id": bad})
    # Booleans are not integers.
    assert canonical_json_v2([True, False]) == b"[true,false]"


def test_v2_rejections_and_fallback():
    with pytest.raises(ValueError):
        canonical_json_v2("\ud800")
    with pytest.raises(TypeError):
        canonical_json_v2({1: "non-string key"})
    with pytest.raises(TypeError):
        canonical_json_v2({"at": datetime(2026, 1, 1)})
    loop: list = []
    loop.append(loop)
    with pytest.raises(ValueError):
        canonical_json_v2(loop)
    assert canonical_json_v2({"at": datetime(2026, 1, 1)}, fallback=str) == (
        b'{"at":"2026-01-01 00:00:00"}'
    )
    assert canonical_json_v2((1, 2)) == b"[1,2]"


def test_canonical_bytes_dispatches_and_refuses_unknown_schemes():
    assert canonical_bytes({"a": 1.0}, HASH_SCHEME_V1) == b'{"a":1.0}'
    assert canonical_bytes({"a": 1.0}, HASH_SCHEME_V2) == b'{"a":1}'
    with pytest.raises(ValueError, match="unknown hash scheme"):
        canonical_bytes({}, "sha1-json")


def _append(appender, n, payload):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return appender.append_row("t", "row-%d" % n, None, t, t, payload)


def test_jcs_chains_record_their_scheme_and_verify():
    store = InMemoryHashChainStore()
    appender = HashChainAppender(store, hash_scheme=HASH_SCHEME_V2)
    entries = [_append(appender, i, {"score": 0.1 * i}) for i in range(4)]
    assert {e.hash_scheme for e in entries} == {HASH_SCHEME_V2}
    assert entries[0].payload_hash == compute_payload_hash({"score": 0.0}, HASH_SCHEME_V2)
    assert verify_chain_segment(entries) == (True, None)


def test_a_segment_may_mix_schemes_and_relabelling_is_detected():
    store = InMemoryHashChainStore()
    old = _append(HashChainAppender(store), 0, {"x": 1})
    new = _append(HashChainAppender(store, hash_scheme=HASH_SCHEME_V2), 1, {"x": 1.5})
    assert old.hash_scheme == HASH_SCHEME_V1 and new.hash_scheme == HASH_SCHEME_V2
    assert verify_chain_segment([old, new]) == (True, None)
    # Claiming the other scheme changes the recomputed hash.
    relabelled = dataclasses.replace(new, hash_scheme=HASH_SCHEME_V1)
    assert verify_chain_segment([old, relabelled]) == (False, "row-1")
    unknown = dataclasses.replace(new, hash_scheme="md5-json")
    assert verify_chain_segment([old, unknown]) == (False, "row-1")


def test_default_appender_stays_on_v1():
    entry = _append(HashChainAppender(InMemoryHashChainStore()), 0, {"x": 1})
    assert entry.hash_scheme == HASH_SCHEME_V1
    with pytest.raises(ValueError):
        HashChainAppender(InMemoryHashChainStore(), hash_scheme="nope")


def test_jsonl_store_round_trips_the_scheme_and_keeps_v1_lines_unchanged(tmp_path):
    path = tmp_path / "chain.jsonl"
    store = JsonlHashChainStore(path)
    _append(HashChainAppender(store), 0, {"x": 1})
    _append(HashChainAppender(store, hash_scheme=HASH_SCHEME_V2), 1, {"x": 2.5})
    store.close()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert "hash_scheme" not in lines[0]
    assert lines[1]["hash_scheme"] == HASH_SCHEME_V2
    entries = JsonlHashChainStore(path).load_entries()
    assert [e.hash_scheme for e in entries] == [HASH_SCHEME_V1, HASH_SCHEME_V2]
    assert verify_chain_segment(entries) == (True, None)
