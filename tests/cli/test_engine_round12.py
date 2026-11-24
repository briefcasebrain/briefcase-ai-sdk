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
    assert [c for c in compose_calls(env) if "start" in c["argv"]]  # they exist: started, not created


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
    assert [c for c in compose_calls(env) if "start" in c["argv"]]


# --- G: `up` starts what exists with a command that cannot remove anything --------------------------------------

def compose_verbs(env):
    return [c["argv"] for c in compose_calls(env) if any(v in c["argv"] for v in ("up", "start", "down", "rm"))]


def test_up_over_existing_containers_uses_start_which_cannot_scale_down(env, monkeypatch, capsys):
    from tests.cli.engine_fakes import docker_state
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))[:2]))  # server and gateway
    monkeypatch.setenv("FAKE_DOCKER_ADD_REPLICA", "gateway")  # another client adds a replica after our snapshot
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    verbs = compose_verbs(env)
    assert len(verbs) == 1 and "start" in verbs[0] and "up" not in verbs[0], verbs
    assert docker_state(env["log"]).get("removed", []) == [], "a container that appeared later was removed"


def test_up_on_a_fresh_stack_creates_it_without_recreating_or_removing(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    (verb,) = compose_verbs(env)
    assert "up" in verb and "--no-recreate" in verb and "--remove-orphans" not in verb


def test_up_refuses_a_partial_stack_instead_of_mixing_start_and_create(env, monkeypatch, capsys):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))[:1]))  # no gateway container
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert compose_verbs(env) == [] and "gateway" in out.err + out.out


# --- the daemon's own id is part of its identity ------------------------------------------------------------

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


def test_a_record_without_a_daemon_id_accepts_the_same_endpoint_and_gains_the_id(env, monkeypatch, capsys):
    synthetic(env, [A], A)  # recorded "default unix:///var/run/docker.sock" by an earlier SDK
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    assert json.loads(_engine.installed_path().read_text())["docker_contexts"] == [
        "default unix:///var/run/docker.sock id=AAA"]


def test_a_same_version_tls_upgrade_on_a_different_daemon_is_refused_and_rolled_back(env, monkeypatch, key, capsys):
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    path, before = tls_upgrade(env, monkeypatch, key)
    monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # a valid pin, to a daemon that runs nothing
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", "[]")
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert _engine.current_install()["tls"] is False and path.read_bytes() == before


# --- F2: names are normalised once and the destination is exact ---------------------------------------------

def stack_with(env, monkeypatch, tls=False, extra=("workers", "verdict-worker")):
    """A synthetic install whose Compose file and record also define ``extra`` services (manifest spelling keeps
    the hyphen in ``image_ids``), every container created for the record, none running. Returns the rows."""
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    state = json.loads(_engine.installed_path().read_text())
    record = state["installs"][A]
    record["tls"] = tls
    grpc = ("https://" if tls else "") + "oci-jj-server:50051"
    compose_path = record["compose"]
    text = open(compose_path).read()
    if tls:
        text = text.replace('OCI_JJ_ALLOW_PLAINTEXT: "1"', "OCI_JJ_TLS_CERT: /tls/server.crt")
        text = text.replace("OCI_JJ_SERVER_GRPC: oci-jj-server:50051", f"OCI_JJ_SERVER_GRPC: {grpc}")
    rows = [list(r) for r in containers_for(A)[:2]]
    for name in extra:
        image_id = "sha256:" + name[0] * 64
        record["image_ids"][name] = image_id
        text += (f"  {name}:\n    image: localhost/briefcase-engine/{name}:{A}\n    pull_policy: never\n"
                 f"    environment:\n      OCI_JJ_SERVER_GRPC: {grpc}\n")
        rows.append([name, image_id, compose_path])
    open(compose_path, "w").write(text)
    _engine.installed_path().write_text(json.dumps(state))
    return stopped(rows), grpc


def test_the_matching_full_stack_with_a_hyphenated_service_is_started(env, monkeypatch, capsys):
    rows, _ = stack_with(env, monkeypatch)
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "start" in c["argv"]]


def test_a_stopped_verdict_worker_of_another_image_is_refused(env, monkeypatch, capsys):
    rows, _ = stack_with(env, monkeypatch)
    for row in rows:
        if row[0] == "verdict-worker":
            row[1] = "sha256:" + "f" * 64  # recorded as image_ids["verdict-worker"]; this one is another image
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert compose_verbs(env) == [] and "verdict-worker" in out.err + out.out


@pytest.mark.parametrize("service", ["workers", "verdict-worker", "gateway"])
@pytest.mark.parametrize("wrong", ["https://evil.example:443", "oci-jj-server:50052", "http://oci-jj-server:50051"])
def test_a_client_that_dials_another_destination_than_the_servers_is_refused(env, monkeypatch, capsys, service, wrong):
    rows, grpc = stack_with(env, monkeypatch, tls=True)
    for row in rows:
        if row[0] == service:
            row.append([f"OCI_JJ_SERVER_GRPC={wrong}"])
        elif row[0] == "oci-jj-server":
            row.append(["OCI_JJ_TLS_CERT=/tls/server.crt"])
        else:
            row.append([f"OCI_JJ_SERVER_GRPC={grpc}"])
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert compose_verbs(env) == [] and service in out.err + out.out


