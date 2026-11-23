"""Opt-in integration suite: the evaluate commands against a real oci-jj-server.

Skipped unless ``BRIEFCASE_OCIJJ_ADDR`` names a running server, so the default
``pytest tests/`` run never needs Docker or a network. Re-run it in CI or by hand
against any oci-jj stack (``briefcase-ai engine up``, oci-jj's own compose, or a
natively built ``oci-jj-server``):

    BRIEFCASE_OCIJJ_ADDR=http://127.0.0.1:50051 \\
    BRIEFCASE_OCIJJ_REPO=rl-gym-env \\
    BRIEFCASE_OCIJJ_CANDIDATE=cand BRIEFCASE_OCIJJ_BASELINE=base \\
    pytest tests/cli/test_ocijj_live.py -v

Prerequisites:

* the ``run`` extra (``pip install 'briefcase-ai[run]'``: grpcio, grpcio-reflection,
  protobuf);
* two refs in ``BRIEFCASE_OCIJJ_REPO``, a candidate and its baseline (defaults
  ``cand`` and ``base``), pushed through oci-jj's gateway, for example with
  ``crane append`` (see ``docs/features/engine.md``);
* for a secured server, the same settings oci-jj's own clients read:
  ``OCI_JJ_AUTH_TOKEN``, ``OCI_JJ_TLS_CA``, ``OCI_JJ_TLS_CLIENT_CERT`` and
  ``OCI_JJ_TLS_CLIENT_KEY``. With a token set, the suite also checks that a call
  without it is refused. ``BRIEFCASE_OCIJJ_READER_TOKEN`` (a ``:reader`` token)
  adds the read-only-role check.

Runs are queued, not scored: scoring needs oci-jj's private verdict worker, so
``run results`` is only checked for a clean exit (the server answers "not
available" until a verdict is recorded). Each run submit enqueues one real
``verdict_queue`` job on the server.
"""
import os
import uuid

import pytest

pytest.importorskip("grpc")
pytest.importorskip("grpc_reflection")

ADDR = os.environ.get("BRIEFCASE_OCIJJ_ADDR", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ADDR, reason="set BRIEFCASE_OCIJJ_ADDR to run against a live oci-jj-server"),
]

REPO = os.environ.get("BRIEFCASE_OCIJJ_REPO", "rl-gym-env")
CANDIDATE = os.environ.get("BRIEFCASE_OCIJJ_CANDIDATE", "cand")
BASELINE = os.environ.get("BRIEFCASE_OCIJJ_BASELINE", "base")


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Run ``briefcase-ai <argv>`` in-process against the live server with a throwaway store."""
    from briefcase.cli.__main__ import main
    from briefcase.cli.grpc_engine import ReflectionTransport

    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / "bc"))
    monkeypatch.setenv("BRIEFCASE_NO_INPUT", "1")
    monkeypatch.setenv("BRIEFCASE_ENGINE", "grpc")
    monkeypatch.setenv("OCI_JJ_SERVER", ADDR)
    monkeypatch.setenv("OCI_JJ_REPO", REPO)
    ReflectionTransport._CACHE.clear()

    def run(*argv):
        return main(list(argv))

    run("dataset", "register", "xor", "--uri", "synthetic://xor")
    yield run
    ReflectionTransport._CACHE.clear()


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_doctor_sees_the_oci_jj_v1_api(cli, capsys):
    cli("doctor")
    out = capsys.readouterr().out
    assert "[ok]   server:" in out and "oci_jj.v1 reflection" in out


def test_dataset_and_secret_registries(cli, capsys):
    assert cli("dataset", "list") == 0
    assert "xor\tsynthetic://xor" in capsys.readouterr().out
    assert cli("secret", "set", "EXAMPLE_KEY=value") == 0
    assert cli("secret", "list") == 0
    out = capsys.readouterr().out
    assert "EXAMPLE_KEY" in out and "value" not in out


def test_gate_run_lifecycle(cli, capsys):
    from briefcase.cli.state import Store

    name = _name("gate")
    assert cli("run", "submit", name, "--repository", CANDIDATE, "--checkpoint", BASELINE,
               "--dataset", "xor") == 0
    out = capsys.readouterr().out
    assert "server job" in out
    run = Store().get_run(name)
    assert run["status"] == "submitted" and run["job_id"] > 0

    assert cli("run", "list") == 0
    assert name in capsys.readouterr().out

    assert cli("run", "inspect", name) == 0
    out = capsys.readouterr().out
    assert f"{REPO}:{CANDIDATE}" in out  # Provenance rendered by the server

    assert cli("run", "results", name) == 0
    capsys.readouterr()

    assert cli("run", "stop", name) == 0
    assert Store().get_run(name)["status"] == "stopped"
    assert cli("run", "delete", name) == 0
    assert Store().get_run(name) is None


def test_hunt_run_is_queued(cli, capsys):
    from briefcase.cli.state import Store

    name = _name("hunt")
    assert cli("run", "submit", name, "--repository", CANDIDATE, "--dataset", "xor",
               "--mode", "hunt") == 0
    assert Store().get_run(name)["job_id"] > 0


def test_results_without_checkpoint_asks_for_one(cli, capsys):
    name = _name("nochk")
    assert cli("run", "submit", name, "--repository", CANDIDATE, "--dataset", "xor") == 0
    capsys.readouterr()
    assert cli("run", "results", name) == 2
    assert "--checkpoint" in capsys.readouterr().err


def test_unknown_ref_is_reported_and_marked_error(cli, capsys):
    from briefcase.cli.state import Store

    name = _name("bad")
    assert cli("run", "submit", name, "--repository", "no-such-ref-" + uuid.uuid4().hex[:6],
               "--checkpoint", BASELINE, "--dataset", "xor") == 1
    assert "unknown ref" in capsys.readouterr().out
    assert Store().get_run(name)["status"] == "error"


def test_dry_run_builds_the_call_without_sending_it(cli, capsys):
    from briefcase.cli.state import Store

    name = _name("dry")
    assert cli("--dry-run", "run", "submit", name, "--repository", CANDIDATE,
               "--checkpoint", BASELINE, "--dataset", "xor") == 0
    assert "DRY RUN: AttachBench(" in capsys.readouterr().out
    assert "job_id" not in Store().get_run(name)


@pytest.mark.skipif(not os.environ.get("OCI_JJ_AUTH_TOKEN"), reason="server auth not configured")
def test_call_without_token_is_refused(cli, monkeypatch, capsys):
    monkeypatch.delenv("OCI_JJ_AUTH_TOKEN")
    assert cli("run", "submit", _name("noauth"), "--repository", CANDIDATE, "--dataset", "xor") == 1
    assert "OCI_JJ_AUTH_TOKEN" in capsys.readouterr().err


@pytest.mark.skipif(not os.environ.get("BRIEFCASE_OCIJJ_READER_TOKEN"), reason="no reader token given")
def test_reader_token_cannot_submit(cli, monkeypatch, capsys):
    monkeypatch.setenv("OCI_JJ_AUTH_TOKEN", os.environ["BRIEFCASE_OCIJJ_READER_TOKEN"])
    assert cli("run", "submit", _name("ro"), "--repository", CANDIDATE, "--checkpoint", BASELINE,
               "--dataset", "xor") == 1
    assert "read-only" in capsys.readouterr().out
