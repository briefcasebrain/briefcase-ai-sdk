"""`briefcase-ai graph`, `env`, `verify --graph`, `run attach` against an in-process gRPC server.

The server implements the VcsService methods these commands call, with message classes built
from the SDK's own vendored descriptor set (briefcase/engine/_proto/oci_jj_v1.binpb), so the test
exercises the real wire format, the bearer token and the error mapping. Skipped without grpcio.
"""
import json
from concurrent import futures

import grpc
import pytest
from google.protobuf import message_factory

from briefcase.cli.__main__ import main
from briefcase.cli.grpc_engine import _build_pool
from briefcase.engine import client as engine_client

SERVICE = "oci_jj.v1.VcsService"
TOKEN = "tenant-token"
COMMIT = {"commit_id": "c0ffee00-0000-4000-8000-000000000001", "repo_id": "r1",
          "manifest_digest": bytes.fromhex("ab" * 32), "mount_graph_id": bytes.fromhex("cd" * 32),
          "parents": ["base0000-0000-4000-8000-000000000000"], "builder": "docker", "message": "add torch",
          "artifact_class": "rl-env", "created_at": "2026-11-22T10:00:00Z"}


def _responses():
    return {
        "Log": [COMMIT],
        "WhoDepends": [dict(COMMIT, commit_id="dep00000-0000-4000-8000-000000000002")],
        "Diff": {"repo": "rl-gym-env", "available": True, "rendered": "+ torch 2.3.1\n- torch 2.2.0\n",
                 "structured_json": '{"added": ["torch 2.3.1"]}'},
        "Provenance": {"rendered": "cand <- base (derived_from)\n", "structured_json": "{}"},
        "Search": [{"repo": "rl-gym-env", "commit_id": COMMIT["commit_id"], "rank": 0.9, "snippet": "torch 2.3.1"}],
        "Resolve": {"provided_by": "--runtime python:3.11 (commit f0a1b2c3)", "tree_path": "/usr/bin/python3",
                    "segment": bytes.fromhex("ee" * 32),
                    "shadowed": [{"path": "/usr/bin/python3", "source": "base, masked by overlay precedence"}]},
        "ListReferrers": {"descriptors": []},
        "ListOwnReferrers": {"descriptors": [{"artifact_digest": bytes.fromhex("12" * 32),
                                              "artifact_type": "application/vnd.oci-jj.verdict.v1+json",
                                              "size": 512, "is_public": False}]},
        "Optimize": [{"action_id": "reorder-layers", "summary": "hot files first", "confidence": 0.4,
                      "effect": {"metric": "cold_start_ms", "baseline": 900.0, "projected": 400.0}}],
        "Fsck": {"ok": True, "trees_checked": 3, "inodes_checked": 40, "segments_checked": 12},
        "ListRefs": {"refs": [{"name": "cand", "kind": "tag", "commit_id": COMMIT["commit_id"], "is_head": True},
                              {"name": "base", "kind": "tag", "commit_id": COMMIT["parents"][0]}],
                     "head_count": 1},
        "SetRef": {"commit_id": COMMIT["commit_id"], "op_id": 41},
        "Undo": {"inverse_op_id": 42},
        "OpLog": [{"op_id": 40, "op_kind": "push"}, {"op_id": 41, "op_kind": "tag_move", "payload_json": "{}"}],
        "Compose": {"commit_id": "comp0000-0000-4000-8000-000000000003", "op_id": 43,
                    "warnings": ["no solver: hints only"], "exportability": "lossy"},
        "CheckoutPlan": {"graph_root": "blake3:aa", "files": [{"path": "/app/main.py", "mode": 420, "size": 10}],
                         "segments": [{"segment": bytes.fromhex("aa" * 32), "bytes": 10, "resolved": True}],
                         "total_bytes": 10, "file_count": 1, "skipped_count": 7, "unmatched_paths": ["/nope"]},
        "MountPlan": {"graph_root": "blake3:aa", "segment_count": 2, "total_bytes": 20, "file_count": 3},
        "WhoAmI": {"tenant": "acme", "role": "tenant", "registry_namespace": "acme"},
        "AttachBench": {"recorded": True, "summary_id": "sum-1", "edge_id": "edge-1", "op_id": 44,
                        "decision": "accept"},
    }


class FakeEngine:
    def __init__(self):
        self.pool = _build_pool(engine_client.descriptor_files())
        self.calls = []
        self.unimplemented = set()
        self.responses = _responses()
        handlers = {}
        svc = self.pool.FindServiceByName(SERVICE)
        for method in svc.methods:
            req_cls = message_factory.GetMessageClass(method.input_type)
            resp_cls = message_factory.GetMessageClass(method.output_type)
            handler = self._handler(method.name, resp_cls)
            if method.server_streaming:
                handlers[method.name] = grpc.unary_stream_rpc_method_handler(
                    handler, request_deserializer=req_cls.FromString,
                    response_serializer=resp_cls.SerializeToString)
            else:
                handlers[method.name] = grpc.unary_unary_rpc_method_handler(
                    handler, request_deserializer=req_cls.FromString,
                    response_serializer=resp_cls.SerializeToString)
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(SERVICE, handlers),))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()

    def _handler(self, name, resp_cls):
        def build(value):
            msg = resp_cls()
            _fill(msg, value)
            return msg

        def handle(request, context):
            meta = dict(context.invocation_metadata())
            if meta.get("authorization") != f"Bearer {TOKEN}":
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or unknown bearer token")
            self.calls.append((name, request))
            if name in self.unimplemented:
                context.abort(grpc.StatusCode.UNIMPLEMENTED, f"{name} is not implemented")
            value = self.responses.get(name, {})
            if isinstance(value, list):
                return iter([build(v) for v in value])
            return build(value)

        return handle

    def stop(self):
        self.server.stop(0)


