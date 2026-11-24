"""The engine lifecycle's invariants, checked over generated fake-docker states.

Each combination builds installed releases (one, or two side by side), a docker that is fine, missing or failing
one subcommand, engine volumes (none, ours, or someone else's) and a running server (none, release A's, release
B's), and runs one command: up, down, down --volumes, upgrade, uninstall, uninstall --purge, install. Then:

  L1  secrets and certificates are deleted only when every engine volume is positively confirmed absent (docker
      missing or failing is "unknown" and blocks)
  L2  containers are started only when ownership of every volume they would mount is positively established
  L3  after a command returns 0 the recorded current release is the one whose server runs; a docker error is a
      failure, never "nothing to do"; a failed `compose down` stops an uninstall before anything is deleted
  L4  "release X is running" is decided by the project's server container (its image id), not by a label that a
      shared sidecar also carries
  L5  an upgrade carries the installed options forward (TLS stays TLS unless the user says otherwise)
"""
import hashlib
import json
import os
import random

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main
from briefcase.engine import compose

from tests.cli.engine_fakes import VERSION, build_index, build_release, calls, docker_state
from tests.cli.test_engine_install import LABEL, _install

A, B, C = VERSION, "0.5.0", "0.6.0"
VOLUME_NAMES = [f"{compose.PROJECT}_{k}" for k in compose.VOLUME_KEYS]


def _id(version, name):
    return "sha256:" + hashlib.sha256((version + name).encode()).hexdigest()


def synthetic(env, installs, current, shared_server=False):
    """Write installed.json and the files of ``installs`` as an install would have, without running one. With
    ``shared_server`` every release's server image is the same one (they differ in the other services)."""
    home = env["home"] / "engine"
    home.mkdir(parents=True, exist_ok=True)
    (home / "engine.env").write_text("".join(f"{key}=x\n" for key in compose.VOLUME_BOUND_KEYS))
    (home / "engine.env").chmod(0o600)
    (home / "tls").mkdir(exist_ok=True)
    (home / "tls" / "ca.pem").write_text("x")
    state = {"schema": 2, "current": current, "installs": {}, "compose_used": True,
             "docker_contexts": ["default unix:///var/run/docker.sock"]}
    refs = {}
    for version in installs:
        directory = home / version
        directory.mkdir(parents=True, exist_ok=True)
        images = {n: f"localhost/briefcase-engine/{n}:{version}" for n in ("server", "gateway", "client")}
        ids = {n: _id(version, n) for n in images}
        if shared_server:
            ids["server"] = _id(A, "server")
        refs.update({images[n]: ids[n] for n in images})
        (directory / "docker-compose.yml").write_text(
            f"services:\n  oci-jj-server:\n    image: {images['server']}\n    pull_policy: never\n"
            f"  gateway:\n    image: {images['gateway']}\n    pull_policy: never\n")
        (directory / "engine-manifest.json").write_text("{}")
        state["installs"][version] = {
            "version": version, "compose": str(directory / "docker-compose.yml"), "env_file": str(home / "engine.env"),
            "manifest": str(directory / "engine-manifest.json"), "images": images, "image_ids": ids,
            "image_digests": {}, "grpc_addr": "http://127.0.0.1:50051", "gateway": "http://127.0.0.1:8080",
            "client": {"mode": "binary"}, "binary": None, "tls": False, "source": "bundle", "key_id": "k",
        }
    _engine.installed_path().write_text(json.dumps(state))
    docker_state_file = env["log"].with_suffix(".state.json")
    docker_state_file.write_text(json.dumps({"refs": refs, "ids": list(refs.values())}))


def remove_docker(env, monkeypatch):
    """No docker on PATH at all: the fake is removed and PATH is cut down to its directory, so a real docker in
    /usr/bin (as on CI runners) cannot answer."""
    (env["tmp"] / "bin" / "docker").unlink()
    monkeypatch.setenv("PATH", str(env["tmp"] / "bin"))


