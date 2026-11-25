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

from tests.cli.engine_fakes import build_release, calls
from tests.cli.test_engine_install import _install
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
    monkeypatch.setattr(engine_commands, "wait_running",
                        lambda record, services, *a, **k: waited.append(services) or True)
    assert main(["engine", "up", "--wait"]) == 0
    capsys.readouterr()
    (start,) = [c["argv"] for c in compose_calls(env) if "start" in c["argv"]]
    assert "--wait" not in start and waited


def test_wait_running_needs_every_expected_service_healthy_and_ignores_one_offs(monkeypatch):
    def row(service, state="running", health="", oneoff=False):
        return {"service": service, "state": state, "health": health, "oneoff": oneoff}

    answers = iter([[row("a", health="starting"), row("b")],              # a is not healthy yet
                    [row("a", health="healthy")],                          # b has no container
                    [row("a", health="healthy"), row("b"), row("b", "exited", oneoff=True)]])  # an exited one-off
    monkeypatch.setattr(engine_commands, "_services", lambda record, detail=False: next(answers))
    assert engine_commands.wait_running({}, {"a", "b"}, timeout=5, interval=0)
    subset = [row("a"), row("c")]  # a healthy subset only
    monkeypatch.setattr(engine_commands, "_services", lambda record, detail=False: subset)
    assert not engine_commands.wait_running({}, {"a", "b"}, timeout=0, interval=0)


def test_up_wait_polls_inside_the_daemon_pin(env, monkeypatch, capsys):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))[:2]))
    monkeypatch.setenv("FAKE_DOCKER_PS_ALL", "1")
    assert main(["engine", "up", "--wait"]) == 0
    capsys.readouterr()
    polls = [c for c in compose_calls(env) if "ps" in c["argv"] and "--all" in c["argv"]]
    assert polls and all(c["host"] == "unix:///var/run/docker.sock" for c in polls)


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


def test_the_upgrade_restart_that_removes_orphans_needs_the_confirmation_token(env, monkeypatch):
    synthetic(env, [A], A)
    record = _engine.current_install()
    with pytest.raises(TypeError):
        engine_commands.restart_removing_orphans(record, None)
    assert [c for c in compose_calls(env) if "--remove-orphans" in c["argv"]] == []


# --- round 16: the daemon shown before confirmation is the one used after it, on every destructive path ----------

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


def test_an_upgrade_does_not_restart_onto_a_daemon_that_never_stated_its_id(env, monkeypatch, key, capsys):
    path, before = tls_upgrade(env, monkeypatch, key)
    record_daemons([])  # no history: the early daemon check has nothing to compare
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "info")
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "--remove-orphans" in c["argv"]] == []
    assert _engine.current_install()["tls"] is False and path.read_bytes() == before


def test_a_refused_upgrade_keeps_a_daemon_another_command_recorded_meanwhile(env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    real = _install_mod.complete_online

    def install_then_the_world_changes(*args, **kwargs):
        record = real(*args, **kwargs)
        record_daemons(json.loads(_engine.installed_path().read_text())["docker_contexts"]
                       + ["other unix:///elsewhere.sock id=CCC"])  # recorded by something else meanwhile
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # and the daemon at this address is no longer the old one
        return record

    monkeypatch.setattr(_install_mod, "complete_online", install_then_the_world_changes)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    history = json.loads(_engine.installed_path().read_text())["docker_contexts"]
    assert "other unix:///elsewhere.sock id=CCC" in history, history


def test_a_client_only_upgrade_does_not_need_docker_because_of_old_daemon_history(env, monkeypatch, key, capsys):
    from tests.cli.test_engine_invariants import remove_docker

    assert _install("--code", "bci_good", "--no-images") == 0
    record_daemons(["default unix:///var/run/docker.sock id=AAA"])  # left by an earlier Compose install
    remove_docker(env, monkeypatch)
    env["platform"].release = build_release(key, version=_engine.current_install()["version"])
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--no-images"]) == 0


# --- round 16: observation is proven by delivery, never by a sleep ------------------------------------------------


# --- round 17 ---------------------------------------------------------------------------------------------------

def test_an_upgrade_that_installs_images_is_refused_early_on_the_wrong_daemon_even_from_a_client_only_install(
        env, monkeypatch, key, capsys):
    assert _install("--code", "bci_good", "--no-images") == 0
    record_daemons(["default unix:///var/run/docker.sock id=AAA"])
    monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")
    before = _engine.installed_path().read_text()
    env["platform"].release = build_release(key, version=_engine.current_install()["version"])
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good"]) != 0
    capsys.readouterr()
    assert _engine.installed_path().read_text() == before, "an install record was written before the daemon check"


def test_a_daemon_that_changes_during_the_upgrades_checks_receives_no_restart(env, monkeypatch, key, capsys):
    path, before = tls_upgrade(env, monkeypatch, key)
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    record_daemons(["default unix:///var/run/docker.sock id=AAA"])
    real = engine_commands.guard_start

    def checks_then_the_daemon_is_replaced(record):
        stop = real(record)
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # replaced behind the pinned endpoint during the inventory
        return stop

    monkeypatch.setattr(engine_commands, "guard_start", checks_then_the_daemon_is_replaced)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "--remove-orphans" in c["argv"]] == []
    assert _engine.current_install()["tls"] is False and path.read_bytes() == before


