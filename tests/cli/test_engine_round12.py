"""Regression tests for legacy Docker identity, ownership and safe cleanup."""
import json

import pytest

from briefcase import engine as _engine
from briefcase.cli import engine_commands
from briefcase.cli.__main__ import main

from tests.cli.engine_fakes import calls
from tests.cli.test_engine_invariants import A, configure, synthetic

ENDPOINT = "tcp://remote:2376"


def named_context(env, monkeypatch, tlspath=None):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = [f"tlsctx {ENDPOINT}"]
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "A", context="other")
    monkeypatch.setenv("FAKE_DOCKER_CONTEXT", "tlsctx")
    monkeypatch.setenv("FAKE_DOCKER_HOST", ENDPOINT)
    if tlspath:
        monkeypatch.setenv("FAKE_DOCKER_TLSPATH", tlspath)


def test_a_context_repointed_while_down_waits_cannot_redirect_the_deletion(env, monkeypatch, capsys):
    named_context(env, monkeypatch)
    real = engine_commands._authorize

    def repoint_then_confirm(*args, **kwargs):
        token = real(*args, **kwargs)
        monkeypatch.setenv("FAKE_DOCKER_HOST", "tcp://attacker:2376")  # the context now names another daemon
        return token

    monkeypatch.setattr(engine_commands, "_authorize", repoint_then_confirm)
    assert main(["engine", "down", "--volumes", "--yes"]) == 0
    capsys.readouterr()
    deleting = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]
                or c["argv"][:2] == ["volume", "rm"]]
    assert deleting, "nothing was deleted"
    assert all(c["host"] == ENDPOINT and c["context"] is None for c in deleting), \
        [(c["argv"][:2], c["host"], c["context"]) for c in deleting]


def test_a_context_with_certificates_is_pinned_with_its_certificates(env, monkeypatch, capsys):
    named_context(env, monkeypatch, tlspath="/ctx/tls/abc")
    assert main(["engine", "down", "--yes"]) == 0
    capsys.readouterr()
    (down,) = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert (down["host"], down["tls_verify"], down["cert_path"]) == (ENDPOINT, "1", "/ctx/tls/abc/docker")


def test_an_explicit_docker_host_keeps_the_tls_variables_that_define_it(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = [f"default {ENDPOINT}"]
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "A")
    monkeypatch.setenv("DOCKER_HOST", ENDPOINT)
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/my/certs")
    assert main(["engine", "down", "--yes"]) == 0
    capsys.readouterr()
    (down,) = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert (down["host"], down["tls_verify"], down["cert_path"]) == (ENDPOINT, "1", "/my/certs")


def test_a_context_that_only_skips_verification_is_pinned_with_tls_but_no_verification(env, monkeypatch, capsys):
    named_context(env, monkeypatch, tlspath="/ctx/tls/abc")
    monkeypatch.setenv("FAKE_DOCKER_SKIPTLS", "true")
    assert main(["engine", "down", "--yes"]) == 0
    capsys.readouterr()
    (down,) = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert (down["host"], down["tls"], down["tls_verify"]) == (ENDPOINT, "1", None)


def test_a_skip_verify_context_without_certificates_is_pinned_with_tls_and_empty_certificate_flags(
        env, monkeypatch, capsys):
    """Checked on Docker 29.8.2 against a TLS server that does not ask for client certificates: `docker -H <host> --tls
    --tlscacert= --tlscert= --tlskey= version` answers, which is what the CLI does for such a context; DOCKER_TLS=1
    and a bare `--tls` fail on the missing ca.pem."""
    named_context(env, monkeypatch, tlspath="/ctx/tls/abc")
    monkeypatch.setenv("FAKE_DOCKER_SKIPTLS", "true")
    monkeypatch.setenv("FAKE_DOCKER_NOCERTS", "1")
    assert main(["engine", "down", "--yes"]) == 0
    capsys.readouterr()
    (down,) = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert down["host"] == ENDPOINT and down["tls_flags"] == ["--tls", "--tlscacert=", "--tlscert=", "--tlskey="]
    assert down["tls"] is None and down["cert_path"] is None


def test_the_default_context_with_tls_in_memory_keeps_the_callers_tls_variables(env, monkeypatch, capsys):
    """Docker 29.8.2 reports `<IN MEMORY>` as the storage path of the default context built from DOCKER_HOST and
    DOCKER_TLS_VERIFY/DOCKER_CERT_PATH; there is no `<IN MEMORY>/docker` directory, the variables are the material."""
    named_context(env, monkeypatch, tlspath="<IN MEMORY>")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/my/certs")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = [f"tlsctx {ENDPOINT}"]
    _engine.installed_path().write_text(json.dumps(state))
    assert main(["engine", "down", "--yes"]) == 0
    capsys.readouterr()
    (down,) = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert down["cert_path"] == "/my/certs" and down["tls_verify"] == "1"


def record_daemons(entries):
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = entries
    _engine.installed_path().write_text(json.dumps(state))


