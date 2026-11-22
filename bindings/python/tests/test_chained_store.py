"""Chained (tamper-evident) SqliteBackend through the real native core.

Cross-language evidence: chains written by Rust are re-verified by the
pure-Python verifier and vice versa, keys made in Python sign in Rust (and
the other way), and two OS processes appending to one file never fork it.
"""

import importlib.util
import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from briefcase import _native
from briefcase._native import DecisionSnapshot, Output, SqliteBackend, init_with_config, is_initialized
from briefcase.compliance.audit_bundle import Selection, create_bundle, verify_bundle
from briefcase.integrity import canonical_json_v2, keys, merkle, signing
from briefcase.integrity.evidence import verify_store

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def runtime():
    if not is_initialized():
        init_with_config(2)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "signing-key.jwk"
    keys.generate_key_file(path)
    return path


def _chain_writer():
    spec = importlib.util.spec_from_file_location("chain_db", REPO / "tests" / "integrity" / "chain_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ChainWriter


def _tamper(path, *statements):
    conn = sqlite3.connect(str(path))
    for name in ("entries_no_update", "entries_no_delete", "checkpoints_no_update", "checkpoints_no_delete"):
        conn.execute("DROP TRIGGER IF EXISTS chain_%s" % name)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def decision(answer, confidence=0.93):
    d = DecisionSnapshot("review_claim")
    d.add_output(Output("verdict", answer, "string").with_confidence(confidence))
    return d


def test_chained_backend_is_append_only_and_verifies(tmp_path, key_file):
    db = tmp_path / "evidence.db"
    backend = SqliteBackend(str(db), chained=True, signing_key=str(key_file), checkpoint_every=3)
    assert backend.chained and backend.append_only and "chained=True" in repr(backend)
    d = decision("approved")
    decision_id = backend.save_decision(d)
    with pytest.raises(FileExistsError):
        backend.save_decision(d)
    with pytest.raises(PermissionError):
        backend.delete(decision_id)
    for i in range(5):
        entry = backend.append_record("rec-%d" % i, {"i": i, "score": 0.1 * i, "big": 2**60, "text": "Grüße 😀"})
    assert entry["seq"] == 6 and entry["hash_scheme"] == "jcs-1"
    with pytest.raises(ValueError, match="encode it as a string"):
        backend.append_record("too-big", {"id": 2**53 + 1})
    native = backend.verify()
    python = verify_store(db)
    assert native["intact"] and python.intact
    assert native["entries_checked"] == python.entries_checked == 6
    assert native["signatures_verified"] == python.signatures_verified == 2
    assert native["key_ids"] == python.key_ids == [keys.key_id(keys.load_public_key(key_file)["x"])]


def test_plain_backends_are_unchanged(tmp_path):
    backend = SqliteBackend(str(tmp_path / "plain.db"))
    assert not backend.chained
    with pytest.raises(ValueError, match="chained=True"):
        SqliteBackend(None, signing_key=b"\x00" * 32)


@pytest.mark.parametrize(
    "statements, expected",
    [
        (["UPDATE chain_entries SET record_json = '{\"i\":99}' WHERE seq = 2"], {"modified_entry"}),
        (["DELETE FROM chain_entries WHERE seq = 3"], {"missing_entries"}),
        (["UPDATE chain_checkpoints SET merkle_root = '%s' WHERE checkpoint_no = 1" % ("ab" * 32)],
         {"checkpoint_modified", "checkpoint_mismatch"}),
        (["UPDATE chain_checkpoints SET signature = 'AAAA' WHERE checkpoint_no = 1"],
         {"checkpoint_signature_invalid"}),
    ],
)
def test_rust_and_python_verifiers_agree_on_tampering(tmp_path, key_file, statements, expected):
    db = tmp_path / "e.db"
    backend = SqliteBackend(str(db), chained=True, signing_key=str(key_file), checkpoint_every=0)
    for i in range(5):
        backend.append_record("r%d" % i, {"i": i, "f": i / 3})
    backend.checkpoint()
    _tamper(db, *statements)
    native = backend.verify()
    python = verify_store(db)
    assert not native["intact"] and not python.intact
    assert {p["kind"] for p in native["problems"]} == {p.kind for p in python.problems} == expected
    assert native["first_broken_seq"] == python.first_broken_seq


def test_a_chain_written_in_python_verifies_in_rust(tmp_path):
    ChainWriter = _chain_writer()
    seed = bytes(range(32))
    writer = ChainWriter(tmp_path / "py.db", seed)
    for i in range(4):
        writer.append("r%d" % i, {"i": i, "x": 1e16 + i, "y": 0.1 * i})
    writer.checkpoint()
    writer.close()
    report = SqliteBackend(str(tmp_path / "py.db")).verify()
    assert report["intact"], report["problems"]
    assert report["signatures_verified"] == 1


def test_truncation_against_an_anchored_checkpoint(tmp_path, key_file):
    db = tmp_path / "e.db"
    backend = SqliteBackend(str(db), chained=True, signing_key=str(key_file), checkpoint_every=0)
    for i in range(4):
        backend.append_record("r%d" % i, {"i": i})
    anchor = backend.checkpoint()
    _tamper(db, "DELETE FROM chain_entries WHERE seq = 4", "DELETE FROM chain_checkpoints")
    assert backend.verify()["intact"]
    report = backend.verify(against_checkpoint=anchor)
    assert [p["kind"] for p in report["problems"]] == ["truncated"]
    assert verify_store(db, against=anchor).problems[0].kind == "truncated"
    # The anchor can also be passed as JSON text.
    assert not backend.verify(against_checkpoint=json.dumps(anchor))["intact"]


def test_keys_are_interchangeable_between_python_and_rust(tmp_path, key_file):
    seed = keys.load_seed(key_file)
    jwk = keys.public_jwk(seed)
    assert _native.ed25519_public_jwk(str(key_file)) == jwk
    assert _native.ed25519_public_jwk(seed) == jwk
    assert keys._pure_public_key(seed) == keys._unb64url(jwk["x"])
    assert _native.ed25519_key_id(jwk["x"]) == keys.key_id(jwk["x"]) == signing.jwk_thumbprint(jwk)
    digest = "ab" * 32
    # Python signs (RFC 8032 reference code, independent of Rust); Rust verifies.
    py_sig = keys._b64url(keys._pure_sign(seed, bytes.fromhex(digest)))
    assert _native.ed25519_verify_digest(digest, py_sig, jwk["x"])
    assert not _native.ed25519_verify_digest("cd" * 32, py_sig, jwk["x"])
    # Rust signs; Python verifies. Ed25519 is deterministic, so the bytes match.
    rust_sig = _native.ed25519_sign_digest(str(key_file), digest)
    assert rust_sig == py_sig
    assert keys._pure_verify(keys._unb64url(jwk["x"]), bytes.fromhex(digest), keys._unb64url(rust_sig))
    # A Rust checkpoint signature verifies in Python.
    backend = SqliteBackend(str(tmp_path / "e.db"), chained=True, signing_key=key_file.read_bytes())
    backend.append_record("a", {"v": 1})
    cp = backend.checkpoint()
    assert keys.verify_digest(cp["checkpoint_hash"], cp["signature"], jwk["x"])
    assert keys._pure_verify(
        keys._unb64url(jwk["x"]), bytes.fromhex(cp["checkpoint_hash"]), keys._unb64url(cp["signature"])
    )
    try:
        import nacl  # noqa: F401
    except ImportError:
        return
    # With PyNaCl installed, the existing signing module agrees too.
    assert signing.sign_digest(seed, digest) == rust_sig
    assert signing.verify_digest(cp["checkpoint_hash"], cp["signature"], jwk)


def test_rust_inclusion_proofs_verify_in_python(tmp_path):
    backend = SqliteBackend(str(tmp_path / "e.db"), chained=True, checkpoint_every=5)
    for i in range(12):
        backend.append_record("r%d" % i, {"i": i})
    roots = {cp["checkpoint_no"]: cp["merkle_root"] for cp in backend.checkpoints()}
    for seq in range(1, 11):
        proof = backend.inclusion_proof(seq)
        assert merkle.verify_inclusion(
            bytes.fromhex(proof["entry_hash"]), proof["leaf_index"], proof["tree_size"],
            [bytes.fromhex(h) for h in proof["audit_path"]], bytes.fromhex(roots[proof["checkpoint_no"]]),
        )
    assert backend.inclusion_proof(12) is None


def test_rust_canonicalizer_matches_python_on_the_shared_vectors():
    doc = json.loads((REPO / "tests" / "vectors" / "canonical_json_v2.json").read_text("utf-8"))
    checked = 0
    for vector in doc["vectors"]:
        if "error" in vector:
            continue
        assert _native.jcs_canonicalize(vector["canonical"], stored=True) == vector["canonical"]
        checked += 1
    assert checked > 60
    assert _native.jcs_canonicalize('{"b":1e16,"a":[1.0,-0.0]}') == canonical_json_v2(
        {"b": 1e16, "a": [1.0, -0.0]}).decode()


def _child_append(path, tag, n):
    from briefcase._native import SqliteBackend as Backend

    backend = Backend(path, chained=True, checkpoint_every=10)
    for i in range(n):
        backend.append_record("%s-%d" % (tag, i), {"tag": tag, "i": i, "f": i * 0.1})


def test_two_processes_appending_do_not_fork_the_chain(tmp_path):
    db = str(tmp_path / "shared.db")
    SqliteBackend(db, chained=True)  # create the schema first
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_child_append, args=(db, tag, 60)) for tag in ("a", "b")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(120)
        assert proc.exitcode == 0
    report = verify_store(db)
    assert report.intact, report.problems
    assert report.entries_checked == 120 and report.checkpoints_checked == 12
    assert SqliteBackend(db).verify()["intact"]


def test_observe_chain_exports_records_and_bundles_them(tmp_path, key_file, monkeypatch):
    import briefcase

    monkeypatch.setenv("BRIEFCASE_SIGNING_KEY", str(key_file))
    db = tmp_path / "evidence.db"
    exporter = briefcase.observe("chain:%s" % db)
    assert type(exporter).__name__ == "ChainedSqliteExporter" and exporter.signed

    @briefcase.capture(async_capture=False)
    def score(amount: float) -> float:
        return round(amount / 3, 4)

    for amount in (10.0, 20.5, 31.25):
        score(amount)
    briefcase.shutdown()  # closes the exporter, which writes a checkpoint
    report = verify_store(db)
    assert report.intact and report.entries_checked == 3
    assert report.checkpoints_checked == 1 and report.signatures_verified == 1
    out = tmp_path / "bundle.zip"
    summary = create_bundle(db, out, selection=Selection(), sign_key=key_file)
    assert summary.records == 3
    assert verify_bundle(out, expected_key_id=keys.key_id(keys.load_public_key(key_file)["x"])).intact