def test_a_rollback_removes_only_the_daemons_this_install_added(env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    real = _install_mod.complete_online

    def install_then_another_records_the_daemon_settle_will_see(*args, **kwargs):
        record = real(*args, **kwargs)
        record_daemons(json.loads(_engine.installed_path().read_text())["docker_contexts"]
                       + ["default unix:///var/run/docker.sock id=BBB"])  # owned by another operation
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")
        return record

    monkeypatch.setattr(_install_mod, "complete_online", install_then_another_records_the_daemon_settle_will_see)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    history = json.loads(_engine.installed_path().read_text())["docker_contexts"]
    assert "default unix:///var/run/docker.sock id=BBB" in history, history


# --- round 18 ---------------------------------------------------------------------------------------------------

def test_a_legacy_upgrade_does_not_restart_on_a_daemon_other_than_the_one_preflight_verified(
        env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    legacy = "default unix:///var/run/docker.sock"
    record_daemons([legacy])  # a record from before ids existed: it matches any daemon at that address
    real = _install_mod.complete_online

    def install_then_the_tunnel_moves(*args, **kwargs):
        record = real(*args, **kwargs)
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # settle will see another daemon behind the same address
        return record

    monkeypatch.setattr(_install_mod, "complete_online", install_then_the_tunnel_moves)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "--remove-orphans" in c["argv"]] == []
    # and the legacy entry the install completed with an id is put back as it was
    assert json.loads(_engine.installed_path().read_text())["docker_contexts"] == [legacy]


def test_confirmation_tokens_need_the_answer_that_earns_them():
    assert isinstance(engine_commands._mint("Go ahead?", True), engine_commands.Confirmed)
    for answer in (False, None):
        with pytest.raises(PermissionError):
            engine_commands._mint("Go ahead?", answer)


# --- round 19: `up` owns nothing it does not create through Compose ------------------------------------------------

@pytest.mark.parametrize("stack", ["fresh", "existing"])
def test_up_issues_no_docker_volume_creation_removal_or_events_command(env, monkeypatch, capsys, stack):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none" if stack == "fresh" else "ours", "none")
    if stack == "existing":
        monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(stopped(containers_for(A))[:2]))
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    used = [c["argv"] for c in calls(env["log"])]
    assert [a for a in used if a[:1] == ["events"]] == []
    assert [a for a in used if a[:2] in (["volume", "create"], ["volume", "rm"], ["volume", "prune"])] == []


