"""Command handlers: dependency-injected store + engine, no globals."""
from argparse import Namespace

import pytest

from briefcase.cli import commands
from briefcase.cli.engine import OciJJEngine
from briefcase.cli.state import Store


class RecordingRunner:
    """A fake subprocess runner that records the last call and returns a chosen rc."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, env=None, **kw):
        self.calls.append({"argv": argv, "env": env})
        return Namespace(returncode=self.returncode)


@pytest.fixture
def store(tmp_path):
    return Store(home=tmp_path)


def _submit_args(**over):
    base = dict(
        name="demo", repository="cand", revision=None, dataset="xor",
        checkpoint="base", metric="f1", mode="gate", secret=["TOKEN=t"], env=["K=V"],
        dry_run=False,
    )
    base.update(over)
    return Namespace(**base)


def test_dataset_register_and_list(store, capsys):
    assert commands.cmd_dataset_register(_ns(name="xor", uri="synthetic://xor"), store, None) == 0
    assert store.get_dataset("xor")["uri"] == "synthetic://xor"
    commands.cmd_dataset_list(Namespace(), store, None)
    assert "xor" in capsys.readouterr().out


def test_secret_set_requires_kv(store):
    assert commands.cmd_secret_set(Namespace(assignment="NOEQUALS"), store, None) == 2
    assert commands.cmd_secret_set(Namespace(assignment="K=V"), store, None) == 0
    assert store.get_secrets() == {"K": "V"}


def test_run_submit_gate_resolves_uri_injects_secrets_records_run(store):
    store.register_dataset("xor", "synthetic://xor")
    store.set_secret("STORED", "s")
    runner = RecordingRunner()
    engine = OciJJEngine(binary="oci-jj", server="S", repo="R", runner=runner)

    rc = commands.cmd_run_submit(_submit_args(), store, engine)
    assert rc == 0

    call = runner.calls[-1]
    # dataset name resolved to its uri; gate path → attach-bench --run
    assert call["argv"] == [
        "oci-jj", "--server", "S", "--repo", "R",
        "attach-bench", "cand", "--run", "--baseline", "base",
        "--dataset", "synthetic://xor", "--metric", "f1",
    ]
    # stored secret + per-run secret + per-run env all injected
    assert call["env"]["STORED"] == "s"
    assert call["env"]["TOKEN"] == "t"
    assert call["env"]["K"] == "V"

    run = store.get_run("demo")
    assert run["status"] == "submitted"
    assert run["dataset_uri"] == "synthetic://xor"
    assert run["mode"] == "gate"


def test_run_submit_hunt_uses_hunt_argv(store):
    store.register_dataset("xor", "synthetic://xor")
    runner = RecordingRunner()
    engine = OciJJEngine(binary="oci-jj", server="S", repo="R", runner=runner)
    commands.cmd_run_submit(_submit_args(mode="hunt"), store, engine)
    argv = runner.calls[-1]["argv"]
    assert argv[5] == "hunt"  # after [oci-jj, --server, S, --repo, R]
    assert "attach-bench" not in argv


def test_run_submit_unknown_dataset_errors(store):
    runner = RecordingRunner()
    engine = OciJJEngine(runner=runner)
    rc = commands.cmd_run_submit(_submit_args(dataset="missing"), store, engine)
    assert rc == 2
    assert runner.calls == []  # never executed


def test_run_submit_dry_run_skips_exec(store, capsys):
    store.register_dataset("xor", "synthetic://xor")
    runner = RecordingRunner()
    engine = OciJJEngine(binary="oci-jj", server="S", repo="R", runner=runner)
    rc = commands.cmd_run_submit(_submit_args(dry_run=True), store, engine)
    assert rc == 0
    assert runner.calls == []
    assert "attach-bench" in capsys.readouterr().out
    assert store.get_run("demo")["status"] == "dry-run"


def test_run_submit_nonzero_rc_marks_error(store):
    store.register_dataset("xor", "synthetic://xor")
    engine = OciJJEngine(runner=RecordingRunner(returncode=3))
    rc = commands.cmd_run_submit(_submit_args(), store, engine)
    assert rc == 1
    assert store.get_run("demo")["status"] == "error"


def test_run_results_diffs_checkpoint_vs_candidate(store):
    store.record_run({"name": "demo", "repository": "cand", "checkpoint": "base", "mode": "gate"})
    runner = RecordingRunner()
    engine = OciJJEngine(binary="oci-jj", server="S", repo="R", runner=runner)
    commands.cmd_run_results(Namespace(id="demo"), store, engine)
    assert runner.calls[-1]["argv"] == [
        "oci-jj", "--server", "S", "--repo", "R", "diff", "base", "cand", "--depth", "bench",
    ]


def test_run_delete_and_stop(store):
    store.record_run({"name": "demo", "status": "submitted"})
    engine = OciJJEngine(runner=RecordingRunner())
    assert commands.cmd_run_stop(Namespace(id="demo"), store, engine) == 0
    assert store.get_run("demo")["status"] == "stopped"
    assert commands.cmd_run_delete(Namespace(id="demo"), store, engine) == 0
    assert store.get_run("demo") is None


# ---- the same handlers, driven by the gRPC engine (Call seam is transport-neutral) ----
class _FakeTransport:
    def __init__(self, response=None):
        self.calls = []
        self._response = response or {}

    def unary(self, service, method, request):
        from briefcase.cli.grpc_engine import ResponseView
        self.calls.append((service, method, dict(request)))
        return ResponseView(self._response)

    def server_stream(self, service, method, request):
        return iter([])

    def list_services(self):
        return ["oci_jj.v1.VcsService"]


def test_run_submit_gate_via_grpc_records_rpc_call(store):
    from briefcase.cli.grpc_engine import GrpcEngine
    store.register_dataset("xor", "synthetic://xor")
    engine = GrpcEngine(server="s", repo="rl-gym-env", transport=_FakeTransport({"decision": "accept"}))
    rc = commands.cmd_run_submit(_submit_args(), store, engine)
    assert rc == 0
    run = store.get_run("demo")
    assert run["status"] == "submitted"
    assert run["call"]["rpc"] == "oci_jj.v1.VcsService/AttachBench"
    assert run["call"]["request"]["ref"] == "cand"
    assert run["call"]["request"]["baseline_ref"] == "base"


def test_run_submit_dry_run_via_grpc_describes_call(store, capsys):
    from briefcase.cli.grpc_engine import GrpcEngine

    class Boom(_FakeTransport):
        def unary(self, *a):
            raise AssertionError("dry-run must not invoke the RPC")

    store.register_dataset("xor", "synthetic://xor")
    engine = GrpcEngine(server="s", repo="rl-gym-env", transport=Boom())
    rc = commands.cmd_run_submit(_submit_args(dry_run=True), store, engine)
    assert rc == 0
    assert "AttachBench" in capsys.readouterr().out
    assert store.get_run("demo")["call"]["rpc"].endswith("AttachBench")


def _ns(**kw):
    return Namespace(**kw)
