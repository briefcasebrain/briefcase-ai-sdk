"""Regressions found running the evaluate commands against a live oci-jj-server.

Each test pins one gap between what the SDK sent or showed and what oci-jj's
``proto/oci_jj/v1/vcs.proto`` and CLI actually provide. No gRPC dependency: the
engines run over injected fakes.
"""
from argparse import Namespace

import pytest

from briefcase.cli import commands
from briefcase.cli.engine import OciJJEngine, default_binary
from briefcase.cli.grpc_engine import GrpcEngine, ResponseView, Transport
from briefcase.cli.state import Store


class _Transport(Transport):
    def __init__(self, responses=None):
        self.calls = []
        self.token = None
        self._responses = responses or {}

    def set_token(self, token):
        self.token = token

    def unary(self, service, method, request):
        self.calls.append((method, dict(request)))
        return ResponseView(self._responses.get(method, {}))

    def server_stream(self, service, method, request):
        return iter([])

    def list_services(self):
        return ["oci_jj.v1.VcsService"]


@pytest.fixture
def store(tmp_path):
    s = Store(home=tmp_path / "bc")
    s.register_dataset("xor", "synthetic://xor")
    return s


def _submit(**over):
    ns = dict(name="r1", repository="cand", revision=None, dataset="xor", checkpoint="base",
              metric="f1", mode="gate", secret=None, env=None, dry_run=False)
    ns.update(over)
    return Namespace(**ns)


def test_submit_records_and_prints_the_server_job_id(store, capsys):
    """AttachBenchResponse.job_id is the only handle on the queued verdict job; it was dropped."""
    engine = GrpcEngine(server="http://x", repo="R",
                        transport=_Transport({"AttachBench": {"job_id": 42}}))
    assert commands.cmd_run_submit(_submit(), store, engine) == 0
    assert store.get_run("r1")["job_id"] == 42
    assert "server job 42" in capsys.readouterr().out


def test_hunt_submit_records_job_id(store):
    engine = GrpcEngine(server="http://x", repo="R",
                        transport=_Transport({"RunHunt": {"job_id": 7}}))
    commands.cmd_run_submit(_submit(mode="hunt", checkpoint=None), store, engine)
    assert store.get_run("r1")["job_id"] == 7


def test_results_without_checkpoint_is_a_clear_error_not_a_crash(store, capsys):
    """A gate run submitted without --checkpoint has no baseline; results used to pass None into
    the request builder and die with 'TypeError: bad argument type for built-in operation'."""
    t = _Transport()
    engine = GrpcEngine(server="http://x", repo="R", transport=t)
    store.record_run({"name": "r1", "repository": "cand", "checkpoint": None, "mode": "gate"})
    assert commands.cmd_run_results(Namespace(id="r1"), store, engine) == 2
    assert "--checkpoint" in capsys.readouterr().err
    assert t.calls == []


def test_unavailable_bench_reason_is_printed_once(store, capsys):
    """oci-jj sets rendered == unavailable_reason when no bench verdict exists yet."""
    msg = "not available — telemetry class disabled"
    engine = GrpcEngine(server="http://x", repo="R", transport=_Transport(
        {"Diff": {"unavailable_reason": msg, "rendered": msg}}))
    store.record_run({"name": "r1", "repository": "cand", "checkpoint": "base", "mode": "gate"})
    commands.cmd_run_results(Namespace(id="r1"), store, engine)
    assert capsys.readouterr().out.count(msg) == 1


def test_grpc_exec_uses_auth_token_from_secrets_and_warns_about_the_rest(capsys):
    """Over gRPC nothing carries run secrets to oci-jj; only the client's own OCI_JJ_AUTH_TOKEN
    (e.g. from `briefcase secret set`) has an effect. The rest used to vanish silently."""
    t = _Transport()
    engine = GrpcEngine(server="http://x", repo="R", transport=t)
    engine.exec(engine.call_provenance("cand"), env={"OCI_JJ_AUTH_TOKEN": "tok", "HF_TOKEN": "v"})
    assert t.token == "tok"
    err = capsys.readouterr().err
    assert "HF_TOKEN" in err and "tok" not in err.replace("HF_TOKEN", "")


def test_grpc_exec_without_env_is_silent(capsys):
    engine = GrpcEngine(server="http://x", repo="R", transport=_Transport())
    engine.exec(engine.call_provenance("cand"))
    assert capsys.readouterr().err == ""


def test_cli_engine_defaults_to_the_oci_bai_binary(monkeypatch):
    """oci-jj's CLI crate builds `oci-bai`; the old `oci-jj` default failed with FileNotFoundError."""
    monkeypatch.delenv("BRIEFCASE_OCIJJ_BIN", raising=False)
    assert default_binary(which=lambda name: None) == "oci-bai"
    assert default_binary(which=lambda name: "/bin/x" if name == "oci-jj" else None) == "oci-jj"
    assert default_binary(which=lambda name: "/bin/x") == "oci-bai"
    monkeypatch.setattr("briefcase.cli.engine.shutil.which", lambda name: None)
    assert OciJJEngine(server="S", repo="R").binary == "oci-bai"


def test_cli_engine_binary_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_OCIJJ_BIN", "/opt/oci-jj")
    assert OciJJEngine(server="S", repo="R").binary == "/opt/oci-jj"
