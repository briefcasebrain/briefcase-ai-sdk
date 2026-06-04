"""Store: local registries for datasets, secrets, and runs (pure JSON, atomic writes)."""
import json

import pytest

from briefcase.cli.state import Store


@pytest.fixture
def store(tmp_path):
    return Store(home=tmp_path)


def test_home_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / "envhome"))
    s = Store()
    assert s.home == tmp_path / "envhome"
    assert s.home.is_dir()  # created eagerly


def test_dataset_round_trip(store):
    rec = store.register_dataset("xor", "synthetic://xor")
    assert rec == {"name": "xor", "uri": "synthetic://xor"}
    assert store.get_dataset("xor") == rec
    assert store.get_dataset("missing") is None
    store.register_dataset("abc", "file:///a.parquet")
    assert [r["name"] for r in store.list_datasets()] == ["abc", "xor"]  # sorted by name


def test_secret_set_get_and_keys_are_redacted(store):
    store.set_secret("OCI_JJ_S3_ENDPOINT", "http://127.0.0.1:9000")
    store.set_secret("TOKEN", "supersecret")
    assert store.get_secrets() == {
        "OCI_JJ_S3_ENDPOINT": "http://127.0.0.1:9000",
        "TOKEN": "supersecret",
    }
    # list_secret_keys never leaks values
    keys = store.list_secret_keys()
    assert keys == ["OCI_JJ_S3_ENDPOINT", "TOKEN"]
    assert "supersecret" not in "".join(keys)


def test_run_lifecycle(store):
    run_id = store.record_run({"name": "demo", "status": "submitted", "mode": "gate"})
    assert run_id == "demo"  # the job name is the handle
    assert store.get_run("demo")["status"] == "submitted"
    store.update_run("demo", status="completed")
    assert store.get_run("demo")["status"] == "completed"
    assert [r["name"] for r in store.list_runs()] == ["demo"]
    assert store.delete_run("demo") is True
    assert store.get_run("demo") is None
    assert store.delete_run("demo") is False


def test_update_missing_run_raises(store):
    with pytest.raises(KeyError):
        store.update_run("nope", status="x")


def test_save_is_atomic_and_leaves_no_tmp(store, tmp_path):
    store.register_dataset("xor", "synthetic://xor")
    # the real file is valid JSON and no temp files linger
    data = json.loads((tmp_path / "datasets.json").read_text())
    assert data["xor"]["uri"] == "synthetic://xor"
    assert list(tmp_path.glob(".*tmp*")) == []


def test_failed_save_preserves_prior_state(store, tmp_path):
    store.register_dataset("xor", "synthetic://xor")
    # a non-serializable payload must not corrupt the existing file or leave a temp file
    with pytest.raises(TypeError):
        store.record_run({"name": "bad", "obj": object()})
    assert store.get_dataset("xor") == {"name": "xor", "uri": "synthetic://xor"}
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".")] == []
