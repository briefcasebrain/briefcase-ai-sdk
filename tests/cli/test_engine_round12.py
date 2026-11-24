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
