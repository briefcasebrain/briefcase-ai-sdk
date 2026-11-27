"""Offline verification of legacy records and managed engine discovery."""
import json
from pathlib import Path

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main
from briefcase.engine import trust

from tests.cli.engine_fakes import (
    OPENSSL, VERSION, build_index, build_release, calls,
    install_fake_tools,
)


def test_check_file_rejects_a_same_length_change_without_a_signature(tmp_path, key, pinned):
    release = build_release(key)
    entry = release.manifest["bundles"][0]
    path = tmp_path / entry["file"]
    data = release.files[entry["file"]]
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    manifest = trust.load_manifest(release.files["engine-manifest.json"],
                                   release.files["engine-manifest.json.sig"], trust.load_keys(trust.KEYS_DIR))
    with pytest.raises(trust.TrustError, match="does not match"):
        trust.check_file(path, entry, manifest, None)


def test_key_loader_accepts_any_release_pub_and_ignores_others(key, tmp_path):
    keys_dir = tmp_path / "k"
    keys_dir.mkdir()
    (keys_dir / "release-anything.pub").write_bytes(key.public_pem)
    (keys_dir / "release-broken.pub").write_text("not a key")
    (keys_dir / "notes.pub").write_bytes(key.public_pem)
    (keys_dir / "README.md").write_text("x")
    loaded = trust.load_keys(keys_dir)
    assert [k.key_id for k in loaded] == [key.key_id] and loaded[0].label == "release-anything.pub"


def test_shipped_key_directory_has_readme_and_only_the_pinned_release_keys():
    assert (trust.KEYS_DIR / "README.md").is_file()
    keys = trust.load_keys(trust.KEYS_DIR)
    assert sorted(k.key_id for k in keys) == [
        "sha256:2c2303bdef3d97b33ca998f9eb397a8bf3de61465a0cb4fe955767e8b5470111",   # active release key
        "sha256:54a3f59fc571363966ea0e0700388201ce51a5d50e4d2f3de7843e8cd61fee43"]   # offline recovery key


@pytest.fixture
def offline(env):
    directory = env["tmp"] / "retained"
    env["release"].write(directory)
    return directory


