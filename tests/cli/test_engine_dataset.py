"""`briefcase dataset publish`: into the local engine's tenant-scoped dataset bucket.

oci-jj (e89ddbf, dataset.rs) accepts only synthetic:// and s3://<dataset bucket>/tenants/<tenant>/...;
the upload pipes the file into `mc` inside the MinIO container, so the fake docker records the
compose exec argv and the sha256 of what arrived on stdin."""
import hashlib
import json

import pytest

from briefcase.cli.__main__ import main
from briefcase.cli.state import Store
from briefcase.engine import datasets
from briefcase.engine.install import save_state

from tests.cli.engine_fakes import docker_calls, install_fake_docker


@pytest.fixture
def local_engine(shell_home, tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    install_fake_docker(tmp_path / "bin", log, monkeypatch)
    for name in ("OCI_JJ_SERVER", "BRIEFCASE_ENGINE_ADDR"):
        monkeypatch.delenv(name, raising=False)
    home = shell_home / ".briefcase" / "engine"
    (home / "0.2.0").mkdir(parents=True)
    (home / "engine.env").write_text("MINIO_ROOT_USER=u\n")
    save_state({"schema": 1, "current": "0.2.0", "installs": {"0.2.0": {
        "version": "0.2.0", "compose": str(home / "0.2.0" / "docker-compose.yml"),
        "env_file": str(home / "engine.env"), "tenant": "local", "binary": "", "grpc_addr": "http://127.0.0.1:50051"}}})
    return log


def test_publish_pipes_into_the_tenant_prefix_and_registers_the_s3_uri(local_engine, tmp_path, capsys):
    data = tmp_path / "churn data.csv"
    data.write_text("a,b,target\n1,2,0\n3,4,1\n")
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    assert main(["dataset", "publish", "churn", str(data)]) == 0
    uri = f"s3://oci-jj-datasets/tenants/local/datasets/{digest}/churn-data.csv"
    out = capsys.readouterr().out
    assert uri in out and "Next: briefcase run submit" in out
    (call,) = [c for c in docker_calls(local_engine) if "exec" in c["argv"]]
    argv = call["argv"]
    assert argv[:3] == ["compose", "--project-name", "briefcase-engine"]
    assert argv[argv.index("exec"):argv.index("exec") + 3] == ["exec", "-T", "minio"]
    assert argv[-1] == uri[len("s3://"):]
    assert call["stdin"] == f"sha256:{digest}"  # the file itself, on stdin
    assert not any("MINIO_ROOT_PASSWORD=" in part for part in argv)  # credentials stay in the container
    assert Store().get_dataset("churn")["uri"] == uri


def test_publish_rejects_formats_the_worker_cannot_read(local_engine, tmp_path, capsys):
    data = tmp_path / "d.json"
    data.write_text("{}")
    assert main(["dataset", "publish", "d", str(data)]) == 2
    assert ".parquet or .csv" in capsys.readouterr().err


def test_publish_reports_a_stopped_engine(local_engine, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_EXEC_FAIL", "1")
    data = tmp_path / "d.csv"
    data.write_text("x,target\n1,0\n")
    assert main(["dataset", "publish", "d", str(data)]) == 1
    err = capsys.readouterr().err
    assert "minio" in err and "Fix: briefcase engine up" in err
    assert Store().get_dataset("d") is None


def test_publish_to_an_engine_elsewhere_explains_the_manual_path(shell_home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "https://engine.example:443")
    data = tmp_path / "d.parquet"
    data.write_bytes(b"PAR1")
    assert main(["dataset", "publish", "d", str(data)]) == 1
    assert "briefcase dataset register d --uri s3://" in capsys.readouterr().err


def test_object_keys_stay_in_the_engine_charset():
    key = datasets.object_key("local", "ab" * 32, "../../etc/pass wd.csv")
    assert key == f"tenants/local/datasets/{'ab' * 32}/etc-pass-wd.csv"
    for segment in key.split("/"):
        assert segment not in ("", ".", "..")
    assert json.dumps(key).isascii()