# --- G2: a fresh stack's `up` is verified afterwards, and never claims success over a removal ------------------

def test_up_on_a_fresh_stack_fails_when_compose_removed_a_container_during_the_start(env, monkeypatch, capsys):
    """Every Compose v5.5.0 create path (up, create) reconciles, and reconcile.go scales a service down to its expected
    count, so a second client creating replicas during `up` can have one removed. That cannot be prevented, so it is
    detected from the daemon's destroy events and reported, never passed off as a start."""
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("FAKE_DOCKER_DESTROY_ON_UP", "briefcase-engine-gateway-2")
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert "briefcase-engine-gateway-2" in out.err + out.out and "started" not in out.out


def test_up_on_a_fresh_stack_reports_success_when_nothing_was_removed(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    assert main(["engine", "up"]) == 0
    assert "started" in capsys.readouterr().out


# --- can-follows -------------------------------------------------------------------------------------------

def test_the_fake_lists_every_service_of_the_generated_compose_file(env, key, capsys):
    from pathlib import Path

    from briefcase.engine import compose as _compose
    from tests.cli.test_engine_install import _install

    assert _install("--code", "bci_good") == 0
    record = _engine.current_install()
    capsys.readouterr()
    import re

    text = open(record["compose"]).read().split("\nvolumes:\n")[0]
    declared = set(re.findall(r"^  (\S+):\n", text, re.M))
    assert {"postgres", "minio", "registry", "oci-jj-server", "gateway", "workers"} <= declared
    argv = _compose.compose_argv(Path(record["compose"]), Path(record["env_file"]), "config", "--services")
    out = engine_commands._run(argv, capture_output=True, text=True)
    assert set(out.stdout.split()) == declared


def test_up_wait_over_existing_containers_starts_then_waits_without_start_wait_flags(env, monkeypatch, capsys):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))[:2]))
    waited = []
    monkeypatch.setattr(engine_commands, "wait_running", lambda record, *a, **k: waited.append(record) or True)
    assert main(["engine", "up", "--wait"]) == 0
    capsys.readouterr()
    (start,) = [c["argv"] for c in compose_calls(env) if "start" in c["argv"]]
    assert "--wait" not in start and waited


def test_wait_running_waits_for_health_and_gives_up(monkeypatch):
    answers = iter([[{"service": "s", "state": "running", "health": "starting"}],
                    [{"service": "s", "state": "running", "health": "healthy"}]])
    monkeypatch.setattr(engine_commands, "_services", lambda record: next(answers))
    assert engine_commands.wait_running({}, timeout=5, interval=0)
    monkeypatch.setattr(engine_commands, "_services",
                        lambda record: [{"service": "s", "state": "exited", "health": ""}])
    assert not engine_commands.wait_running({}, timeout=0, interval=0)


# --- round 15: an identity without the daemon's id is never recorded or trusted -------------------------------

def test_up_refuses_and_records_nothing_when_the_daemons_id_cannot_be_read(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    record_daemons([])
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "info")
    assert main(["engine", "up"]) != 0
    capsys.readouterr()
    assert json.loads(_engine.installed_path().read_text()).get("docker_contexts") == []


def test_a_purge_is_refused_when_the_daemon_cannot_state_its_id_even_if_the_record_has_none(env, monkeypatch, capsys):
    synthetic(env, [A], A)  # an old record: endpoint only
    configure(env, monkeypatch, "ok", "none", "none")  # the daemon here may be a replacement: it cannot say who it is
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "info")
    assert main(["engine", "uninstall", "--purge", "--yes"]) != 0
    capsys.readouterr()
    assert (env["home"] / "engine" / "engine.env").exists()


def test_a_daemon_that_changes_while_down_waits_for_confirmation_receives_no_deletion(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    record_daemons(["default unix:///var/run/docker.sock id=AAA"])
    configure(env, monkeypatch, "ok", "ours", "A")
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    real = engine_commands._authorize

    def retarget_then_confirm(*args, **kwargs):
        token = real(*args, **kwargs)
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # the tunnel now reaches another daemon at the same address
        return token

    monkeypatch.setattr(engine_commands, "_authorize", retarget_then_confirm)
    assert main(["engine", "down", "--volumes", "--yes"]) != 0
    out = capsys.readouterr()
    assert [c for c in compose_calls(env) if "down" in c["argv"]] == []
    assert [c for c in calls(env["log"]) if c["argv"][:2] == ["volume", "rm"]] == []
    assert "changed" in out.err + out.out


def test_a_refused_upgrade_on_the_wrong_daemon_leaves_the_history_so_the_right_daemon_can_upgrade(
        env, monkeypatch, key, capsys):
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    before = json.loads(_engine.installed_path().read_text())["docker_contexts"]
    monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", "[]")
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert json.loads(_engine.installed_path().read_text())["docker_contexts"] == before
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")  # back on the daemon that runs the old engine
    rows = [["oci-jj-server", _engine.current_install()["image_ids"]["server"], _engine.current_install()["compose"]]]
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) == 0
