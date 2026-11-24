"""Round 12: the verified daemon endpoint is the pin, ordinary `up` deletes nothing, and a failed settle always
restores what it changed.

  P1  after a named context is verified, every docker call uses its endpoint, not its name (a context repointed
      while `down` waits for the answer cannot redirect the deletion), and an upgrade probes what runs inside the pin
  U1  `up` never lets Compose remove containers: not through an inherited or env-file COMPOSE_REMOVE_ORPHANS, and
      not through excess replicas
  S1  an exception from the first probe of an install settle restores the record and the Compose file
  S2  an unreadable installed.json fails the settle instead of passing for a client-only install
  N1  `up` reuses stopped containers only when they match the current record
"""
import json
import os

import pytest

from briefcase import engine as _engine
from briefcase.cli import engine_commands
from briefcase.cli.__main__ import main

from tests.cli.engine_fakes import calls
from tests.cli.test_engine_invariants import A, configure, synthetic
from tests.cli.test_engine_round9 import tls_upgrade

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


def test_an_upgrade_probes_what_runs_inside_the_pin(env, monkeypatch, key, capsys):
    synthetic_tls = tls_upgrade(env, monkeypatch, key)  # noqa: F841 - leaves one server container running
    monkeypatch.setenv("FAKE_DOCKER_CONTEXT", "tlsctx")
    monkeypatch.setenv("FAKE_DOCKER_HOST", ENDPOINT)
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = [f"tlsctx {ENDPOINT}"]
    _engine.installed_path().write_text(json.dumps(state))
    before = len(calls(env["log"]))
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) == 0
    capsys.readouterr()
    probes = [c for c in calls(env["log"])[before:] if c["argv"][:1] in (["ps"], ["inspect"])]
    assert probes and all(c["host"] == ENDPOINT for c in probes), [(c["argv"][:2], c["host"]) for c in probes]


# --- U1: `up` deletes nothing -------------------------------------------------------------------

def compose_calls(env):
    return [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"]]


@pytest.mark.parametrize("where", ["caller_environment", "engine_env_file"])
def test_up_never_lets_compose_remove_orphans(env, monkeypatch, capsys, where):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    if where == "caller_environment":
        monkeypatch.setenv("COMPOSE_REMOVE_ORPHANS", "true")
    else:  # Compose imports COMPOSE_* variables that are unset in the process from --env-file
        with open(env["home"] / "engine" / "engine.env", "a") as handle:
            handle.write("COMPOSE_REMOVE_ORPHANS=true\n")
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    ups = [c for c in compose_calls(env) if "up" in c["argv"]]
    assert ups and all(c["orphans"] == "false" for c in ups), [c["orphans"] for c in ups]


def test_up_refuses_when_a_service_has_more_containers_than_it_will_keep(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "A")
    rows = json.loads(os.environ["FAKE_DOCKER_CONTAINERS"])
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows + [rows[1]]))  # a second gateway container
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"]] == []
    assert "gateway" in out.err + out.out


# --- S1/S2: the settle restores on any failure and refuses to read state permissively ---------------

def test_an_exception_from_the_first_probe_restores_the_record_and_the_compose_file(env, monkeypatch, key, capsys):
    path, before = tls_upgrade(env, monkeypatch, key)

    def boom():
        raise PermissionError("simulated docker socket")

    monkeypatch.setattr(engine_commands, "running_releases", boom)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert _engine.current_install()["tls"] is False and path.read_bytes() == before


def test_an_unreadable_record_fails_the_settle_instead_of_passing_for_client_only(env, capsys):
    from types import SimpleNamespace

    synthetic(env, [A], A)
    _engine.installed_path().write_text("{not json")
    opts = SimpleNamespace(env=None)
    code = engine_commands._settle(opts, {"version": A, "compose": "x"}, None, False, lambda *_: None)
    assert code not in (None, 0), "an unreadable installed.json was treated as a client-only install"
    assert _engine.installed_path().read_text() == "{not json"


def test_set_current_does_not_read_an_unreadable_record_as_empty(env):
    from briefcase.engine import install as _install_mod

    synthetic(env, [A], A)
    _engine.installed_path().write_text("{not json")
    with pytest.raises(_engine.StateUnreadable):
        _install_mod.set_current(A)


