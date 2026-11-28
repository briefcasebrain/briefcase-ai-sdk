"""Regression tests for legacy Docker identity, ownership and safe cleanup."""
import json

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main

from tests.cli.engine_fakes import calls
from tests.cli.test_engine_invariants import A, B, configure, snapshot_paths, synthetic

REMOVING = (["volume", "rm"], ["volume", "prune"], ["image", "rm"], ["rmi"], ["rm"], ["container", "rm"],
            ["system", "prune"])


def removals(env):
    out = []
    for call in calls(env["log"]):
        argv = call["argv"]
        if argv[:1] == ["compose"] and ("down" in argv or "--remove-orphans" in argv or "rm" in argv):
            out.append(argv)
        elif any(argv[:len(prefix)] == prefix for prefix in REMOVING):
            out.append(argv)
    return out


DRY = {
    "down": ["engine", "down"],
    "down_volumes": ["engine", "down", "--volumes"],
    "uninstall": ["engine", "uninstall"],
    "purge": ["engine", "uninstall", "--purge"],
}
DRY_FORMS = ["engine_flag", "global_flag", "global_flag_yes"]


def dry_argv(command, form):
    base = DRY[command]
    if form == "engine_flag":
        return base + ["--dry-run"] + (["--yes"] if command.startswith(("down", "uninstall")) else [])
    return ["--dry-run"] + base + (["--yes"] if form == "global_flag_yes" and command.startswith(("down", "uninstall"))
                                   else [])


@pytest.mark.parametrize("running", ["none", "A"])
@pytest.mark.parametrize("form", DRY_FORMS)
@pytest.mark.parametrize("command", list(DRY))
def test_a_dry_run_removes_nothing(env, monkeypatch, capsys, command, form, running):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", running)
    before = snapshot_paths(env)
    main(dry_argv(command, form))
    capsys.readouterr()
    assert removals(env) == [], f"a dry run removed something: {removals(env)}"
    assert snapshot_paths(env) == before
    assert [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "up" in c["argv"]] == []


@pytest.mark.parametrize("command", ["uninstall", "purge", "down", "down_volumes"])
def test_a_dry_run_on_a_clean_home_creates_nothing(env, capsys, command):
    root = env["home"] / "engine"
    assert not root.exists()
    main(dry_argv(command, "engine_flag"))
    capsys.readouterr()
    assert not root.exists(), "a dry run created the engine directory or its lock file"