def test_a_recorded_daemon_id_that_matches_lets_a_purge_proceed(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    record_daemons(["default unix:///var/run/docker.sock id=AAA"])
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    capsys.readouterr()
    assert [c for c in calls(env["log"]) if c["argv"][:2] == ["volume", "rm"]]


def test_another_daemon_behind_the_same_endpoint_is_not_the_recorded_one(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    record_daemons(["default unix:///var/run/docker.sock id=AAA"])
    configure(env, monkeypatch, "ok", "none", "none")  # the replacement daemon is empty
    monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")
    assert main(["engine", "uninstall", "--purge", "--yes"]) != 0
    capsys.readouterr()
    assert (env["home"] / "engine" / "engine.env").exists(), "credentials the original daemon still needs were deleted"


def test_a_context_with_a_client_certificate_but_no_ca_is_pinned_with_an_empty_ca_flag(env, monkeypatch, capsys):
    """Checked on Docker 29.8.2: with DOCKER_CERT_PATH and DOCKER_TLS_VERIFY alone the CLI reads a ca.pem that is not
    there; `--tlscacert=` makes it use the system roots, which is what `--context` does for such a context."""
    named_context(env, monkeypatch, tlspath="/ctx/tls/abc")
    monkeypatch.setenv("FAKE_DOCKER_TLSFILES", "cert.pem,key.pem")
    assert main(["engine", "down", "--yes"]) == 0
    capsys.readouterr()
    (down,) = [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert down["tls_flags"] == ["--tlscacert="]
    assert (down["host"], down["tls_verify"], down["cert_path"]) == (ENDPOINT, "1", "/ctx/tls/abc/docker")


def test_a_legacy_purge_does_not_follow_a_daemon_that_changed_after_the_preview(env, monkeypatch, capsys):
    synthetic(env, [A], A)  # a record from before ids: the endpoint only
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    real = engine_commands._authorize

    def retarget_then_confirm(*args, **kwargs):
        token = real(*args, **kwargs)
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # another daemon behind the same address
        return token

    monkeypatch.setattr(engine_commands, "_authorize", retarget_then_confirm)
    assert main(["engine", "uninstall", "--purge", "--yes"]) != 0
    out = capsys.readouterr()
    assert [c for c in calls(env["log"]) if c["argv"][:2] == ["volume", "rm"]] == []
    assert (env["home"] / "engine" / "engine.env").exists() and "changed" in out.err + out.out


def test_confirmation_tokens_need_the_answer_that_earns_them():
    assert isinstance(engine_commands._mint("Go ahead?", True), engine_commands.Confirmed)
    for answer in (False, None):
        with pytest.raises(PermissionError):
            engine_commands._mint("Go ahead?", answer)


def test_removal_requires_verified_image_and_compose_file(monkeypatch):
    record = {"compose": "/fixture/compose.yml", "image_ids": {"server": "image-a"}}
    row = {"image": "image-a", "config": record["compose"], "service": "oci-jj-server"}

    def inventory(stopped, include_oneoffs):
        assert stopped and include_oneoffs
        return True, [row]

    monkeypatch.setattr(engine_commands, "project_containers", inventory)
    engine_commands.require_owned_containers(record)
    for key, value in (("image", "image-b"), ("config", "/another/compose.yml")):
        with monkeypatch.context() as patch:
            patch.setitem(row, key, value)
            with pytest.raises(RuntimeError, match="ownership"):
                engine_commands.require_owned_containers(record)


def test_uninstall_can_select_the_only_retained_release(env, monkeypatch, capsys):
    synthetic(env, [A], None)
    configure(env, monkeypatch, "ok", "none", "none")
    assert main(["engine", "uninstall", "--dry-run"]) == 0
    assert f"Uninstall engine {A}" in capsys.readouterr().out
    assert main(["engine", "uninstall", "--yes"]) == 0
    assert A not in _engine.load_state().get("installs", {})


@pytest.mark.parametrize("running", ["none", "B"])
@pytest.mark.parametrize("problem", ["inventory-failure", "missing-image-id", "missing-service"])
def test_client_uninstall_cannot_promote_an_unverified_image_release(env, monkeypatch, problem, running):
    from tests.cli.test_engine_invariants import B

    synthetic(env, [A, B], A)
    state = _engine.load_state()
    state["installs"][A].update(compose=None, images={}, image_ids={})
    configure(env, monkeypatch, "ok", "none", running)
    if problem == "missing-service":
        for field in ("images", "image_ids", "image_digests"):
            del state["installs"][B][field]["gateway"]
    elif problem == "missing-image-id":
        del state["installs"][B]["image_ids"]["gateway"]
    else:
        monkeypatch.setenv("FAKE_DOCKER_FAIL", "ps")
    _engine.installed_path().write_text(json.dumps(state))
    assert main(["engine", "uninstall", "--yes"]) == 0
    assert set(_engine.load_state()["installs"]) == {B}
    assert _engine.current_install() is None
