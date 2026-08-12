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


def test_home_and_registry_files_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / "home"))
    s = Store()
    assert (s.home.stat().st_mode & 0o777) == 0o700
    s.set_secret("TOKEN", "supersecret")
    assert ((s.home / "secrets.json").stat().st_mode & 0o777) == 0o600


def test_existing_home_and_secrets_are_tightened(tmp_path, monkeypatch):
    """A store laid down by an older version is re-secured on open."""
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    secrets = home / "secrets.json"
    secrets.write_text('{"TOKEN": "supersecret"}')
    secrets.chmod(0o644)
    monkeypatch.setenv("BRIEFCASE_HOME", str(home))

    s = Store()

    assert (s.home.stat().st_mode & 0o777) == 0o700
    assert (secrets.stat().st_mode & 0o777) == 0o600
    assert s.get_secrets() == {"TOKEN": "supersecret"}


def test_tightening_skips_symlinked_registries(tmp_path, monkeypatch):
    """A planted symlink named *.json must not chmod its target."""
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    victim = tmp_path / "victim.txt"
    victim.write_text("shared")
    victim.chmod(0o644)
    (home / "runs.json").symlink_to(victim)
    monkeypatch.setenv("BRIEFCASE_HOME", str(home))

    Store()

    assert (victim.stat().st_mode & 0o777) == 0o644


def test_save_survives_foreign_tmp_collision(store, tmp_path):
    """A leftover or concurrent writer's temp file never blocks or gets deleted."""
    import os

    foreign = tmp_path / f".runs.{os.getpid()}.tmp"
    foreign.write_text("in-flight")

    store.record_run({"name": "demo", "status": "submitted"})

    assert store.get_run("demo")["status"] == "submitted"
    assert foreign.read_text() == "in-flight"


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
