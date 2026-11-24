"""Round 9: one function decides what a destructive engine command deletes, from state read under the lock.

  D1  no engine command removes anything in a dry run: not a container (`down`, `up --remove-orphans`), not a volume,
      not an image, not a file; and a dry run on a clean home creates nothing (no engine directory, no lock file)
  D2  what is deleted is decided from installed.json read under the lock: an install that lands while the command
      waits is never treated as leftovers
  D3  volumes are removed only on the daemon the install recorded; a client-only record never authorises it; and
      containers and images are touched only through that daemon
  D4  the daemon is recorded before the first operation that can create a volume
  D5  a truncated or partial engine.env is unreadable, never replaced by new passwords
  D6  a failed same-version TLS restart restores the compose file as well as the record
"""
import json
import os

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main
from briefcase.engine import compose

from tests.cli.engine_fakes import build_release, calls
from tests.cli.test_engine_install import _install
from tests.cli.test_engine_invariants import A, B, configure, snapshot_paths, synthetic

REMOVING = (["volume", "rm"], ["image", "rm"], ["rmi"], ["rm"], ["container", "rm"], ["system", "prune"])


def removals(env):
    out = []
    for call in calls(env["log"]):
        argv = call["argv"]
        if argv[:1] == ["compose"] and ("down" in argv or "--remove-orphans" in argv or "rm" in argv):
            out.append(argv)
        elif any(argv[:len(prefix)] == prefix for prefix in REMOVING):
            out.append(argv)
    return out


# --- D1: a dry run removes nothing -----------------------------------------------------------------

DRY = {
    "down": ["engine", "down"],
    "down_volumes": ["engine", "down", "--volumes"],
    "up": ["engine", "up"],
    "up_wait": ["engine", "up", "--wait"],
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


@pytest.mark.parametrize("command", ["uninstall", "purge", "down", "down_volumes", "up"])
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


# --- D2: the plan is made from state read under the lock -------------------------------------------

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


# --- D3: volumes only on the recorded daemon; a client-only record never authorises their deletion ---

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


# --- D4: the daemon is recorded before the first operation that can create a volume ----------------

@pytest.mark.parametrize("fail", [None, "compose up"])
def test_the_daemon_is_recorded_before_compose_up_runs(env, monkeypatch, capsys, fail):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["compose_used"], state["docker_contexts"] = False, []
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "none", "none")
    if fail:
        monkeypatch.setenv("FAKE_DOCKER_FAIL", fail)
    seen = []
    from briefcase.cli import engine_commands

    real = engine_commands._run

    def spy(argv, **kw):
        if argv[:2] == ["docker", "compose"] and "up" in argv:
            recorded = _engine.load_state()
            seen.append((recorded.get("compose_used"), list(recorded.get("docker_contexts") or [])))
        return real(argv, **kw)

    monkeypatch.setattr(engine_commands, "_run", spy)
    main(["engine", "up"])
    capsys.readouterr()
    after = _engine.load_state()
    assert after["compose_used"] and after["docker_contexts"] == ["default unix:///var/run/docker.sock"]
    assert seen and seen[0] == (True, ["default unix:///var/run/docker.sock"]), "recorded only after compose ran"


def test_up_does_not_start_where_the_daemon_cannot_be_identified(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["compose_used"], state["docker_contexts"] = False, []
    _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "fail:context show", "none", "none")
    assert main(["engine", "up"]) != 0
    capsys.readouterr()
    assert [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "up" in c["argv"]] == []


# --- D5: a truncated or partial engine.env is not a reason to generate new passwords ---------------

PARTIAL = {
    "empty": "",
    "one_key": "OCI_JJ_POSTGRES_PASSWORD=abc\n",
    "comments_only": "# Generated by `briefcase-ai engine install`.\n",
}


@pytest.mark.parametrize("shape", list(PARTIAL) + ["missing_with_volumes"])
def test_a_partial_secrets_file_is_never_completed_with_new_passwords(env, capsys, shape):
    assert _install("--code", "bci_good") == 0
    secrets = env["home"] / "engine" / "engine.env"
    if shape == "missing_with_volumes":
        secrets.unlink()
    else:
        secrets.write_text(PARTIAL[shape])
    before = secrets.read_bytes() if secrets.exists() else None
    env["platform"].release = build_release(env["key"], version=B)
    code = main(["engine", "upgrade", "--yes", "--token", "bcl_good"])
    capsys.readouterr()
    assert code != 0
    assert (secrets.read_bytes() if secrets.exists() else None) == before, "passwords were generated"


def test_ensure_secrets_itself_refuses_an_unreadable_file(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    path = tmp_path / "engine.env"
    path.write_text("".join(f"{key}=x\n" for key in compose.VOLUME_BOUND_KEYS))
    path.chmod(0)
    try:
        with pytest.raises(_engine.StateUnreadable):
            compose.ensure_secrets(path)
    finally:
        path.chmod(0o600)


def test_ensure_secrets_itself_refuses_a_partial_file(tmp_path):
    path = tmp_path / "engine.env"
    path.write_text("OCI_JJ_POSTGRES_PASSWORD=abc\n")
    with pytest.raises(_engine.StateUnreadable):
        compose.ensure_secrets(path)
    assert path.read_text() == "OCI_JJ_POSTGRES_PASSWORD=abc\n"
    fresh = tmp_path / "new" / "engine.env"
    assert set(compose.SECRET_KEYS) <= set(compose.ensure_secrets(fresh))  # a missing file starts a first install


# --- D6: a failed same-version TLS restart restores the compose file too ---------------------------

def test_a_failed_tls_restart_restores_the_compose_file(env, monkeypatch, key, capsys):
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    assert _install("--code", "bci_good") == 0
    record = _engine.current_install()
    compose_path = __import__("pathlib").Path(record["compose"])
    before = compose_path.read_bytes()
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps([["oci-jj-server", record["image_ids"]["server"],
                                                              record["compose"]]]))
    env["platform"].release = build_release(key, version=record["version"])
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "compose up")
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    assert _engine.current_install()["tls"] is False
    assert compose_path.read_bytes() == before, "the compose file still describes the TLS transport that never started"