def test_up_reports_an_expected_container_that_is_missing_after_the_start(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("FAKE_DOCKER_VANISH_ON_UP", "gateway")  # started, and gone when the inventory is taken
    assert main(["engine", "up"]) != 0
    out = capsys.readouterr()
    assert "started" not in out.out and "gateway" in out.err + out.out


def test_every_refusal_after_the_install_puts_back_the_legacy_history_entry_it_completed(
        env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    legacy = "default unix:///var/run/docker.sock"
    record_daemons([legacy])
    real = _install_mod.complete_online

    def install_then_the_daemon_cannot_be_identified(*args, **kwargs):
        record = real(*args, **kwargs)
        monkeypatch.setenv("FAKE_DOCKER_FAIL", "context show")  # settlement cannot say which daemon this is
        return record

    monkeypatch.setattr(_install_mod, "complete_online", install_then_the_daemon_cannot_be_identified)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert json.loads(_engine.installed_path().read_text())["docker_contexts"] == [legacy]


# --- round 20 ---------------------------------------------------------------------------------------------------

def test_fresh_up_rechecks_the_daemon_immediately_before_compose(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    real = engine_commands.existing_containers_state
    taken = []

    def inventory_then_the_tunnel_moves(record, **kwargs):
        result = real(record, **kwargs)
        if not taken:  # the inventory that decides between `up` and `start`
            taken.append(1)
            monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # a daemon whose replicas were never inventoried
        return result

    monkeypatch.setattr(engine_commands, "existing_containers_state", inventory_then_the_tunnel_moves)
    assert main(["engine", "up"]) != 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "up" in c["argv"] or "start" in c["argv"]] == []


def test_fresh_up_reports_a_daemon_that_changed_during_the_start(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    real = engine_commands._run

    def run_then_the_tunnel_moves(argv, **kw):
        result = real(argv, **kw)
        if argv[:2] == ["docker", "compose"] and "up" in argv:
            monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")
        return result

    monkeypatch.setattr(engine_commands, "_run", run_then_the_tunnel_moves)
    assert main(["engine", "up"]) != 0
    out = capsys.readouterr()
    assert "started" not in out.out and "changed" in out.err + out.out


def test_an_install_writes_no_daemon_history_so_a_transient_identity_is_never_recorded(env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    legacy = "default unix:///var/run/docker.sock"
    record_daemons([legacy])
    real = _install_mod.complete_online
    seen = []

    def install_while_the_endpoint_briefly_answers_as_b(*args, **kwargs):
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")  # a transient endpoint change during the install
        record = real(*args, **kwargs)
        seen.append(json.loads(_engine.installed_path().read_text())["docker_contexts"])
        monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")  # and settlement verifies A
        return record

    monkeypatch.setattr(_install_mod, "complete_online", install_while_the_endpoint_briefly_answers_as_b)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) == 0
    capsys.readouterr()
    assert seen == [[legacy]], "the install itself recorded a daemon"
    assert json.loads(_engine.installed_path().read_text())["docker_contexts"] == [f"{legacy} id=AAA"]


def test_a_refusal_keeps_a_daemon_another_up_recorded_between_install_and_settlement(env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    monkeypatch.setenv("FAKE_DOCKER_ID", "AAA")
    tls_upgrade(env, monkeypatch, key)
    real = _install_mod.complete_online

    def install_then_another_up_records_b(*args, **kwargs):
        record = real(*args, **kwargs)
        monkeypatch.setenv("FAKE_DOCKER_ID", "BBB")
        identity = _install_mod.docker_identity(engine_commands._runner())
        _install_mod.note_daemon(engine_commands._runner(), identity=identity)  # a successful `up` relied on B
        return record

    monkeypatch.setattr(_install_mod, "complete_online", install_then_another_up_records_b)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    history = json.loads(_engine.installed_path().read_text())["docker_contexts"]
    assert any("id=BBB" in entry for entry in history), history


# --- sidecars that the compose file pins by digest but the record does not -----------------------------------------

PINNED = "bitnamilegacy/minio:2025.7.23@sha256:" + "8" * 64
PINNED_ID = "sha256:" + "m" * 64  # what containers_for() gives the shared minio sidecar


def with_a_pinned_sidecar(env, monkeypatch, container_image, resolvable=True):
    from tests.cli.test_engine_invariants import containers_for

    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    path = _engine.current_install()["compose"]
    open(path, "a").write(f"  minio:\n    image: {PINNED}\n    pull_policy: never\n")
    state_path = env["log"].with_suffix(".state.json")
    docker = json.loads(state_path.read_text())
    if resolvable:
        docker["refs"][PINNED] = PINNED_ID
    state_path.write_text(json.dumps(docker))
    rows = stopped(containers_for(A))
    for row in rows:
        if row[0] == "minio":
            row[1] = container_image
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))


def test_a_stopped_sidecar_made_from_another_image_than_the_pinned_one_is_refused(env, monkeypatch, capsys):
    with_a_pinned_sidecar(env, monkeypatch, "sha256:" + "x" * 64)
    assert main(["engine", "up"]) == 1
    out = capsys.readouterr()
    assert compose_verbs(env) == [] and "minio" in out.err + out.out


def test_a_sidecar_whose_pinned_image_cannot_be_resolved_locally_is_refused(env, monkeypatch, capsys):
    with_a_pinned_sidecar(env, monkeypatch, PINNED_ID, resolvable=False)
    assert main(["engine", "up"]) == 1
    capsys.readouterr()
    assert compose_verbs(env) == []


def test_a_sidecar_made_from_the_pinned_image_is_started(env, monkeypatch, capsys):
    with_a_pinned_sidecar(env, monkeypatch, PINNED_ID)
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    assert [c for c in compose_calls(env) if "start" in c["argv"]]