def containers_for(version, shared_server=False):
    path = str(_engine.engine_dir() / version / "docker-compose.yml")
    server = _id(A if shared_server else version, "server")
    return [["oci-jj-server", server, path], ["gateway", _id(version, "gateway"), path],
            ["minio", "sha256:" + "m" * 64, path]]  # a sidecar image every release shares


def configure(env, monkeypatch, docker, volumes, running, context="same", shared_server=False):
    if docker == "absent":
        remove_docker(env, monkeypatch)
    elif docker.startswith("fail:"):
        monkeypatch.setenv("FAKE_DOCKER_FAIL", docker[5:])
    if context == "other":
        monkeypatch.setenv("FAKE_DOCKER_CONTEXT", "remote")
        monkeypatch.setenv("FAKE_DOCKER_HOST", "ssh://elsewhere")
    names = {"none": {}, "ours": {n: LABEL for n in VOLUME_NAMES}, "foreign": {VOLUME_NAMES[0]: {}}}[volumes]
    monkeypatch.setenv("FAKE_DOCKER_VOLUMES", json.dumps(names))
    running_rows = {
        "none": [], "A": containers_for(A, shared_server), "B": containers_for(B, shared_server),
        # the new server with the old gateway: a mix no install produced
        "mixed": [containers_for(B)[0], containers_for(A)[1], containers_for(A)[2]],
        "unrecognised": [["oci-jj-server", "sha256:" + "f" * 64, ""]],
    }[running]
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(running_rows))
    monkeypatch.setenv("FAKE_DOCKER_SERVERS", json.dumps([r[1] for r in running_rows if r[0] == "oci-jj-server"]))


DOCKERS = ["ok", "absent", "fail:volume inspect", "fail:ps", "fail:inspect", "fail:compose down",
           "fail:compose up", "fail:volume rm"]
VOLUMES = ["none", "ours", "foreign"]
RUNNING = ["none", "A", "B", "mixed", "unrecognised"]
INSTALLS = ["one", "two"]
IMAGES = ["distinct", "shared_server"]
CONTEXTS = ["same", "other"]
COMMANDS = ["up", "down", "down_volumes", "uninstall", "purge", "upgrade", "install"]

ALL = [(d, v, r, i, c, im, cx) for d in DOCKERS for v in VOLUMES for r in RUNNING for i in INSTALLS
       for c in COMMANDS for im in IMAGES for cx in CONTEXTS
       if not (r == "B" and i == "one") and not (im == "shared_server" and i == "one")]
# A seeded, stratified sample of the product (about 4000 combinations): every command meets every docker state,
# every running state, every image sharing and both contexts, several times each.
_rng = random.Random(7)
COMBOS = []
for _idx, _values in ((0, DOCKERS), (2, RUNNING), (5, IMAGES), (6, CONTEXTS)):
    for _v in _values:
        for _c in COMMANDS:
            pool = [x for x in ALL if x[_idx] == _v and x[4] == _c]
            COMBOS += _rng.sample(pool, min(len(pool), 2 if _idx in (0, 2) else 3))
COMBOS += [x for x in ALL if x[4] == "upgrade" and x[2] == "A" and x[1] == "ours" and x[3] == "one"
           and x[5] == "distinct" and x[6] == "same"]
COMBOS = sorted(set(COMBOS))


def argv_for(command):
    return {"up": ["engine", "up"], "down": ["engine", "down", "--yes"],
            "down_volumes": ["engine", "down", "--volumes", "--yes"],
            "uninstall": ["engine", "uninstall", "--yes"], "purge": ["engine", "uninstall", "--purge", "--yes"],
            "upgrade": ["engine", "upgrade", "--yes", "--token", "bcl_good"],
            "install": ["engine", "install", "--yes", "--token", "bcl_good", "--version", C]}[command]


def fake_state(env):
    state = docker_state(env["log"])
    return state


@pytest.mark.parametrize("docker, volumes, running, installs, command, images, context", COMBOS,
                         ids=["-".join(c).replace(" ", "_").replace(":", "_") for c in COMBOS])