# --- can-follows -----------------------------------------------------------------------------------

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


# --- round 10 -------------------------------------------------------------------------------------------------------

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


def compose_ups(env):
    return [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "up" in c["argv"]]


def test_up_runs_every_step_against_the_daemon_it_recorded_and_changes_nothing_that_exists(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none")
    monkeypatch.setenv("COMPOSE_REMOVE_ORPHANS", "true")  # the caller's setting must not reach our compose call
    seen = []
    from briefcase.cli import engine_commands

    real = subprocess_run = engine_commands.subprocess.run

    def spy(argv, **kw):
        if argv[:2] == ["docker", "compose"] or "--context" in argv:
            inherited = kw["env"] if kw.get("env") is not None else os.environ  # omitted env is inherited
            seen.append((list(argv), inherited.get("COMPOSE_REMOVE_ORPHANS")))
        return real(argv, **kw)

    monkeypatch.setattr(engine_commands.subprocess, "run", spy)
    assert main(["engine", "up"]) == 0
    capsys.readouterr()
    del subprocess_run
    (up,) = compose_ups(env)
    assert up["host"] == "unix:///var/run/docker.sock", "compose ran on the active daemon, not the one identified"
    assert "--no-recreate" in up["argv"] and "--remove-orphans" not in up["argv"]
    assert all(env_value == "false" for _, env_value in seen), "COMPOSE_REMOVE_ORPHANS reached a compose call"
    checks = [c for c in calls(env["log"]) if c["argv"][:1] in (["volume"], ["image"], ["ps"])]
    assert checks and all(c["host"] == "unix:///var/run/docker.sock" for c in checks), "a check ran before the pin"


def test_docker_identity_follows_the_precedence_the_docker_cli_has():
    """Checked against Docker 29.8.2: DOCKER_HOST wins over DOCKER_CONTEXT, and `context show` then says default."""
    from briefcase.engine import install as _install_mod

    asked = []

    def runner(argv, **kw):
        asked.append(argv)
        out = {"show": "default\n", "inspect": "ssh://from-context\n"}[argv[2]]
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


@pytest.mark.parametrize("fail", ["volume inspect", "ps"])
def test_a_tls_change_is_rolled_back_when_the_restart_never_gets_to_start(env, monkeypatch, key, capsys, fail):
    import pathlib
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    assert _install("--code", "bci_good") == 0
    record = _engine.current_install()
    compose_path = pathlib.Path(record["compose"])
    before = compose_path.read_bytes()
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps([["oci-jj-server", record["image_ids"]["server"],
                                                              record["compose"]]]))
    env["platform"].release = build_release(key, version=record["version"])
    monkeypatch.setenv("FAKE_DOCKER_FAIL", fail)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert _engine.current_install()["tls"] is False and compose_path.read_bytes() == before, \
        "the new transport was left in place although the running server never switched"


def test_the_install_dry_run_hint_does_not_print_the_install_code(env, capsys):
    main(["engine", "install", "--code", "bci_SENTINEL1234567", "--dry-run"])
    captured = capsys.readouterr()
    assert "bci_SENTINEL1234567" not in captured.out + captured.err


# --- round 11 --------------------------------------------------------------------------------

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


def tls_upgrade(env, monkeypatch, key):
    import pathlib
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    assert _install("--code", "bci_good") == 0
    record = _engine.current_install()
    path = pathlib.Path(record["compose"])
    before = path.read_bytes()
    rows = [["oci-jj-server", record["image_ids"]["server"], record["compose"]]]
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    env["platform"].release = build_release(key, version=record["version"])
    return path, before


def test_a_state_error_raised_during_the_restart_still_rolls_the_transport_back(env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    path, before = tls_upgrade(env, monkeypatch, key)

    def boom(*args, **kwargs):
        raise _engine.StateUnreadable("simulated")

    monkeypatch.setattr(_install_mod, "note_daemon", boom)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    capsys.readouterr()
    assert _engine.current_install()["tls"] is False and path.read_bytes() == before


def test_the_rollback_does_not_rewrite_a_record_it_cannot_read(env, monkeypatch, key, capsys):
    from briefcase.engine import install as _install_mod

    tls_upgrade(env, monkeypatch, key)

    def corrupt_then_fail(*args, **kwargs):
        _engine.installed_path().write_text("{not json")
        raise OSError("simulated")

    monkeypatch.setattr(_install_mod, "note_daemon", corrupt_then_fail)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    assert _engine.installed_path().read_text() == "{not json", "rollback replaced an unreadable record from nothing"
    assert "could not put the previous transport back" in capsys.readouterr().err
