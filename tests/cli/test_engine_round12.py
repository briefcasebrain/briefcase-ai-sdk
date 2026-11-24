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