def test_lifecycle_invariants(env, monkeypatch, capsys, key, docker, volumes, running, installs, command, images,
                              context):
    shared = images == "shared_server"
    synthetic(env, [A] if installs == "one" else [A, B], A, shared_server=shared)
    if command in ("upgrade", "install"):
        env["platform"].release = build_release(key, version=C)
        env["platform"].index = build_index(key, [C, B, A])
    configure(env, monkeypatch, docker, volumes, running, context, shared)
    secrets = env["home"] / "engine" / "engine.env"
    tls = env["home"] / "engine" / "tls"
    code = main(argv_for(command))
    capsys.readouterr()
    state = _engine.load_state()
    log = [c["argv"] for c in calls(env["log"])]
    fake = fake_state(env)
    left = fake["volumes"] if "volumes" in fake else json.loads(os.environ["FAKE_DOCKER_VOLUMES"])
    ours_left = [n for n, labels in left.items() if labels.get("ai.briefcase.engine") == "1"]

    # L1: the secrets and certificates went only because every volume is positively absent, on the daemon used
    if not secrets.exists() or not tls.exists():
        assert docker not in ("absent", "fail:volume inspect"), "secrets deleted without docker answering"
        assert not ours_left, f"secrets deleted while {ours_left} remain"
        assert context == "same", "secrets deleted on a daemon that is not the one the volumes live on"
        assert secrets.exists() == tls.exists(), "secrets and certificates must go together"
    if command == "purge" and code != 0:
        assert secrets.exists() and tls.exists() and (state.get("installs") or state.get("compose_used")), \
            "a failed purge must keep the secrets, the certificates and the record"
    if command != "purge":
        assert secrets.exists() and tls.exists()
    if command in ("uninstall", "down", "down_volumes") and ours_left or volumes == "ours" and command == "uninstall":
        assert state.get("compose_used"), "volumes outlive an ordinary uninstall, so the record of them must too"

    # L2: nothing was started without ownership of every volume it would mount
    ups = [a for a in log if a[:1] == ["compose"] and "up" in a]
    if ups:
        assert docker not in ("absent", "fail:volume inspect") and volumes != "foreign", \
            "containers were started without establishing who owns the volumes"

    # L3/L4: current is what runs; anything docker cannot establish is a failure that changes nothing
    containers = fake.get("containers") if "containers" in fake else json.loads(os.environ["FAKE_DOCKER_CONTAINERS"])
    current = (state.get("installs") or {}).get(state.get("current") or "")
    servers = [c[1] for c in containers if c[0] == "oci-jj-server"]
    if code == 0 and servers:
        assert current is not None and current["image_ids"]["server"] in servers, \
            "a command succeeded while the recorded current release is not the one that runs"
    if command in ("upgrade", "install") and running in ("mixed", "unrecognised") and docker == "ok":
        assert code != 0 and state["current"] == A, "an engine this install cannot recognise must not be replaced"
    if command in ("uninstall", "purge") and running in ("mixed", "unrecognised") and docker == "ok":
        assert code != 0 and A in state["installs"], "an engine that cannot be recognised must not be taken down"
    if command == "upgrade" and running == "A" and docker.startswith("fail:") and docker[5:] in (
            "ps", "inspect", "compose up", "volume inspect"):
        assert code != 0 and state["current"] == A, "an error during the restart must fail and keep A current"
    if command in ("uninstall", "purge") and running == "A" and docker == "fail:compose down":
        assert code != 0 and A in state["installs"] and (env["home"] / "engine" / A).exists(), \
            "a failed compose down must stop the uninstall before anything is deleted"
    if command in ("uninstall", "purge") and running == "A" and docker in ("fail:ps", "fail:inspect"):
        assert code != 0 and A in state["installs"], "not knowing what runs must not delete it"
    if command in ("uninstall", "purge") and installs == "two" and running == "B" and docker == "ok" \
            and not shared and code == 0:
        assert not [a for a in log if a[:1] == ["compose"] and "down" in a], "took down the release that runs"
        assert state["current"] == B, "current must follow what is running"
    if command == "upgrade" and code == 0:
        assert state["current"] == C