def test_engine_verify(env, offline, capsys):
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    manifest = offline / "engine-manifest.json"
    assert main(["engine", "verify", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert f"verified: engine-manifest.json (engine {VERSION})" in out and env["key"].key_id in out
    assert main(["engine", "verify", str(bundle), "--manifest", str(manifest), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["checked"] == ["size", "sha256", "signature"]
    assert result["key_id"] == env["key"].key_id and result["key_file"].startswith("release-")
    index = build_index(env["key"], [VERSION])
    (offline / "index.json").write_bytes(index["index.json"])
    assert main(["engine", "verify", str(offline / "index.json")]) == 0  # one object: no .sig to look for
    assert "checked: signature" in capsys.readouterr().out
    binary = offline / f"oci-bai-{VERSION}-linux-x86_64.tar.gz"
    assert main(["engine", "verify", str(binary)]) == 0  # its .sig alone
    assert "checked: signature" in capsys.readouterr().out

    binary.write_bytes(binary.read_bytes() + b"x")
    assert main(["engine", "verify", str(binary)]) == 1
    assert "does not verify with any key pinned in this SDK" in capsys.readouterr().err
    assert main(["engine", "verify", str(binary), "--manifest", str(manifest)]) == 1
    assert "does not match the signed manifest" in capsys.readouterr().err
    (offline / "lonely.bin").write_bytes(b"x")
    assert main(["engine", "verify", str(offline / "lonely.bin")]) == 2
    assert "--signature" in capsys.readouterr().err


def test_engine_verify_with_cosign_when_cryptography_is_missing(env, offline, capsys, monkeypatch):
    if OPENSSL is None:
        pytest.skip("the fake cosign verifies with openssl")
    install_fake_tools(env["tmp"] / "bin", env["log"], monkeypatch, ("cosign",))
    monkeypatch.setattr(trust, "crypto_available", lambda: False)
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    assert main(["engine", "verify", str(bundle)]) == 0
    argv = calls(env["log"], "cosign")[0]["argv"]
    assert argv[0] == "verify-blob" and "--key" in argv and "--insecure-ignore-tlog=true" in argv
    (offline / (bundle.name + ".sig")).write_bytes(env["key"].sign(b"other"))
    assert main(["engine", "verify", str(bundle)]) == 1


def test_signature_format_is_cosign_blob(key):
    data = b"payload"
    sig = key.sign(data)
    k = trust.ReleaseKey(key.key_id, trust.Path("release-x.pub"), key.public_pem, trust.pem_to_der(key.public_pem))
    if not trust.crypto_available():
        pytest.skip("pure-python verification needs cryptography")
    assert trust.verify_blob(data, sig, k) and trust.verify_blob(data, sig + b"\n", k)
    assert not trust.verify_blob(b"other", sig, k)
    assert trust.key_id_of(key.public_pem) == key.key_id


VECTOR = Path(__file__).parent / "testdata" / "index-envelope"


def _pin_vector(tmp_path, monkeypatch, also=None):
    """Trust the vector's public key, as a temporary key, next to the test key."""
    keys_dir = tmp_path / "vector-keys"
    keys_dir.mkdir()
    key_id = (VECTOR / "key_id.txt").read_text().strip()
    (keys_dir / f"release-{key_id[7:23]}.pub").write_bytes((VECTOR / "public.pem").read_bytes())
    if also is not None:
        also.pin(keys_dir)
    monkeypatch.setattr(trust, "KEYS_DIR", keys_dir)
    return key_id


def test_the_index_envelope_vector_from_the_release_tooling_opens_with_its_key(tmp_path, monkeypatch):
    """Both files come from oci-jj/scripts/release/testdata/index-envelope (its own test checks them with
    openssl); here the SDK's reader must agree."""
    key_id = _pin_vector(tmp_path, monkeypatch)
    index, key = trust.open_index((VECTOR / "envelope.json").read_bytes(), trust.require_keys())
    assert key.key_id == key_id and index["key_id"] == key_id
    assert [v["version"] for v in index["versions"]] == ["0.2.0", "0.1.0-rc.1"]
    with pytest.raises(trust.TrustError, match="does not verify"):
        trust.open_index((VECTOR / "envelope-tampered.json").read_bytes(), trust.require_keys())


def test_an_index_envelope_is_refused_whole_and_its_payload_never_used(tmp_path, monkeypatch, key):
    _pin_vector(tmp_path, monkeypatch)
    good = json.loads((VECTOR / "envelope.json").read_bytes())

    def refused(doc, match):
        with pytest.raises(trust.TrustError, match=match):
            trust.open_index(json.dumps(doc).encode(), trust.require_keys())

    refused(dict(good, key_id=key.key_id), "does not trust")  # a well-formed key id that is not pinned
    refused({k: v for k, v in good.items() if k != "signature"}, "malformed")
    refused(dict(good, extra=1), "malformed")
    refused(dict(good, schema_version=1), "schema_version")
    refused(dict(good, payload=["not", "a", "string"]), "malformed")
    refused(dict(good, signature="not base64!"), "malformed")
    refused(dict(good, signature=good["signature"][:-8] + "AAAAAAAA"), "does not verify")
    # Signed correctly, but the payload names another key than the envelope does.
    other = build_index(key, ["1.0.0"])
    forged = json.loads(other["index.json"])
    refused(dict(good, payload=forged["payload"]), "does not verify")
    with pytest.raises(trust.TrustError, match="malformed"):
        trust.open_index(b"not json", trust.require_keys())
    with pytest.raises(trust.TrustError, match="malformed"):
        trust.open_index(b"[]", trust.require_keys())


def test_a_payload_that_names_another_key_than_its_envelope_is_refused(tmp_path, monkeypatch, key):
    _pin_vector(tmp_path, monkeypatch, also=key)
    doc = json.loads(build_index(key, ["1.0.0"])["index.json"])
    payload = json.loads(doc["payload"])
    payload["key_id"] = (VECTOR / "key_id.txt").read_text().strip()
    text = json.dumps(payload)
    doc["payload"], doc["signature"] = text, key.sign(text.encode("utf-8")).decode("ascii")
    with pytest.raises(trust.TrustError, match="different key"):
        trust.open_index(json.dumps(doc).encode(), trust.require_keys())


def test_run_logs_says_the_compose_override_is_no_longer_read(env, capsys, monkeypatch):
    from types import SimpleNamespace

    from briefcase.cli import commands

    monkeypatch.setenv("BRIEFCASE_OCIJJ_COMPOSE", "/somewhere/docker-compose.yml")
    store = SimpleNamespace(get_run=lambda run_id: {"id": run_id})
    commands.cmd_run_logs(SimpleNamespace(id="demo", follow=False), store, None)
    assert "BRIEFCASE_OCIJJ_COMPOSE is no longer read" in capsys.readouterr().err


LABEL = {"ai.briefcase.engine": "1"}


def test_run_logs_without_an_engine_says_to_install_it(shell_home, capsys, monkeypatch):
    from types import SimpleNamespace

    from briefcase.cli import commands

    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    store = SimpleNamespace(get_run=lambda run_id: {"id": run_id})
    assert commands.cmd_run_logs(SimpleNamespace(id="demo", follow=False), store, None) == 3
    assert _engine.MANAGED_ACCESS in capsys.readouterr().err


GATED = [
    ["graph", "log", "rl-gym-env"],
    ["graph", "diff", "a", "b"],
    ["env", "push", "img:1"],
    ["env", "checkout", "ref"],
    ["env", "gc"],
    ["verify", "--graph", "ref"],
    ["run", "attach", "x", "--result", "v.json", "--repository", "r"],

    ["engine", "status"],
]


@pytest.mark.parametrize("argv", GATED)
def test_engine_commands_without_an_install_exit_3(shell_home, capsys, monkeypatch, argv, tmp_path):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    (tmp_path / "project" / "v.json").write_text("{}")
    assert main(argv) == 3
    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert _engine.NOT_INSTALLED in text and "Traceback" not in text


def test_engine_status_json_without_an_install_exits_3(shell_home, capsys, monkeypatch):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    assert main(["engine", "status", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["installed"] is False


def test_gated_json_error(shell_home, capsys, monkeypatch):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    assert main(["graph", "log", "x", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "engine_not_installed"


def test_dataset_publish_without_engine(shell_home, capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    data = tmp_path / "project" / "d.csv"
    data.write_text("a,target\n1,0\n")
    assert main(["dataset", "publish", "d", str(data)]) == 3
    assert main(["dataset", "publish", "d", str(data), "--dry-run"]) == 0
    assert "s3://oci-jj-datasets/tenants/local/datasets/" in capsys.readouterr().out


def test_help_status_and_features_mark_the_engine(shell_home, capsys, monkeypatch):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    with pytest.raises(SystemExit):
        main(["--help"])
    flat = " ".join(capsys.readouterr().out.split())
    assert "Managed engine access and legacy migration." in flat
    for help_text in (
                      "Engine artifact graph: history, diffs, provenance, search.",
                      "Engine environments: push, compose, check out, bookmark, undo."):
        assert help_text + _engine.HELP_SUFFIX in flat
    assert main(["status", "--json"]) == 0
    items = {i["id"]: i for i in json.loads(capsys.readouterr().out)["items"]}
    for ident in ("engine", "graph", "env-artifacts"):
        assert items[ident]["state"] == "off"
        assert items[ident]["detail"] == _engine.NOT_INSTALLED and items[ident]["next"] is None
    assert main(["features", "--json"]) == 0
    ids = {f["id"] for g in json.loads(capsys.readouterr().out)["groups"] for f in g["features"]}
    assert {"engine", "graph", "env-artifacts"} <= ids


def test_remote_engine_counts_as_available(shell_home, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "https://engine.example:443")
    assert _engine.available() and _engine.client_settings()["addr"] == "https://engine.example:443"
    assert _engine.client_settings()["token"] is None  # the local install's token is not sent elsewhere


def _grpc_missing():
    try:
        import grpc  # noqa: F401
    except ImportError:
        return True
    return False


@pytest.mark.skipif(not _grpc_missing(), reason="checks the message shown when grpcio is absent")
def test_engine_command_without_grpcio_names_the_extra(shell_home, capsys, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "http://127.0.0.1:50051")
    assert main(["graph", "log", "x"]) == 1
    err = capsys.readouterr().err
    assert "pip install 'briefcase-ai[engine]'" in err and "Traceback" not in err


def test_engine_commands_render_over_an_injected_transport(shell_home, capsys, monkeypatch):
    """Without grpcio too: the commands only see plain dicts from the client."""
    from briefcase.engine import cli_support
    from briefcase.engine.client import EngineClient

    class Transport:
        def unary(self, service, method, request):
            return {"ok": True, "trees_checked": 1, "inodes_checked": 2, "segments_checked": 3}

        def server_stream(self, service, method, request):
            return iter([])

    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "http://127.0.0.1:50051")
    monkeypatch.setattr(cli_support, "CLIENT_FACTORY",
                        lambda server=None, repo=None: EngineClient(server=server, repo=repo, transport=Transport()))
    assert main(["verify", "--graph", "cand"]) == 0
    assert "intact (1 trees" in capsys.readouterr().out
    assert main(["graph", "log", "cand"]) == 0
    assert "no commits for 'cand'" in capsys.readouterr().out