def test_a_multi_release_purge_preview_does_not_promise_to_delete_the_shared_secrets(env, monkeypatch, capsys):
    synthetic(env, [A, B], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    main(["engine", "uninstall", "--purge", "--dry-run"])
    out = capsys.readouterr().out
    assert "engine.env" not in out, "the preview lists the shared secrets, which execution keeps"
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    assert (env["home"] / "engine" / "engine.env").exists()


def test_an_install_that_lands_while_a_purge_waits_is_not_leftovers(env, monkeypatch, capsys):
    from briefcase.engine import install as _install_mod

    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    tombstone = dict(state, installs={}, current=None)
    _engine.installed_path().write_text(json.dumps(tombstone))  # what the command sees before it takes the lock
    configure(env, monkeypatch, "ok", "ours", "none")
    real = _install_mod.engine_lock
    landed = []

    class Racing(object):
        def __init__(self, *a, **k):
            self.inner = real(*a, **k)

        def __enter__(self):
            self.inner.__enter__()
            if not landed:  # an install finishes just as the lock is acquired
                landed.append(True)
                _engine.installed_path().write_text(json.dumps(state))
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    monkeypatch.setattr(_install_mod, "engine_lock", Racing)
    main(["engine", "uninstall", "--purge", "--yes"])
    capsys.readouterr()
    assert landed
    after = _engine.load_state()
    secrets = env["home"] / "engine" / "engine.env"
    # The install that landed is not what the person was shown: it is kept whole, with everything it needs.
    assert A in after["installs"], "a purge of leftovers removed an install that landed while it waited"
    assert secrets.exists() and (env["home"] / "engine" / A / "docker-compose.yml").exists()
    assert removals(env) == []


def test_what_else_is_installed_is_read_under_the_lock(env, monkeypatch, capsys):
    from briefcase.engine import install as _install_mod

    synthetic(env, [A, B], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    real = _install_mod.engine_lock

    class Switching(object):
        def __init__(self, *a, **k):
            self.inner = real(*a, **k)

        def __enter__(self):
            self.inner.__enter__()
            state = _engine.load_state()
            state["installs"].pop(B)  # the other release was removed while this command waited
            _engine.installed_path().write_text(json.dumps(state))
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    monkeypatch.setattr(_install_mod, "engine_lock", Switching)
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    capsys.readouterr()
    # B was gone by the time the lock was held, so A was the last release: its volumes and secrets go with it.
    assert _engine.load_state().get("installs") in (None, {})
    assert not (env["home"] / "engine" / "engine.env").exists()
    assert [a for a in removals(env) if a[:2] == ["volume", "rm"]]


def client_only(env, contexts=()):
    state = json.loads(_engine.installed_path().read_text())
    state["compose_used"] = False
    state["docker_contexts"] = list(contexts)
    for record in state["installs"].values():
        record["compose"] = None
    _engine.installed_path().write_text(json.dumps(state))


@pytest.mark.parametrize("flags", [[], ["--force-without-docker"]])
@pytest.mark.parametrize("command", [["engine", "uninstall", "--purge", "--yes"]])
def test_a_client_only_record_never_authorises_volume_deletion(env, monkeypatch, capsys, command, flags):
    synthetic(env, [A], A)
    client_only(env)
    configure(env, monkeypatch, "ok", "ours", "none")  # a reachable daemon with volumes that carry our label
    main(command + flags)
    capsys.readouterr()
    assert [a for a in removals(env) if a[:2] == ["volume", "rm"]] == [], "volumes removed for a client-only record"


@pytest.mark.parametrize("command", [["engine", "uninstall", "--yes"], ["engine", "uninstall", "--purge", "--yes"]])
@pytest.mark.parametrize("shape", ["one_release", "two_releases"])
def test_nothing_is_stopped_or_removed_on_a_daemon_the_install_did_not_use(env, monkeypatch, capsys, command, shape):
    synthetic(env, [A] if shape == "one_release" else [A, B], A)
    configure(env, monkeypatch, "ok", "ours", "A", context="other")
    main(command)
    capsys.readouterr()
    assert removals(env) == [], f"destructive calls on another daemon: {removals(env)}"
    assert A in _engine.load_state()["installs"]


def test_destructive_calls_are_bound_to_the_verified_daemon(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = ["remote ssh://elsewhere"]  # the daemon the install used is not the default one
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "A", context="other")  # and the active context is that same daemon
    main(["engine", "uninstall", "--purge", "--yes"])
    capsys.readouterr()
    destructive = [c for c in calls(env["log"]) if c["argv"][:2] in (["volume", "rm"], ["image", "rm"])
                   or (c["argv"][:1] == ["compose"] and "down" in c["argv"])]
    assert destructive, "the purge should have run"
    assert all(c["host"] == "ssh://elsewhere" and c["context"] is None for c in destructive), \
        "a destructive call was not pinned to the verified daemon's endpoint"


def test_no_internal_delete_path_runs_without_a_confirmed_token(env, monkeypatch):
    from briefcase.cli import engine_commands

    synthetic(env, [A], A)
    record = _engine.current_install()
    for call in (lambda: engine_commands._remove_install(A, False, False, None),
                 lambda: engine_commands._remove_install(A, True, False, None),
                 lambda: engine_commands._remove_install(None, True, False, None),
                 lambda: engine_commands.teardown(record, True, None),
                 lambda: engine_commands.teardown(record, False, None),
                 lambda: engine_commands.remove_engine_volumes(None)):
        with pytest.raises(TypeError):
            call()
    assert A in _engine.load_state()["installs"] and removals(env) == []


def old_record(env, contexts):
    """A schema-2 record written before ``compose_used`` existed (and maybe before the daemon was recorded)."""
    state = json.loads(_engine.installed_path().read_text())
    state.pop("compose_used", None)
    state["docker_contexts"] = list(contexts)
    _engine.installed_path().write_text(json.dumps(state))


def test_a_purge_of_a_record_with_no_stack_history_does_not_delete_the_secrets_unverified(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    old_record(env, [])
    configure(env, monkeypatch, "ok", "ours", "none")
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 1
    capsys.readouterr()
    assert (env["home"] / "engine" / "engine.env").exists() and (env["home"] / "engine" / "tls").exists()
    assert removals(env) == [] and A in _engine.load_state()["installs"]


def test_a_purge_of_an_old_record_that_names_its_daemon_still_removes_the_volumes_first(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    old_record(env, ["default unix:///var/run/docker.sock"])
    configure(env, monkeypatch, "ok", "ours", "none")
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    capsys.readouterr()
    assert [a for a in removals(env) if a[:2] == ["volume", "rm"]]
    assert not (env["home"] / "engine" / "engine.env").exists()


def test_docker_identity_follows_the_precedence_the_docker_cli_has():
    """Checked against Docker 29.8.2: DOCKER_HOST wins over DOCKER_CONTEXT, and `context show` then says default."""
    from briefcase.engine import install as _install_mod

    asked = []

    def runner(argv, **kw):
        asked.append(argv)
        out = {"show": "default\n", "inspect": "ssh://from-context\n"}.get(argv[2], "")
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    both = {"DOCKER_CONTEXT": "work", "DOCKER_HOST": "tcp://from-host:2375"}
    assert _install_mod.docker_identity(runner, both) == "default tcp://from-host:2375"
    asked.clear()
    assert _install_mod.docker_identity(runner, {"DOCKER_CONTEXT": "work"}) == "work ssh://from-context"
    assert any(a[:5] == ["docker", "context", "inspect", "work", "--format"] for a in asked)


def test_a_context_pin_is_its_verified_endpoint(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = ["tlsctx tcp://remote:2376"]
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "A", context="other")
    monkeypatch.setenv("FAKE_DOCKER_CONTEXT", "tlsctx")
    monkeypatch.setenv("FAKE_DOCKER_HOST", "tcp://remote:2376")
    main(["engine", "uninstall", "--purge", "--yes"])
    capsys.readouterr()
    pinned = [c for c in calls(env["log"]) if c["argv"][:2] in (["volume", "rm"], ["image", "rm"])]
    assert pinned and all(c["host"] == "tcp://remote:2376" and c["context"] is None for c in pinned)


def test_plain_down_refuses_a_daemon_the_record_does_not_name(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["docker_contexts"] = []
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "A")
    assert main(["engine", "down", "--yes"]) == 1
    capsys.readouterr()
    assert removals(env) == []


@pytest.mark.parametrize("command", [["engine", "down"], ["engine", "down", "--volumes"]])
def test_down_needs_a_confirmation_like_every_other_removal(env, monkeypatch, capsys, command):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "A")
    assert main(command) in (1, 2)
    capsys.readouterr()
    assert removals(env) == [] and "compose down" not in str(calls(env["log"]))
    assert main(command + ["--yes"]) == 0
    assert [a for a in removals(env) if a[:1] == ["compose"]]


def test_an_ordinary_uninstall_of_a_record_with_no_history_keeps_the_history_for_the_next_purge(
        env, monkeypatch, capsys):
    synthetic(env, [A, B], A)
    state = json.loads(_engine.installed_path().read_text())
    state.pop("compose_used")
    state["installs"][B]["compose"] = None  # B is client-only; A, the current release, ran a stack
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "none")
    assert main(["engine", "uninstall", "--yes"]) == 0
    capsys.readouterr()
    after = json.loads(_engine.installed_path().read_text())
    assert after["compose_used"] is True and list(after["installs"]) == [B]
    # The volumes may exist, so the purge removes them (on the recorded daemon) before it deletes the secrets.
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    capsys.readouterr()
    assert [a for a in removals(env) if a[:2] == ["volume", "rm"]]
    assert not (env["home"] / "engine" / "engine.env").exists()