def test_an_upgrade_of_a_tls_install_stays_tls_unless_told_otherwise(env, capsys, key):
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    assert _install("--code", "bci_good", "--tls") == 0
    assert _engine.current_install()["tls"] is True
    env["platform"].release = build_release(key, version=B)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good"]) == 0
    assert _engine.current_install()["version"] == B and _engine.current_install()["tls"] is True
    env["platform"].release = build_release(key, version=C)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--no-tls"]) == 0
    assert _engine.current_install()["version"] == C and _engine.current_install()["tls"] is False


def test_purge_is_refused_without_docker_even_for_a_client_only_install_unless_it_is_provable(
        env, monkeypatch, capsys):
    synthetic(env, [A], A)
    state = json.loads(_engine.installed_path().read_text())
    state["installs"][A].update(compose=None, images={}, image_ids={})
    state["compose_used"] = True  # an earlier release may have created volumes
    _engine.installed_path().write_text(json.dumps(state))
    remove_docker(env, monkeypatch)
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 1
    assert (env["home"] / "engine" / "engine.env").exists()
    assert main(["engine", "uninstall", "--purge", "--force-without-docker", "--yes"]) == 1  # not provable
    assert (env["home"] / "engine" / "engine.env").exists()
    state["compose_used"] = False  # the record proves no volume was ever created
    _engine.installed_path().write_text(json.dumps(state))
    assert main(["engine", "uninstall", "--purge", "--force-without-docker", "--yes"]) == 0
    assert not (env["home"] / "engine" / "engine.env").exists()


def test_a_retained_sidecar_of_the_old_release_does_not_make_it_the_running_one(env, monkeypatch, capsys):
    synthetic(env, [A, B], A)
    configure(env, monkeypatch, "ok", "none", "B")
    # B's server runs; every other container of the project may still carry A's labels.
    assert main(["engine", "uninstall", "--yes"]) == 0
    log = [c["argv"] for c in calls(env["log"])]
    assert not [a for a in log if a[:1] == ["compose"] and "down" in a]
    assert set(_engine.load_state()["installs"]) == {B} and _engine.load_state()["current"] == B


def test_two_upgrades_at_once_leave_the_running_release_current(env, monkeypatch, key, capsys):
    import multiprocessing
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})  # macOS's proxy lookup is not fork-safe

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("needs fork")
    synthetic(env, [A], A)
    env["platform"].release = build_release(key, version=B)
    configure(env, monkeypatch, "ok", "none", "A")
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()

    def upgrade():
        results.put(main(["engine", "upgrade", "--yes", "--token", "bcl_good"]))

    procs = [ctx.Process(target=upgrade) for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(120)
        assert proc.exitcode == 0
    assert sorted(results.get(timeout=5) for _ in procs) == [0, 0]
    state = _engine.load_state()
    assert state["current"] == B
    assert state["installs"][B]["image_ids"]["server"] in docker_state(env["log"])["servers"]


def test_a_client_only_upgrade_works_without_docker(env, monkeypatch, key, capsys):
    assert _install("--code", "bci_good", "--no-images") == 0
    assert _engine.current_install()["compose"] is None
    env["platform"].release = build_release(key, version=B)
    remove_docker(env, monkeypatch)
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--no-images"]) == 0
    assert _engine.current_install()["version"] == B


def test_a_tls_change_to_the_running_version_restarts_it(env, monkeypatch, key, capsys):
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    assert _install("--code", "bci_good") == 0
    record = _engine.current_install()
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps([["oci-jj-server", record["image_ids"]["server"],
                                                              record["compose"]]]))
    env["platform"].release = build_release(key, version=A)
    env["log"].unlink()
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) == 0
    ups = [c["argv"] for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "up" in c["argv"]]
    assert ups and _engine.current_install()["tls"] is True, "the running transport must follow the record"


