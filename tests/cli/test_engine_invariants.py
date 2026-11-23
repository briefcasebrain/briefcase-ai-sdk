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


def synthetic(env, installs, current):
    """Write installed.json and the files of ``installs`` as an install would have, without running one."""
    home = env["home"] / "engine"
    home.mkdir(parents=True, exist_ok=True)
    (home / "engine.env").write_text("OCI_JJ_POSTGRES_PASSWORD=x\n")
    (home / "engine.env").chmod(0o600)
    state = {"schema": 2, "current": current, "installs": {}, "compose_used": True}
    refs = {}
    for version in installs:
        directory = home / version
        directory.mkdir(parents=True, exist_ok=True)
        images = {n: f"localhost/briefcase-engine/{n}:{version}" for n in ("server", "gateway", "client")}
        ids = {n: _id(version, n) for n in images}
        refs.update({images[n]: ids[n] for n in images})
        (directory / "docker-compose.yml").write_text(
            f"services:\n  oci-jj-server:\n    image: {images['server']}\n    pull_policy: never\n")
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


def configure(env, monkeypatch, docker, volumes, running):
    if docker == "absent":
        (env["tmp"] / "bin" / "docker").unlink()
    elif docker.startswith("fail:"):
        monkeypatch.setenv("FAKE_DOCKER_FAIL", docker[5:])
    names = {"none": {}, "ours": {n: LABEL for n in VOLUME_NAMES}, "foreign": {VOLUME_NAMES[0]: {}}}[volumes]
    monkeypatch.setenv("FAKE_DOCKER_VOLUMES", json.dumps(names))
    monkeypatch.setenv("FAKE_DOCKER_SERVERS", json.dumps({"none": [], "A": [_id(A, "server")],
                                                          "B": [_id(B, "server")]}[running]))


DOCKERS = ["ok", "absent", "fail:volume inspect", "fail:ps", "fail:inspect", "fail:compose down",
           "fail:compose up", "fail:volume rm"]
VOLUMES = ["none", "ours", "foreign"]
RUNNING = ["none", "A", "B"]
INSTALLS = ["one", "two"]
COMMANDS = ["up", "down", "down_volumes", "uninstall", "purge", "upgrade", "install"]

ALL = [(d, v, r, i, c) for d in DOCKERS for v in VOLUMES for r in RUNNING for i in INSTALLS for c in COMMANDS
       if not (r == "B" and i == "one")]  # B cannot run when it is not installed
# A seeded, stratified sample of the product (about 800 combinations): three per (docker state, command) pair, so
# every command meets every docker state, plus every upgrade with a running engine and every failing docker.
_rng = random.Random(6)
COMBOS = []
for _d in DOCKERS:
    for _c in COMMANDS:
        COMBOS += _rng.sample([x for x in ALL if x[0] == _d and x[4] == _c], 3)
COMBOS += [x for x in ALL if x[4] == "upgrade" and x[2] == "A" and x[1] == "ours" and x[3] == "one"]
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


@pytest.mark.parametrize("docker, volumes, running, installs, command", COMBOS,
                         ids=["-".join(c).replace(" ", "_").replace(":", "_") for c in COMBOS])
def test_lifecycle_invariants(env, monkeypatch, capsys, key, docker, volumes, running, installs, command):
    synthetic(env, [A] if installs == "one" else [A, B], A)
    if command in ("upgrade", "install"):
        env["platform"].release = build_release(key, version=C)
        env["platform"].index = build_index(key, [C, B, A])
    configure(env, monkeypatch, docker, volumes, running)
    secrets = env["home"] / "engine" / "engine.env"
    code = main(argv_for(command))
    capsys.readouterr()
    state = _engine.load_state()
    log = [c["argv"] for c in calls(env["log"])]
    fake = fake_state(env)
    left = fake["volumes"] if "volumes" in fake else json.loads(os.environ["FAKE_DOCKER_VOLUMES"])
    ours_left = [n for n, labels in left.items() if labels.get("ai.briefcase.engine") == "1"]

    # L1: the secrets went only because every engine volume is positively absent
    if not secrets.exists():
        assert docker not in ("absent", "fail:volume inspect"), "secrets deleted without docker answering"
        assert not ours_left, f"secrets deleted while {ours_left} remain"
    if command == "purge" and code != 0:
        assert secrets.exists() and state.get("installs"), "a failed purge must keep the secrets and the record"
    if command != "purge":
        assert secrets.exists()

    # L2: nothing was started without ownership of every volume it would mount
    ups = [a for a in log if a[:1] == ["compose"] and "up" in a]
    if ups:
        assert docker not in ("absent", "fail:volume inspect") and volumes != "foreign", \
            "containers were started without establishing who owns the volumes"

    # L3: current is what runs; an error is a failure
    servers = fake.get("servers")
    current = (state.get("installs") or {}).get(state.get("current") or "")
    if code == 0 and servers and current:
        assert current["image_ids"]["server"] in servers, \
            f"current is {current['version']} but a different release is running"
    if command == "upgrade" and running == "A" and docker.startswith("fail:") and docker[5:] in (
            "ps", "inspect", "compose up", "volume inspect"):
        assert code != 0 and state["current"] == A, "an error during the restart must fail and keep A current"
    if command in ("uninstall", "purge") and running == "A" and docker == "fail:compose down":
        assert code != 0 and A in state["installs"] and (env["home"] / "engine" / A).exists(), \
            "a failed compose down must stop the uninstall before anything is deleted"
    if command in ("uninstall", "purge") and running == "A" and docker in ("fail:ps", "fail:inspect"):
        assert code != 0 and A in state["installs"], "not knowing what runs must not delete it"

    # L4: releases are told apart by the server's image id
    if command in ("uninstall", "purge") and installs == "two" and running == "B" and docker == "ok":
        assert not [a for a in log if a[:1] == ["compose"] and "down" in a], "took down the release that runs"
        assert code == 0 or command == "purge" and volumes != "none"
        if code == 0:
            assert state["current"] == B, "current must follow what is running"

    # the command's own success means what it says
    if command == "upgrade" and code == 0:
        assert state["current"] == C
    if command == "install" and code == 0 and running in ("A", "B"):
        assert state["current"] == (A if running == "A" else B) or state["current"] == C and running == "none"


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
    (env["tmp"] / "bin" / "docker").unlink()
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
