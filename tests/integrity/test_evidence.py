"""Evidence layer, pure Python: Merkle proofs, keys, store/JSONL verification, bundles, CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile

import pytest

from briefcase.cli import bundle_commands, verify_commands
from briefcase.compliance.audit_bundle import BundleError, Selection, create_bundle, verify_bundle
from briefcase.integrity import keys, merkle
from briefcase.integrity.evidence import (
    EvidenceFormatError,
    export_jsonl,
    load_checkpoint_file,
    verify_jsonl,
    verify_store,
)

from .chain_db import ChainWriter

SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))


def kinds(report):
    return [p.kind for p in report.problems]


# --------------------------------------------------------------------------- merkle


def test_merkle_proofs_for_every_leaf_of_every_size():
    for n in range(1, 34):
        leaves = [hashlib.sha256(bytes([i])).digest() for i in range(n)]
        root = merkle.merkle_root(leaves)
        for i in range(n):
            path = merkle.inclusion_proof(leaves, i)
            assert merkle.verify_inclusion(leaves[i], i, n, path, root)
            assert not merkle.verify_inclusion(leaves[(i + 1) % n], i, n, path, root) or n == 1
            assert not merkle.verify_inclusion(leaves[i], i, n, path + [root], root)


def test_merkle_matches_rfc6962_layout_for_three_leaves():
    a, b, c = (hashlib.sha256(x).digest() for x in (b"a", b"b", b"c"))
    expected = merkle.node_hash(merkle.node_hash(merkle.leaf_hash(a), merkle.leaf_hash(b)), merkle.leaf_hash(c))
    assert merkle.merkle_root([a, b, c]) == expected


# --------------------------------------------------------------------------- keys


def test_pure_python_ed25519_matches_rfc8032():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert keys._pure_public_key(seed).hex() == (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    )
    sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac"
        "c61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert keys._pure_verify(keys._pure_public_key(seed), b"", sig)
    assert not keys._pure_verify(keys._pure_public_key(seed), b"x", sig)
    assert keys._pure_sign(seed, b"") == sig
    digest = hashlib.sha256(b"checkpoint").hexdigest()
    signature = keys.sign_digest(SEED, digest)
    x = keys.public_jwk(SEED)["x"]
    raw_sig = keys._unb64url(signature)
    assert keys._pure_verify(keys._unb64url(x), bytes.fromhex(digest), raw_sig)
    assert keys.verify_digest(digest, signature, x)
    assert not keys.verify_digest(digest, signature, keys.public_jwk(OTHER_SEED)["x"])
    assert not keys.verify_digest("zz", signature, x)


def test_key_files_round_trip_and_are_private(tmp_path):
    path = tmp_path / "k.jwk"
    jwk = keys.generate_key_file(path)
    assert path.stat().st_mode & 0o777 == 0o600
    seed = keys.load_seed(path)
    assert keys.public_jwk(seed) == jwk
    assert keys.load_public_key(path) == jwk
    (tmp_path / "pub.jwk").write_text(json.dumps(jwk))
    assert keys.load_public_key(tmp_path / "pub.jwk") == jwk
    with pytest.raises(keys.KeyFileError, match="public key"):
        keys.load_seed(tmp_path / "pub.jwk")
    with pytest.raises(FileExistsError):
        keys.generate_key_file(path)
    assert keys.load_seed(SEED.hex().encode()) == SEED
    assert keys.load_seed(SEED) == SEED


def test_a_mocked_native_core_is_never_trusted_for_signatures():
    # The facade suite mocks briefcase._native with a MagicMock.
    assert keys._native() is None


# --------------------------------------------------------------------------- store


@pytest.fixture
def store(tmp_path):
    writer = ChainWriter(tmp_path / "evidence.db", SEED)
    for i in range(6):
        record = {"decision_id": "d-%d" % i, "score": 0.1 * i, "workflow_id": "wf-a" if i < 4 else "wf-b"}
        writer.append("d-%d" % i, record, kind="decision")
    writer.checkpoint()
    writer.append("r-6", {"decision_id": "r-6", "workflow_id": "wf-b", "x": 1e16})
    yield writer
    writer.close()


def test_an_untouched_store_verifies(store):
    report = verify_store(store.path)
    assert report.intact, report.problems
    assert (report.entries_checked, report.checkpoints_checked, report.signatures_verified) == (7, 1, 1)
    assert report.entries_after_last_checkpoint == 1
    assert report.key_ids == [keys.key_id(keys.public_jwk(SEED)["x"])]
    assert any("cannot be detected" in n for n in report.notes)


def _drop_triggers(writer):
    for name in ("entries_no_update", "entries_no_delete", "checkpoints_no_update", "checkpoints_no_delete"):
        writer.sql("DROP TRIGGER IF EXISTS chain_%s" % name)


def test_in_place_edit_of_a_decision_is_located(store):
    store.sql("UPDATE snapshots SET data_json = replace(data_json, '0.2', '0.9') WHERE id = 'd-2'")
    report = verify_store(store.path)
    assert kinds(report) == ["snapshot_modified"]
    assert report.modified_seqs == [3] and report.first_broken_seq == 3


def test_edit_of_a_chain_entry_and_its_checkpoint_root(store):
    store.sql("UPDATE chain_entries SET record_json = replace(record_json, 'wf-a', 'wf-z') WHERE seq = 2")
    report = verify_store(store.path)
    assert "modified_entry" in kinds(report)
    assert report.modified_seqs == [2]


def test_deletion_and_unchained_rows(store):
    store.sql("DELETE FROM chain_entries WHERE seq IN (3, 4)")
    report = verify_store(store.path)
    assert report.missing_ranges == [(3, 4)]
    assert kinds(report).count("unchained_record") == 2
    assert not report.intact


def test_truncation_needs_an_anchor(store, tmp_path):
    anchor = store.checkpoint()
    store.sql("DELETE FROM chain_entries WHERE seq = 7")
    store.sql("DELETE FROM chain_checkpoints WHERE checkpoint_no = 2")
    assert verify_store(store.path).intact
    report = verify_store(store.path, against=anchor)
    assert kinds(report) == ["truncated"] and report.anchored


def test_key_substitution_is_caught_only_with_a_pinned_key(tmp_path):
    forged = ChainWriter(tmp_path / "forged.db", OTHER_SEED)
    forged.append("a", {"v": 1})
    forged.checkpoint()
    assert verify_store(forged.path).intact
    pinned = verify_store(forged.path, expected_key_id=keys.key_id(keys.public_jwk(SEED)["x"]))
    assert kinds(pinned) == ["unexpected_key"]


def test_a_plain_database_is_a_usage_error(tmp_path):
    import sqlite3

    sqlite3.connect(str(tmp_path / "plain.db")).execute("CREATE TABLE snapshots (id TEXT)").connection.commit()
    with pytest.raises(EvidenceFormatError, match="without a hash chain"):
        verify_store(tmp_path / "plain.db")


# --------------------------------------------------------------------------- jsonl


def test_jsonl_export_verifies_and_detects_edits(store, tmp_path):
    out = tmp_path / "chain.jsonl"
    assert export_jsonl(store.path, out) == 8
    assert verify_jsonl(out).intact
    lines = out.read_text().splitlines()
    first = json.loads(lines[0])
    first["record"]["score"] = 42
    lines[0] = json.dumps(first)
    out.write_text("\n".join(lines) + "\n")
    assert verify_jsonl(out).modified_seqs == [1]


def test_hash_chain_store_files_are_verified_per_stream(tmp_path):
    from datetime import datetime, timezone

    from briefcase.integrity import HashChainAppender, JsonlHashChainStore

    path = tmp_path / "hc.jsonl"
    appender = HashChainAppender(JsonlHashChainStore(path), hash_scheme="jcs-1")
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        appender.append_row("t", "row-%d" % i, None, t, t, {"i": i})
    report = verify_jsonl(path)
    assert report.intact and report.format == "hash-chain-jsonl" and report.entries_checked == 3
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")
    assert kinds(verify_jsonl(path)) == ["broken_link"]


def test_plain_record_files_are_explained(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(json.dumps({"decision_id": "x"}) + "\n")
    with pytest.raises(EvidenceFormatError, match="no chain fields"):
        verify_jsonl(path)


# --------------------------------------------------------------------------- bundles


def test_bundle_round_trip_with_selection_and_signed_tail(store, tmp_path):
    key = tmp_path / "k.jwk"
    key.write_bytes(SEED.hex().encode())
    out = tmp_path / "b.zip"
    summary = create_bundle(store.path, out, selection=Selection(workflow="wf-b"), sign_key=key)
    # d-4, d-5 (checkpointed) and r-6 (after the checkpoint: bundle checkpoint).
    assert summary.records == 3 and summary.checkpoints == 2 and summary.bundle_checkpoints == 1
    report = verify_bundle(out, expected_key_id=keys.key_id(keys.public_jwk(SEED)["x"]))
    assert report.intact, report.problems
    assert report.records_checked == 3 and report.signatures_verified == 3
    readme = zipfile.ZipFile(out).read("README.txt").decode()
    assert "WHAT IT DOES NOT PROVE" in readme and "complete" in readme
    with pytest.raises(BundleError, match="already exists"):
        create_bundle(store.path, out)


def _rewrite(zip_path, name, transform):
    with zipfile.ZipFile(zip_path) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    items[name] = transform(items[name])
    with zipfile.ZipFile(zip_path, "w") as zf:
        for n, data in items.items():
            zf.writestr(n, data)


def test_bundle_tampering_is_reported(store, tmp_path):
    out = tmp_path / "b.zip"
    create_bundle(store.path, out, sign_key=SEED)
    _rewrite(out, "records.jsonl", lambda b: b.replace(b'"wf-a"', b'"wf-x"', 1))
    report = verify_bundle(out)
    assert {"file_modified", "modified_entry"} <= set(kinds(report))

    out2 = tmp_path / "b2.zip"
    create_bundle(store.path, out2, sign_key=SEED)
    _rewrite(out2, "manifest.json", lambda b: b.replace(b'"records": 7', b'"records": 6'))
    assert "manifest_modified" in kinds(verify_bundle(out2))

    out3 = tmp_path / "b3.zip"
    create_bundle(store.path, out3, sign_key=SEED)
    with zipfile.ZipFile(out3, "a") as zf:
        zf.writestr("extra.txt", "hi")
    assert "unexpected_file" in kinds(verify_bundle(out3))

    out4 = tmp_path / "b4.zip"
    create_bundle(store.path, out4, sign_key=SEED)
    wrong = keys.key_id(keys.public_jwk(OTHER_SEED)["x"])
    assert "unexpected_key" in kinds(verify_bundle(out4, expected_key_id=wrong))


def test_bundle_from_a_broken_source_is_refused(store, tmp_path):
    store.sql("UPDATE snapshots SET data_json = '{}' WHERE id = 'd-1'")
    with pytest.raises(BundleError, match="not intact"):
        create_bundle(store.path, tmp_path / "b.zip")


def test_bundle_selection_that_matches_nothing(store, tmp_path):
    with pytest.raises(BundleError, match="no record"):
        create_bundle(store.path, tmp_path / "b.zip", selection=Selection(session="nope"))


def test_unsigned_bundles_say_so(tmp_path):
    writer = ChainWriter(tmp_path / "e.db")
    writer.append("a", {"v": 1})
    out = tmp_path / "b.zip"
    create_bundle(writer.path, out)
    report = verify_bundle(out)
    assert report.intact
    assert any("not signed" in n or "No checkpoint" in n for n in report.notes)


# --------------------------------------------------------------------------- CLI


def run_cli(argv, capsys):
    parser = argparse.ArgumentParser(prog="briefcase")
    sub = parser.add_subparsers(dest="command")
    verify_commands.register(sub)
    bundle_commands.register(sub)
    args = parser.parse_args(argv)
    code = args.func(args, None, None, None)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_verify_exit_codes_and_json(store, tmp_path, capsys):
    code, out, _ = run_cli(["verify", str(store.path)], capsys)
    assert code == 0 and out.startswith("INTACT: evidence.db (chained store)") and "Next: " in out
    code, out, _ = run_cli(["verify", str(store.path), "--json"], capsys)
    assert code == 0 and json.loads(out)["verdict"] == "intact"
    store.sql("UPDATE snapshots SET data_json = '{}' WHERE id = 'd-1'")
    code, out, _ = run_cli(["verify", str(store.path)], capsys)
    assert code == 1 and out.startswith("TAMPERED") and "entry 2" in out
    code, _, err = run_cli(["verify", str(tmp_path / "missing.db")], capsys)
    assert code == 2 and "does not exist" in err and "fix:" in err


def test_cli_save_checkpoint_then_detect_truncation(store, tmp_path, capsys):
    store.checkpoint()
    anchor = tmp_path / "anchor.json"
    code, out, _ = run_cli(["verify", str(store.path), "--save-checkpoint", str(anchor)], capsys)
    assert code == 0 and "Saved the latest checkpoint" in out
    assert load_checkpoint_file(anchor)["seq"] == 7
    store.sql("DELETE FROM chain_entries WHERE seq = 7")
    store.sql("DELETE FROM chain_checkpoints WHERE checkpoint_no = 2")
    code, out, _ = run_cli(["verify", str(store.path), "--against", str(anchor)], capsys)
    assert code == 1 and out.startswith("INCOMPLETE") and "entry 7 was removed" in out


def test_cli_bundle_create_verify_keygen(store, tmp_path, capsys):
    key = tmp_path / "k.jwk"
    code, out, _ = run_cli(["bundle", "keygen", str(key)], capsys)
    assert code == 0 and "Key id" in out
    key_id = keys.key_id(keys.load_public_key(key)["x"])
    out_zip = tmp_path / "b.zip"
    code, out, _ = run_cli(
        ["bundle", "create", "--from", str(store.path), "--workflow", "wf-a", "--out", str(out_zip),
         "--sign-key", str(key)], capsys)
    assert code == 0 and "4 record(s) of 7" in out
    # Checkpoints are signed by the store key, the bundle by the new key.
    code, out, _ = run_cli(["bundle", "verify", str(out_zip), "--key", str(key)], capsys)
    assert code == 1 and "not the expected key" in out
    store_key = keys.key_id(keys.public_jwk(SEED)["x"])
    code, out, _ = run_cli(["bundle", "verify", str(out_zip), "--key", str(key), "--key", store_key], capsys)
    assert code == 0 and out.startswith("INTACT")
    # `briefcase verify` dispatches bundles too; a wrong pinned key fails.
    code, out, _ = run_cli(["verify", str(out_zip), "--key", "0" * 64], capsys)
    assert code == 1 and "not the expected key" in out
    code, out, _ = run_cli(["bundle", "verify", str(out_zip), "--json"], capsys)
    data = json.loads(out)
    assert code == 0 and data["records_checked"] == 4 and key_id in data["key_ids"]
    code, _, err = run_cli(["bundle", "create", "--from", str(store.path), "--out", str(out_zip)], capsys)
    assert code == 2 and "already exists" in err
    code, _, err = run_cli(["bundle", "create", "--from", str(store.path), "--out", str(tmp_path / "c.zip"),
                            "--since", "yesterday"], capsys)
    assert code == 2 and "ISO 8601" in err


def test_status_hook(monkeypatch, tmp_path, store):
    monkeypatch.delenv("BRIEFCASE_EXPORTER", raising=False)
    assert verify_commands.status()["state"] == "off"
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "chain:%s" % store.path)
    monkeypatch.delenv("BRIEFCASE_SIGNING_KEY", raising=False)
    state = verify_commands.status()
    assert state["state"] == "attention" and "7 entries" in state["detail"]
    monkeypatch.setenv("BRIEFCASE_SIGNING_KEY", "/k")
    assert verify_commands.status()["state"] == "on"
    for key in ("id", "state", "detail", "next"):
        assert key in state


def test_everything_works_without_pynacl_or_the_core(store, tmp_path, monkeypatch):
    """An install with neither PyNaCl nor the core falls back to RFC 8032 code."""
    monkeypatch.setattr(keys, "_nacl", lambda: None)
    monkeypatch.setattr(keys, "_native", lambda: None)
    assert keys.describe_backend() == "pure-Python RFC 8032 reference"
    out = tmp_path / "b.zip"
    create_bundle(store.path, out, sign_key=OTHER_SEED)
    report = verify_bundle(out)
    assert report.intact and report.signatures_verified == 3
    assert verify_store(store.path).signatures_verified == 1