def test_an_ordinary_uninstall_keeps_a_tombstone_so_the_later_force_cannot_prove_there_were_no_volumes(
        env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "ours", "none")
    assert main(["engine", "uninstall", "--yes"]) == 0
    state = _engine.load_state()
    assert state["installs"] == {} and state["compose_used"] is True
    assert state["docker_contexts"] == ["default unix:///var/run/docker.sock"]
    assert (env["home"] / "engine" / "engine.env").exists()  # the passwords those volumes were created with
    remove_docker(env, monkeypatch)
    assert main(["engine", "uninstall", "--purge", "--force-without-docker", "--yes"]) == 1  # not provable
    assert (env["home"] / "engine" / "engine.env").exists()
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 1  # docker still absent
    assert (env["home"] / "engine" / "engine.env").exists()
    monkeypatch.setenv("PATH", str(env["tmp"] / "bin") + os.pathsep + "/usr/bin:/bin")
    from tests.cli.engine_fakes import install_fake_docker
    install_fake_docker(env["tmp"] / "bin", env["log"], monkeypatch)
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0  # the daemon is back: volumes removed, then secrets
    assert not (env["home"] / "engine" / "engine.env").exists() and not _engine.installed_path().exists()


def test_a_purge_on_another_docker_daemon_is_refused(env, monkeypatch, capsys):
    synthetic(env, [A], A)
    configure(env, monkeypatch, "ok", "none", "none", context="other")
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 1
    assert (env["home"] / "engine" / "engine.env").exists()
    assert "not the one this install used" in capsys.readouterr().err


def test_up_cannot_start_the_old_release_while_an_upgrade_is_moving_current(env, monkeypatch, key, capsys):
    import multiprocessing
    import time
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})  # macOS's proxy lookup is not fork-safe

    from briefcase.cli import engine_commands

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("needs fork")
    synthetic(env, [A], A)
    env["platform"].release = build_release(key, version=B)
    configure(env, monkeypatch, "ok", "none", "A")
    real = engine_commands.guard_start

    def slow(record):  # the upgrade holds the lock here for a while
        time.sleep(1.0)
        return real(record)

    monkeypatch.setattr(engine_commands, "guard_start", slow)
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()

    def upgrade():
        results.put(main(["engine", "upgrade", "--yes", "--token", "bcl_good"]))

    def up():
        time.sleep(0.4)
        results.put(main(["engine", "up"]))

    procs = [ctx.Process(target=upgrade), ctx.Process(target=up)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(120)
        assert proc.exitcode == 0
    assert sorted(results.get(timeout=5) for _ in procs) == [0, 0]
    state = _engine.load_state()
    assert state["current"] == B
    servers = [c[1] for c in docker_state(env["log"])["containers"] if c[0] == "oci-jj-server"]
    assert servers == [state["installs"][B]["image_ids"]["server"]], "what runs is not the current release"


# --- authorisation: nothing is deleted unless it was confirmed, and never in a dry run --------------------------

DESTRUCTIVE = {
    "down": ["engine", "down"],
    "uninstall": ["engine", "uninstall"],
    "purge": ["engine", "uninstall", "--purge"],
    "purge_force_without_docker": ["engine", "uninstall", "--purge", "--force-without-docker"],
    "down_volumes": ["engine", "down", "--volumes"],
}
AUTH = ["dry_run", "no_yes_non_tty", "yes"]
AUTH_STATES = ["installed", "tombstone", "other_daemon", "volumes_ours"]
AUTH_COMBOS = [(c, a, st) for c in DESTRUCTIVE for a in AUTH for st in AUTH_STATES
               if not (c in ("down", "down_volumes") and st == "tombstone")]
DELETING = (["volume", "rm"], ["image", "rm"])


def snapshot_paths(env):
    root = env["home"] / "engine"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != ".install.lock"}


@pytest.mark.parametrize("command, auth, state_name", AUTH_COMBOS,
                         ids=[f"{c}-{a}-{s}" for c, a, s in AUTH_COMBOS])