def _fill(msg, value):
    for key, val in value.items():
        field = msg.DESCRIPTOR.fields_by_name[key]
        if field.message_type is not None:
            if isinstance(val, list):
                for item in val:
                    _fill(getattr(msg, key).add(), item)
            else:
                _fill(getattr(msg, key), val)
        elif isinstance(val, list):
            getattr(msg, key).extend(val)
        else:
            setattr(msg, key, val)


@pytest.fixture
def engine(shell_home, monkeypatch):
    fake = FakeEngine()
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", f"http://127.0.0.1:{fake.port}")
    monkeypatch.setenv("OCI_JJ_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    monkeypatch.delenv("OCI_JJ_REPO", raising=False)
    engine_client.DescriptorTransport._CACHE.clear()
    yield fake
    fake.stop()


def _req(fake, name):
    return [r for n, r in fake.calls if n == name]


def test_graph_log_text_and_json(engine, capsys):
    assert main(["graph", "log", "rl-gym-env", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "c0ffee00" in out and "add torch" in out and "Next: briefcase-ai graph show rl-gym-env" in out
    (req,) = _req(engine, "Log")
    assert req.repo == "rl-gym-env" and req.ref == "rl-gym-env" and req.limit == 5
    assert main(["graph", "log", "rl-gym-env", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["commits"][0]["manifest_digest"] == "ab" * 32  # bytes as hex


def test_graph_diff_uses_the_depth_enum(engine, capsys):
    assert main(["graph", "diff", "base", "cand", "--depth", "loaded-libs"]) == 0
    assert "+ torch 2.3.1" in capsys.readouterr().out
    assert _req(engine, "Diff")[0].depth == 5
    assert main(["graph", "diff", "base", "cand", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["structured"] == {"added": ["torch 2.3.1"]}


def test_graph_diff_unavailable_depth(engine, capsys):
    engine.responses["Diff"] = {"available": False, "unavailable_reason": "not available — telemetry class disabled"}
    assert main(["graph", "diff", "base", "cand", "--depth", "bench"]) == 0
    assert "telemetry class disabled" in capsys.readouterr().out


def test_graph_read_commands(engine, capsys):
    assert main(["graph", "provenance", "cand"]) == 0
    assert "derived_from" in capsys.readouterr().out
    assert main(["graph", "whodepends", "base"]) == 0
    assert "dep00000" in capsys.readouterr().out
    assert main(["graph", "search", "torch>=2.3"]) == 0
    assert "torch 2.3.1" in capsys.readouterr().out
    assert main(["graph", "show", "cand"]) == 0
    out = capsys.readouterr().out
    assert "manifest   sha256:" + "ab" * 32 in out and "derived_from" in out
    assert main(["graph", "explain", "cand"]) == 0
    out = capsys.readouterr().out
    assert "Changes from parent base (package depth)" in out and _req(engine, "Diff")[0].depth == 2
    assert _req(engine, "Diff")[0].from_ref == "base"  # Diff takes ref names, not commit ids
    assert main(["graph", "resolve", "cand:/usr/bin/python3"]) == 0
    out = capsys.readouterr().out
    assert "provided by --runtime python:3.11" in out and "shadows" in out
    assert _req(engine, "Resolve")[0].path == "/usr/bin/python3"
    assert main(["graph", "optimize", "cand"]) == 0
    assert "reorder-layers" in capsys.readouterr().out


def test_graph_referrers(engine, capsys):
    assert main(["graph", "referrers", "sha256:" + "ab" * 32]) == 0
    assert "Add --mine" in capsys.readouterr().out
    assert main(["graph", "referrers", "sha256:" + "ab" * 32, "--mine"]) == 0
    assert "(private)" in capsys.readouterr().out
    assert _req(engine, "ListOwnReferrers")[0].subject_digest == bytes.fromhex("ab" * 32)
    assert main(["graph", "referrers", "notadigest"]) == 2


def test_graph_search_empty_state(engine, capsys):
    engine.responses["Search"] = []
    assert main(["graph", "search", "nothing"]) == 0
    out = capsys.readouterr().out
    assert "nothing matches" in out and "Next:" not in out


def test_env_grpc_commands(engine, capsys):
    assert main(["env", "compose", "--runtime", "py311", "--runtime", "cuda12", "--app", "app", "--tag", "s1"]) == 0
    assert "lossy" in capsys.readouterr().out
    req = _req(engine, "Compose")[0]
    assert list(req.runtime) == ["py311", "cuda12"] and req.tag == "s1"
    assert main(["env", "checkout", "cand", "--paths", "/app,/nope"]) == 0
    out = capsys.readouterr().out
    assert "/app/main.py" in out and "/nope selected nothing" in out and "nothing was written" in out
    assert list(_req(engine, "CheckoutPlan")[0].paths) == ["/app", "/nope"]
    assert main(["env", "mount", "cand", "/mnt/x"]) == 0
    assert "snapshotter" in capsys.readouterr().out
    assert main(["env", "bookmark", "list"]) == 0
    assert "cand" in capsys.readouterr().out
    assert main(["env", "bookmark", "set", "prod", "cand", "--kind", "tag"]) == 0
    assert "op 41" in capsys.readouterr().out
    assert _req(engine, "SetRef")[0].kind == "tag"
    assert main(["env", "undo", "--op", "41"]) == 0
    assert "new op 42" in capsys.readouterr().out
    assert main(["env", "undo"]) == 2  # no terminal, no --yes: shows the newest op and refuses
    assert "newest operation: 41 tag_move" in capsys.readouterr().out
    assert main(["env", "undo", "--yes"]) == 0
    assert _req(engine, "Undo")[-1].op_ref == "41"


def test_env_commands_that_need_a_newer_engine(engine, capsys):
    assert main(["env", "checkout", "cand", "--dest", "/tmp/x"]) == 1
    assert "requires a newer engine" in capsys.readouterr().err
    assert main(["env", "gc"]) == 0
    assert "runs inside the engine" in capsys.readouterr().out
    assert main(["env", "gc", "--now"]) == 1


def test_verify_graph(engine, capsys):
    assert main(["verify", "--graph", "cand"]) == 0
    assert "intact (3 trees" in capsys.readouterr().out
    engine.responses["Fsck"] = {"ok": False, "problems": ["segment ee.. missing"]}
    assert main(["verify", "--graph", "cand", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "damaged"


def test_run_attach_records_the_verdict(engine, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE", "grpc")
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"decision": "accept"}))
    assert main(["run", "attach", "demo", "--result", str(verdict), "--repository", "cand",
                 "--checkpoint", "base"]) == 0
    assert "accept" in capsys.readouterr().out
    req = _req(engine, "AttachBench")[0]
    assert not req.run and json.loads(req.verdict_json) == {"decision": "accept"} and req.baseline_ref == "base"


def test_unimplemented_rpc_says_upgrade(engine, capsys):
    engine.unimplemented.add("Fsck")
    assert main(["verify", "--graph", "cand"]) == 1
    err = capsys.readouterr().err
    assert "not available on this engine" in err and "Fix: Contact your managed workspace administrator" in err


def test_wrong_token_is_explained(engine, capsys, monkeypatch):
    monkeypatch.setenv("OCI_JJ_AUTH_TOKEN", "wrong")
    engine_client.DescriptorTransport._CACHE.clear()
    assert main(["graph", "log", "x"]) == 1
    err = capsys.readouterr().err
    assert "refused the call" in err and "Traceback" not in err


def test_engine_down_is_explained(shell_home, capsys, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "http://127.0.0.1:1")
    assert main(["graph", "log", "x"]) == 1
    err = capsys.readouterr().err
    assert "not reachable" in err and "Fix: briefcase-ai engine status" in err


def test_grpc_engine_run_submit_uses_vendored_contract(engine, capsys, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE", "grpc")
    engine.responses["AttachBench"] = {"job_id": 7}
    assert main(["dataset", "register", "xor", "--uri", "synthetic://xor"]) == 0
    assert main(["run", "submit", "r1", "--repository", "cand", "--dataset", "xor", "--checkpoint", "base"]) == 0
    assert "server job 7" in capsys.readouterr().out
    req = _req(engine, "AttachBench")[-1]
    assert req.run and req.dataset_uri == "synthetic://xor"


def test_vendored_contract_matches_the_proto_files():
    """The descriptor set is what scripts/engine/regen_proto.py compiles from the vendored .proto files."""
    names = set(engine_client.descriptor_files())
    assert names == {"oci_jj/v1/common.proto", "oci_jj/v1/telemetry.proto", "oci_jj/v1/vcs.proto"}
    methods = set(engine_client.vendored_methods())
    used = {"Log", "Diff", "Search", "Provenance", "WhoDepends", "Resolve", "ListReferrers", "ListOwnReferrers",
            "Optimize", "Fsck", "ListRefs", "SetRef", "Undo", "Compose", "CheckoutPlan", "MountPlan",
            "AttachBench", "RunHunt", "WhoAmI"}
    assert used <= methods


def test_env_push_learns_the_namespace_from_whoami(engine, capsys):
    assert main(["env", "push", "my-env:1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "docker push 127.0.0.1:8080/acme/my-env:1" in out
    assert "docker login 127.0.0.1:8080 --username briefcase --password-stdin" in out
    assert _req(engine, "WhoAmI")