# --- N1: `up` reuses stopped containers only when they match the record ------------------------------

def stopped(rows, env_for_server=None):
    out = []
    for row in rows:
        row = list(row) + ["stopped"]
        if row[0] == "oci-jj-server" and env_for_server is not None:
            row.append(env_for_server)
        out.append(row)
    return out


def test_up_reuses_stopped_containers_that_match_the_record(env, monkeypatch, capsys):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))))
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"]]


def test_up_refuses_stopped_containers_made_from_another_release(env, monkeypatch, capsys):
    from tests.cli.test_engine_invariants import B, containers_for

    synthetic(env, [A, B], B)  # the record now says B; the stopped containers were made from A
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))))
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"]] == []
    assert "started" not in out.out


def test_a_tls_change_after_a_stop_is_not_reported_as_started_on_the_old_transport(env, monkeypatch, key, capsys):
    path, _ = tls_upgrade(env, monkeypatch, key)
    record = _engine.current_install()
    rows = stopped([["oci-jj-server", record["image_ids"]["server"], record["compose"]]],
                   ["OCI_JJ_ALLOW_PLAINTEXT=1"])
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) == 0  # nothing runs: no restart
    assert _engine.current_install()["tls"] is True
    capsys.readouterr()
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"] and "--no-recreate" in c["argv"]] == []
    assert "started" not in out.out and path.exists()


# --- E: the pin keeps the TLS identity and never falls back to an unpinned probe --------------------------------

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


def test_a_skip_verify_context_without_certificates_cannot_be_pinned_so_nothing_runs(env, monkeypatch, capsys):
    """Checked on Docker 29.8.2: no environment variable or flag selects TLS without verification when there are no
    certificate files (`--tls` and DOCKER_TLS both fail on the missing ca.pem), so such a context cannot be pinned."""
    named_context(env, monkeypatch, tlspath="/ctx/tls/abc")
    monkeypatch.setenv("FAKE_DOCKER_SKIPTLS", "true")
    monkeypatch.setenv("FAKE_DOCKER_NOCERTS", "1")
    assert main(["engine", "down", "--yes"]) != 0
    capsys.readouterr()
    assert [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"]] == []


def test_an_upgrade_that_cannot_identify_the_daemon_is_refused_even_when_the_probe_would_be_empty(
        env, monkeypatch, key, capsys):
    path, before = tls_upgrade(env, monkeypatch, key)
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", "[]")
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "context show")
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert _engine.current_install()["tls"] is False and path.read_bytes() == before


# --- F: every service the record defines is checked, from the real inspect template ----------------------------

def test_up_refuses_a_stopped_plaintext_gateway_for_a_tls_record_with_no_server_container(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["installs"][A]["tls"] = True
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "none")
    from tests.cli.test_engine_invariants import containers_for

    gateway = [row for row in containers_for(A) if row[0] == "gateway"]  # created from the plaintext compose file
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(gateway)))
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"]] == [] and "gateway" in out.err + out.out


def test_up_refuses_stopped_workers_made_for_the_other_transport(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    state = json.loads(_engine.installed_path().read_text())
    state["installs"][A]["image_ids"]["workers"] = "sha256:" + "w" * 64
    _engine.installed_path().write_text(json.dumps(state))
    rows = [["workers", "sha256:" + "w" * 64, "", "stopped", ["OCI_JJ_SERVER_GRPC=https://oci-jj-server:50051"]]]
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))  # TLS workers, plaintext record
    assert main(["engine", "up"]) == 1
    assert [c for c in compose_calls(env) if "up" in c["argv"]] == []
    capsys.readouterr()


def test_up_ignores_one_off_containers_when_counting_replicas(env, monkeypatch, capsys):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    rows = stopped(containers_for(A))
    gateway = rows[1]  # a `docker compose run gateway ...` leftover: Compose's own reconcile ignores one-off containers
    rows.append(gateway[:4] + [["OCI_JJ_SERVER_GRPC=https://x"], True])
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"]]
