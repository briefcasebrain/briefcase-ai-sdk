"""OciJJEngine: pure argv builders (mapped to the real oci-jj surface) + injected runner."""
import os

import pytest

from briefcase.cli.engine import OciJJEngine


@pytest.fixture
def eng():
    return OciJJEngine(binary="oci-jj", server="S", repo="R")


def base():
    return ["oci-jj", "--server", "S", "--repo", "R"]


def test_argv_attach_bench(eng):
    assert eng.argv_attach_bench("cand", "base", "synthetic://xor", "f1") == base() + [
        "attach-bench", "cand", "--run", "--baseline", "base",
        "--dataset", "synthetic://xor", "--metric", "f1",
    ]


def test_argv_attach_bench_without_baseline(eng):
    assert eng.argv_attach_bench("cand", None, "synthetic://xor", "f1") == base() + [
        "attach-bench", "cand", "--run", "--dataset", "synthetic://xor", "--metric", "f1",
    ]


def test_argv_hunt(eng):
    assert eng.argv_hunt("cand", "synthetic://xor", base_cols="x0,x1", tag_prefix="hunt") == base() + [
        "hunt", "cand", "--dataset", "synthetic://xor", "--base-cols", "x0,x1", "--tag-prefix", "hunt",
    ]
    assert eng.argv_hunt("cand", "synthetic://xor") == base() + [
        "hunt", "cand", "--dataset", "synthetic://xor",
    ]


def test_argv_diff_bench(eng):
    assert eng.argv_diff_bench("base", "cand") == base() + ["diff", "base", "cand", "--depth", "bench"]


def test_argv_log_provenance(eng):
    assert eng.argv_log("rl-gym-env") == base() + ["log", "rl-gym-env"]
    assert eng.argv_provenance("cand") == base() + ["provenance", "cand"]


def test_argv_checkout(eng):
    assert eng.argv_checkout("cand", paths=["/a", "/b"], dest="/out") == base() + [
        "checkout", "cand", "--paths", "/a,/b", "--dest", "/out",
    ]
    assert eng.argv_checkout("cand") == base() + ["checkout", "cand"]


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_OCIJJ_BIN", "/usr/local/bin/oci-jj")
    monkeypatch.setenv("OCI_JJ_SERVER", "http://example:50051")
    monkeypatch.setenv("OCI_JJ_REPO", "myrepo")
    e = OciJJEngine()
    assert e.argv_log("fam")[:5] == [
        "/usr/local/bin/oci-jj", "--server", "http://example:50051", "--repo", "myrepo",
    ]


def test_exec_merges_injected_env():
    captured = {}

    def fake_runner(argv, env=None, **kw):
        captured["argv"] = argv
        captured["env"] = env
        return 0

    e = OciJJEngine(runner=fake_runner)
    e.exec(["oci-jj", "log", "fam"], env={"INJECTED": "1"})
    assert captured["argv"] == ["oci-jj", "log", "fam"]
    assert captured["env"]["INJECTED"] == "1"
    # base environment is preserved (PATH almost always present)
    assert "PATH" in captured["env"] or set(os.environ) <= set(captured["env"])
