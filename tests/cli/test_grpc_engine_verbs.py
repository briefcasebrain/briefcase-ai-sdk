"""GrpcEngine: verb -> RPC mapping over an injected fake transport.

Mirrors the ``RecordingRunner`` injection style used for ``OciJJEngine`` — a fake ``Transport``
records ``(service, method, request)`` and returns canned responses, so the verb mapping is unit-tested
with no gRPC channel. (The live reflection walk is covered by the e2e.)
"""
import pytest

from briefcase.cli.grpc_engine import ApiFloorError, GrpcEngine, ResponseView, Transport


class FakeTransport(Transport):
    def __init__(self, services=("oci_jj.v1.VcsService",), responses=None):
        self.calls = []
        self._services = list(services)
        self._responses = responses or {}

    def unary(self, service, method, request):
        self.calls.append({"service": service, "method": method, "request": dict(request), "kind": "unary"})
        return ResponseView(self._responses.get(method, {}))

    def server_stream(self, service, method, request):
        self.calls.append({"service": service, "method": method, "request": dict(request), "kind": "stream"})
        return iter([ResponseView(r) for r in self._responses.get(method, [])])

    def list_services(self):
        return list(self._services)


@pytest.fixture
def engine():
    return GrpcEngine(server="http://x:50051", repo="R", transport=FakeTransport())


def test_attach_bench_maps_repo_ref_baseline(engine):
    engine.exec(engine.call_attach_bench("cand", "base", "synthetic://xor", "f1"))
    rec = engine._transport.calls[-1]
    assert rec["service"] == "oci_jj.v1.VcsService"
    assert rec["method"] == "AttachBench"
    # repo = oci-jj repository; ref = candidate; baseline_ref = checkpoint (the mapping subtlety)
    assert rec["request"] == {
        "repo": "R", "ref": "cand", "run": True,
        "dataset_uri": "synthetic://xor", "metric": "f1", "baseline_ref": "base",
    }


def test_attach_bench_without_baseline_omits_field(engine):
    engine.exec(engine.call_attach_bench("cand", None, "synthetic://xor", "f1"))
    assert "baseline_ref" not in engine._transport.calls[-1]["request"]


def test_hunt_maps_to_run_hunt_env_ref(engine):
    engine.exec(engine.call_hunt("cand", "synthetic://xor"))
    rec = engine._transport.calls[-1]
    assert rec["method"] == "RunHunt"
    assert rec["request"] == {"repo": "R", "env_ref": "cand", "dataset_uri": "synthetic://xor"}


def test_diff_bench_maps_from_to_depth(engine):
    engine.exec(engine.call_diff_bench("base", "cand"))
    rec = engine._transport.calls[-1]
    assert rec["method"] == "Diff"
    assert rec["request"] == {"repo": "R", "from_ref": "base", "to_ref": "cand", "depth": "BENCH"}


def test_provenance_maps_repo_ref(engine):
    engine.exec(engine.call_provenance("cand"))
    rec = engine._transport.calls[-1]
    assert rec["method"] == "Provenance"
    assert rec["request"] == {"repo": "R", "ref": "cand"}


def test_log_is_server_streaming():
    t = FakeTransport(responses={"Log": [{"commit_id": "a"}, {"commit_id": "b"}]})
    e = GrpcEngine(server="http://x:50051", repo="R", transport=t)
    out = e.exec(e.call_log("rl-gym-env"))
    assert [v.get("commit_id") for v in out] == ["a", "b"]
    assert t.calls[-1]["kind"] == "stream"


def test_api_floor_passes_with_v1_service():
    # Floor is checked lazily on first exec, not at construction (so offline --dry-run never reflects).
    e = GrpcEngine(server="s", repo="R", transport=FakeTransport(services=["oci_jj.v1.VcsService"]))
    e.exec(e.call_provenance("x"))  # does not raise


def test_api_floor_raises_without_v1_service():
    e = GrpcEngine(
        server="s", repo="R",
        transport=FakeTransport(services=["grpc.reflection.v1alpha.ServerReflection"]),
    )
    with pytest.raises(ApiFloorError):
        e.exec(e.call_provenance("x"))


def test_response_view_reads_rendered():
    t = FakeTransport(responses={"Diff": {"rendered": "scorecard", "available": True}})
    e = GrpcEngine(server="s", repo="R", transport=t)
    resp = e.exec(e.call_diff_bench("base", "cand"))
    assert resp.get("rendered") == "scorecard"


class _FakeRpcError(Exception):
    """Mimics grpc.RpcError (duck-typed via code()/details())."""

    def code(self):
        return "INTERNAL"

    def details(self):
        return "unknown ref rl-gym-env:cartpole"


def test_server_rpc_error_becomes_failed_result_not_traceback():
    class Boom(Transport):
        def unary(self, *a):
            raise _FakeRpcError()

        def list_services(self):
            return ["oci_jj.v1.VcsService"]

    e = GrpcEngine(server="s", repo="R", transport=Boom())
    result = e.exec(e.call_attach_bench("cand", "base", "synthetic://xor", "f1"))
    assert getattr(result, "returncode", 0) == 1
    assert "unknown ref" in result.get("unavailable_reason")