def test_nothing_is_deleted_unless_confirmed_and_never_in_a_dry_run(
        env, monkeypatch, capsys, command, auth, state_name):
    synthetic(env, [A], A)
    if state_name == "tombstone":
        state = json.loads(_engine.installed_path().read_text())
        state["installs"] = {}
        state["current"] = None
        _engine.installed_path().write_text(json.dumps(state))
    configure(env, monkeypatch, "ok", "ours", "A" if state_name != "tombstone" else "none",
              context="other" if state_name == "other_daemon" else "same")
    before = snapshot_paths(env)
    argv = list(DESTRUCTIVE[command]) + (["--dry-run"] if auth == "dry_run" else []) + (
        ["--yes"] if auth == "yes" else [])
    code = main(argv)
    capsys.readouterr()
    log = [c["argv"] for c in calls(env["log"])]
    deleted = [a for a in log if a[:2] in DELETING]
    unconfirmed = auth != "yes"
    if unconfirmed:
        assert not deleted, f"deleted without confirmation: {deleted}"
        assert snapshot_paths(env) == before
        assert (env["home"] / "engine" / A).exists() or state_name == "tombstone"
        assert not [a for a in log if a[:1] == ["compose"] and "down" in a], "stopped the engine unconfirmed"
        if auth == "dry_run":
            assert code in (0, 1, 3)
        else:
            assert code in (1, 2, 3), "an unconfirmed destructive command must not succeed"
    if state_name == "other_daemon" and command != "uninstall":
        assert not deleted, "volumes were removed on a daemon that is not the one the install used"
        assert (env["home"] / "engine" / "engine.env").exists()


def test_a_confirmed_token_cannot_be_made_outside_the_confirmation_step():
    from briefcase.cli import engine_commands

    with pytest.raises(TypeError):
        engine_commands.Confirmed("anything")
    with pytest.raises(TypeError):
        engine_commands.remove_engine_volumes(None)  # type: ignore[arg-type]


def test_an_unreadable_secrets_file_is_never_replaced_by_new_passwords(env, capsys):
    assert _install("--code", "bci_good") == 0
    secrets = env["home"] / "engine" / "engine.env"
    before = secrets.read_bytes()
    secrets.chmod(0)
    try:
        env["platform"].release = build_release(env["key"], version=B)
        code = main(["engine", "upgrade", "--yes", "--token", "bcl_good"])
    finally:
        secrets.chmod(0o600)
    assert code != 0 and secrets.read_bytes() == before, "the passwords the volumes were created with are gone"


def test_an_unreadable_or_corrupt_installed_json_is_not_empty_state(env, capsys):
    assert _install("--code", "bci_good") == 0
    path = _engine.installed_path()
    good = path.read_text()
    for bad in ("{not json", "[]"):
        path.write_text(bad)
        assert _install("--code", "bci_good2", "--no-images") != 0 and path.read_text() == bad
        assert main(["engine", "uninstall", "--purge", "--yes"]) != 0 and path.read_text() == bad
    path.write_text(good)
    path.chmod(0)
    try:
        assert main(["engine", "uninstall", "--purge", "--yes"]) != 0
    finally:
        path.chmod(0o600)
    assert (env["home"] / "engine" / "engine.env").exists()


def test_a_matching_server_with_an_unknown_gateway_image_is_not_the_release(env, monkeypatch, capsys):
    synthetic(env, [A, B], A)
    rows = containers_for(A)
    rows[1] = ["gateway", "sha256:" + "9" * 64, rows[1][2]]  # an image no install recorded, in a recorded service
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps(rows))
    assert main(["engine", "uninstall", "--yes"]) != 0
    assert not [c for c in calls(env["log"]) if c["argv"][:1] == ["compose"] and "down" in c["argv"]]
    assert A in _engine.load_state()["installs"]


def test_a_client_only_install_of_a_composed_version_is_refused(env, capsys):
    assert _install("--code", "bci_good") == 0
    before = _engine.installed_path().read_text()
    assert _install("--code", "bci_good2", "--no-images") != 0
    assert _engine.installed_path().read_text() == before


def test_a_failed_tls_restart_keeps_the_old_record_so_a_retry_restarts(env, monkeypatch, key, capsys):
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    assert _install("--code", "bci_good") == 0
    record = _engine.current_install()
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERS", json.dumps([["oci-jj-server", record["image_ids"]["server"],
                                                              record["compose"]]]))
    env["platform"].release = build_release(key, version=A)
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "compose up")
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good", "--tls"]) != 0
    assert _engine.current_install()["tls"] is False, "the running transport is still plaintext"
