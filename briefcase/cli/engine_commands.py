"""``briefcase-ai engine``: install and run the Briefcase engine (oci-jj) on this machine.

    briefcase-ai engine install --code bci_...     # the one command the portal shows you
    briefcase-ai engine up
    briefcase-ai engine status

The engine is proprietary and is not in this package. Entitled accounts get it from the gated
portal (https://observe.briefcasebrain.io/engine): either the one-command install with a
single-use install code, a licence token for automation, or an offline bundle. Everything is
verified against the release key pinned in this SDK (``briefcase/engine/keys``) before it is
installed. Every other subcommand works on that local install.

Exit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from briefcase.cli import _ux

GROUP = "start"
ORDER = 45

_RAW = argparse.RawDescriptionHelpFormatter
ENV_HELP = (
    "environment:\n"
    "  BRIEFCASE_LICENSE_TOKEN   licence token for automation (platform: Engine page, Licence tokens)\n"
    "  BRIEFCASE_PLATFORM_URL    the Briefcase platform (default https://observe.briefcasebrain.io)\n"
    "  BRIEFCASE_ENGINE_ADDR     use an engine running elsewhere instead of a local install\n"
    "exit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed"
)
PORTAL_HINT = "get the engine at {portal} and copy the one-command install"


def _sub(sp, name: str, help_text: str, example: str):
    return sp.add_parser(name, help=help_text, description=help_text,
                         epilog=f"example:\n  {example}\n\n{ENV_HELP}", formatter_class=_RAW)


def register(sub) -> None:
    from briefcase import engine as _engine

    p = sub.add_parser(
        "engine",
        help=_engine.help_text("Install and run the Briefcase engine (oci-jj) locally."),
        description="Install the engine from the Briefcase portal (one-command install code, licence "
                    "token or offline bundle), verify it, then start, check and upgrade it.",
        epilog="example:\n  briefcase-ai engine install --code bci_...   "
               "# from https://observe.briefcasebrain.io/engine\n\n" + ENV_HELP,
        formatter_class=_RAW,
    )
    sp = p.add_subparsers(dest="engine_sub", metavar="<subcommand>")

    inst = _sub(sp, "install", "Install the engine: one-command code, licence token, or offline bundle.",
                "briefcase-ai engine install --code bci_...   # copy it from https://observe.briefcasebrain.io/engine")
    _install_flags(inst)
    inst.set_defaults(func=cmd_install)

    upg = _sub(sp, "upgrade", "Install the newest engine release and restart onto it.",
               "briefcase-ai engine upgrade --token bcl_...")
    _install_flags(upg)
    upg.set_defaults(func=cmd_upgrade)

    ver = _sub(sp, "verify", "Verify a downloaded engine file against the release key pinned in this SDK.",
               "briefcase-ai engine verify engine-images-0.4.0-x86_64.tar --manifest engine-manifest.json")
    ver.add_argument("file", help="The downloaded file (binary, bundle, engine-manifest.json or index.json).")
    ver.add_argument("--signature", metavar="FILE", help="Its signature (default FILE.sig).")
    ver.add_argument("--manifest", metavar="FILE",
                     help="engine-manifest.json to check FILE's sha256 and size against (its .sig next to it).")
    _ux.add_json_flag(ver)
    ver.set_defaults(func=cmd_verify)

    up = _sub(sp, "up", "Start the installed engine (docker compose up).", "briefcase-ai engine up")
    up.add_argument("--wait", action="store_true", help="Wait until every service is healthy.")
    up.add_argument("--dry-run", dest="engine_dry_run", action="store_true",
                    help="Show the command that would run; start nothing.")
    up.set_defaults(func=cmd_up)

    down = _sub(sp, "down", "Stop the engine. Data is kept unless --volumes.", "briefcase-ai engine down")
    down.add_argument("--volumes", action="store_true", help="Also delete the engine's data (asks first).")
    down.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    down.add_argument("--dry-run", dest="engine_dry_run", action="store_true",
                      help="Show what would be stopped and deleted; change nothing.")
    down.set_defaults(func=cmd_down)

    st = _sub(sp, "status", "Show what is installed, the key it was verified with, and whether it runs.",
              "briefcase-ai engine status --json")
    st.add_argument("--token", metavar="T",
                    help="Licence token to check for a newer release (default $BRIEFCASE_LICENSE_TOKEN).")
    _ux.add_json_flag(st)
    st.set_defaults(func=cmd_status)

    logs = _sub(sp, "logs", "Show engine service logs.", "briefcase-ai engine logs oci-jj-server -f")
    logs.add_argument("service", nargs="?", help="One service (default: all).")
    logs.add_argument("-f", "--follow", action="store_true", help="Stream logs.")
    logs.add_argument("--tail", default="200", help="Lines per service (default 200).")
    logs.set_defaults(func=cmd_logs)

    doc = _sub(sp, "doctor", "Check Docker, the install, its signature, the images and the gRPC endpoint.",
               "briefcase-ai engine doctor")
    _ux.add_json_flag(doc)
    doc.set_defaults(func=cmd_doctor)

    un = _sub(sp, "uninstall", "Remove the engine install (and with --purge, its data and secrets).",
              "briefcase-ai engine uninstall --dry-run")
    un.add_argument("--purge", action="store_true", help="Also delete data volumes, secrets and certificates.")
    un.add_argument("--force-without-docker", action="store_true",
                    help="With --purge and no docker: allowed only for an install that never created volumes.")
    un.add_argument("--dry-run", dest="engine_dry_run", action="store_true", help="Show the plan; change nothing.")
    un.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    un.set_defaults(func=cmd_uninstall)


def _install_flags(p) -> None:
    online = p.add_argument_group("online (from the portal)")
    online.add_argument("--code", metavar="bci_...",
                        help="Single-use install code from the portal's 'Install with one command' box.")
    online.add_argument("--token", metavar="bcl_...",
                        help="Licence token, for automation (default $BRIEFCASE_LICENSE_TOKEN).")
    online.add_argument("--version", dest="engine_version", metavar="X",
                        help="Engine release with --token (default: newest). A code is bound to its version.")
    online.add_argument("--platform-url", metavar="URL",
                        help="Platform URL (default $BRIEFCASE_PLATFORM_URL, else https://observe.briefcasebrain.io).")
    offline = p.add_argument_group("offline (air-gapped)")
    offline.add_argument("--bundle", metavar="FILE", help="engine-images-<version>-<arch>.tar from the portal.")
    offline.add_argument("--manifest", metavar="FILE", help="engine-manifest.json of the same version.")
    offline.add_argument("--signature", metavar="FILE", help="The manifest's signature (default MANIFEST.sig).")
    offline.add_argument("--binary", metavar="FILE",
                         help="The oci-bai-<version>-<os>-<arch>.tar.gz download (default: next to the manifest).")
    tls = p.add_mutually_exclusive_group()
    tls.add_argument("--tls", action="store_true",
                     help="Serve gRPC over TLS with a self-signed certificate (needs openssl). An upgrade keeps the "
                          "installed setting unless you pass this or --no-tls.")
    tls.add_argument("--no-tls", action="store_true", help="Serve gRPC without TLS (an upgrade of a TLS install).")
    p.add_argument("--no-images", action="store_true",
                   help="Only the oci-bai client binary; no Docker images (for an engine run elsewhere).")
    p.add_argument("--dry-run", dest="engine_dry_run", action="store_true",
                   help="Show the plan; change nothing (an install code is not redeemed).")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    _ux.add_json_flag(p)


# --------------------------------------------------------------------------- helpers


def _dry(args) -> bool:
    return bool(getattr(args, "engine_dry_run", False) or getattr(args, "dry_run", False))


_PINS: List[Tuple[Dict[str, str], List[str]]] = []


def _with_pin(argv, kw):
    """``(argv, kw)`` as docker must be started: Compose with ``COMPOSE_REMOVE_ORPHANS`` set to false (unset, Compose
    would take it from ``engine.env``, and the caller's own value must not turn one of our calls into a deletion;
    ``--remove-orphans`` on the command line still wins where an upgrade asks for it), and against the daemon pinned
    by :func:`_pinned`."""
    argv = list(argv)
    if "env" not in kw and argv[:1] == ["docker"]:
        env = dict(os.environ)
        if argv[:2] == ["docker", "compose"]:
            env["COMPOSE_REMOVE_ORPHANS"] = "false"
        if _PINS:
            for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
                env.pop(name, None)
            pin_env, pin_args = _PINS[-1]
            env.update(pin_env)
            argv = [argv[0], *pin_args, *argv[1:]]  # global flags come before the subcommand
        kw = dict(kw, env=env)
    return argv, kw


def _runner():
    """``subprocess.run`` for docker, started as :func:`_with_pin` says."""
    def run(argv, **kw):
        argv, kw = _with_pin(argv, kw)
        return subprocess.run(argv, **kw)
    return run


@contextlib.contextmanager
def _pinned(identity: Optional[str]) -> Iterator[None]:
    """Run every docker call inside the block against the daemon whose identity was verified, by the endpoint (and the
    certificates) that verification read: a context repointed afterwards, while a confirmation waits, changes
    nothing."""
    pin = getattr(identity, "pin_env", None)
    if not pin:
        yield
        return
    _PINS.append((dict(pin), list(getattr(identity, "pin_args", []))))
    try:
        yield
    finally:
        _PINS.pop()


def still_the_daemon(daemon: Optional[str]) -> bool:
    """Called inside the pin, after the confirmation: does the daemon at the pinned endpoint still report the id it had
    when it was verified? ``True`` when nothing was pinned (nothing needs the daemon)."""
    from briefcase.engine import install as _install_mod

    if daemon is None or not getattr(daemon, "pin_env", None):
        return True
    wanted = _install_mod._split_id(daemon)[1]
    pin_env, pin_args = getattr(daemon, "pin_env"), getattr(daemon, "pin_args", [])
    now = _install_mod._daemon_id(subprocess.run, os.environ, pin_env, pin_args)
    return wanted is not None and now == wanted


def _pinned_runner():
    return _runner()


def _local_install() -> Optional[Dict[str, Any]]:
    from briefcase import engine as _engine

    return _engine.current_install()


def _require_local(args) -> Optional[Dict[str, Any]]:
    record = _local_install()
    if record is None:
        from briefcase import engine as _engine

        _engine.not_installed(bool(getattr(args, "json", False)))
    return record


def _compose(record: Dict[str, Any], *args: str) -> List[str]:  # the one place a compose argv is built
    from briefcase.engine import compose

    return compose.compose_argv(Path(record["compose"]), Path(record["env_file"]), *args)


def legacy_stack() -> Dict[str, List[str]]:
    """What the removed 4.4 ``briefcase stack`` left behind. A running one is a Compose service named
    ``oci-jj-server`` in a project other than this engine's own (found by Compose labels, so unrelated
    containers that merely have ``oci-jj-`` in their name are ignored); ``projects`` maps each project to
    its containers and ``volumes`` lists the volumes those projects own."""
    errors: List[str] = []

    def lines(argv: List[str]) -> List[str]:
        res = _run(argv, capture_output=True, text=True)
        if _rc(res) != 0:
            errors.append(" ".join(argv[:3]))
            return []
        return [ln for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.strip()]

    from briefcase.engine import compose as _compose

    projects: Dict[str, List[str]] = {}
    for row in lines(["docker", "ps", "--filter", "label=com.docker.compose.service=oci-jj-server", "--format",
                      '{{.Label "com.docker.compose.project"}}\t{{.Names}}']):
        project, _, name = row.partition("\t")
        if project and project != _compose.PROJECT:
            projects.setdefault(project, []).append(name)
    volumes: List[str] = []
    for project in projects:
        volumes += lines(["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={project}",
                          "--format", "{{.Name}}"])
    return {"projects": projects, "volumes": volumes, "errors": errors}  # type: ignore[dict-item]


def legacy_stack_message(found: Dict[str, Any]) -> str:
    parts = []
    for project, names in found["projects"].items():
        parts.append(f"a 4.4 stack is still running (Compose project {project}: {', '.join(names[:3])}; it uses "
                     f"the same ports): stop it with `docker compose -p {project} down`")
    if found["projects"]:
        parts.append("there is no data transfer from the old stack to the new engine, which starts empty"
                     + (f"; the old volumes ({', '.join(found['volumes'])}) are kept until you remove them with "
                        "`docker volume rm`" if found["volumes"] else ""))
    return "; ".join(parts)


def volume_ownership() -> Tuple[List[str], List[str]]:
    """``(foreign, unknown)`` for the volumes the engine would mount: those that exist without this engine's label,
    and those whose ownership could not be established (docker missing, erroring or refusing to inspect). Only a
    positive answer (carries the label, or does not exist) lets a start go ahead."""
    from briefcase.engine import compose as _compose

    foreign, unknown = [], []
    for key in _compose.VOLUME_KEYS:
        name = f"{_compose.PROJECT}_{key}"
        state, _ = _volume_state(name)
        if state == "foreign":
            foreign.append(name)
        elif state in ("unknown", "nodocker"):
            unknown.append(name)
    return foreign, unknown


def foreign_volumes() -> List[str]:
    """Volumes the new engine would mount that exist but were not created by it (no ``ai.briefcase.engine``
    label): a 4.4 project named ``briefcase-engine`` used the same names, and Compose would reuse that data."""
    return volume_ownership()[0]


def foreign_volumes_message(names: List[str]) -> str:
    from briefcase.engine import compose as _compose

    inspect = "; ".join(f"`docker volume inspect {n}`" for n in names)
    remove = " and ".join(f"`docker volume rm {n}`" for n in names)
    return (f"the volume(s) {', '.join(names)} already exist and were not created by this engine (they lack the "
            f"{_compose.ENGINE_LABEL} label), so they may hold another stack's data: look with {inspect}; if you "
            f"do not need that data, remove them with {remove}, then run `briefcase-ai engine up` again")


def guard_start(record: Dict[str, Any]) -> Optional[int]:
    """The checks every command that starts containers runs first (``up``, an upgrade's restart): the images are
    the verified ones, no 4.4 stack is running on the same ports, and every volume that would be mounted was
    created by this engine. Returns an exit code to stop with, or ``None``."""
    drift = image_drift(record)
    if drift:
        for line in drift:
            print(f"  {line}")
        return _ux.fail("an engine image changed since it was verified at install; not starting it",
                        fix=_reinstall_fix(record))
    legacy = legacy_stack()
    if legacy["projects"]:
        first = next(iter(legacy["projects"]))
        return _ux.fail(legacy_stack_message(legacy),
                        fix=f"docker compose -p {first} down   # then: briefcase-ai engine up")
    if legacy.get("errors"):
        return _ux.fail("could not check whether a 4.4 stack is running on the same ports (docker failed), so "
                        "nothing was started", fix="briefcase-ai engine doctor")
    foreign, unknown = volume_ownership()
    if foreign:
        return _ux.fail(foreign_volumes_message(foreign), fix="briefcase-ai engine doctor")
    if unknown:
        return _ux.fail(f"could not establish who owns the volume(s) {', '.join(unknown)} (docker is missing, "
                        "failed or refused to inspect them), so nothing was started: Compose would mount them "
                        "without asking", fix="briefcase-ai engine doctor")
    return None


def _volume_state(name: str) -> Tuple[str, str]:
    """``(state, detail)``: ``ours`` (carries this engine's label), ``foreign`` (exists without it), ``absent``,
    ``nodocker`` (no docker program, so no volume of ours can be managed) or ``unknown`` (docker could not say)."""
    from briefcase.engine import compose as engine_compose

    label = '{{ index .Labels "' + engine_compose.ENGINE_LABEL + '" }}'
    res = _run(["docker", "volume", "inspect", "--format", label, name], capture_output=True, text=True)
    if res is None:
        return "nodocker", ""
    stderr = (getattr(res, "stderr", "") or "").strip()
    if _rc(res) == 0:
        return ("ours" if (getattr(res, "stdout", "") or "").strip() == "1" else "foreign"), ""
    if "no such volume" in stderr.lower():
        return "absent", ""
    return "unknown", stderr or f"exit {_rc(res)}"


def remove_engine_volumes(confirmed: Confirmed, allow_no_docker: bool = False) -> Tuple[List[str], List[str]]:
    """Remove the volumes carrying this engine's label, and check each is gone. Returns ``(kept, problems)``:
    ``kept`` are same-named volumes this engine did not create (left alone), ``problems`` say why a labelled
    volume could not be confirmed gone."""
    from briefcase.engine import compose as engine_compose

    if not isinstance(confirmed, Confirmed):
        raise TypeError("removing volumes needs a Confirmed token")
    kept: List[str] = []
    problems: List[str] = []
    for key in engine_compose.VOLUME_KEYS:
        name = f"{engine_compose.PROJECT}_{key}"
        state, detail = _volume_state(name)
        if state == "ours":
            res = _run(["docker", "volume", "rm", name], capture_output=True, text=True)
            if _rc(res) != 0:
                why = (getattr(res, "stderr", "") or "").strip() or "docker volume rm failed"
                problems.append(f"{name} was not removed ({why})")
            elif _volume_state(name)[0] != "absent":
                problems.append(f"{name} is still there after docker volume rm")
        elif state == "foreign":
            kept.append(name)
        elif state == "unknown":
            problems.append(f"{name} could not be checked ({detail})")
        elif state == "nodocker" and not allow_no_docker:
            problems.append(f"{name} could not be checked: docker is not installed or not on PATH, so it is not "
                            "known whether any engine volume is left")
    return kept, problems


def _kept_message(kept: List[str]) -> str:
    return (f"left in place, not created by this engine: {', '.join(kept)}; remove by hand if you do not need "
            "them: " + " && ".join(f"docker volume rm {n}" for n in kept))


class Confirmed:
    """Proof that the person authorised this destructive step. Only :func:`_authorize` can make one, and a dry run
    never gets one, so no deleting function can be reached without it (each takes one as a required argument)."""

    _key = object()

    def __init__(self, what: str, key: object = None) -> None:
        if key is not Confirmed._key:
            raise TypeError("a Confirmed token comes only from the confirmation step")
        self.what = what


def _mint(question: str, answer: Optional[bool]) -> Confirmed:
    """The one place a confirmation token is made, and only for a yes: anything else raises."""
    if answer is not True:
        raise PermissionError("a confirmation token needs the answer yes")
    return Confirmed(question, Confirmed._key)


def _authorize(args, question: str, flag_fix: str) -> Tuple[Optional[Confirmed], int]:
    """``(token, 0)`` when confirmed, else ``(None, exit code)``. A dry run is ``(None, 0)``."""
    if _dry(args):
        print("dry run: nothing was changed.")
        return None, 0
    answer = _ux.confirm(question, default=False, assume_yes=bool(getattr(args, "yes", False)))
    if not answer:
        return None, _ux.fail("not deleting anything without confirmation", fix=flag_fix,
                              code=2 if answer is None else 1)
    return _mint(question, answer), 0


@dataclass
class Deletion:
    """What one destructive command will remove, decided once by :func:`plan_deletion` from ``installed.json`` read
    under the install lock. The preview, the confirmation and the deleting code all read this one object."""

    mode: str = "none"  # install | leftovers | stop | volumes | ambiguous | none
    version: Optional[str] = None
    record: Optional[Dict[str, Any]] = None
    others: List[str] = field(default_factory=list)
    last: bool = False  # nothing else would remain installed: shared volumes and secrets may go
    stop: bool = False  # compose down for this release (only if it is the one running)
    images: List[str] = field(default_factory=list)
    directory: Optional[Path] = None
    volumes: bool = False  # remove the data volumes this engine created
    secrets: bool = False  # delete engine.env and the certificates
    daemon: Optional[str] = None  # the verified daemon every docker call is pinned to
    steps: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _daemon_check(state: Dict[str, Any], need_record: bool) -> Tuple[Optional[str], List[str]]:
    """``(identity to pin, problems)``: the daemon this install recorded must be the one docker talks to now. With
    ``need_record`` a record that names no daemon is a problem too (nothing is then deleted on a guess); without it,
    an unrecorded daemon is allowed through (stopping containers) but never a different one."""
    from briefcase.engine import install as _install_mod

    recorded = list(state.get("docker_contexts") or [])
    here = _install_mod.docker_identity(_runner())
    if not recorded:
        if need_record:
            return None, ["the record does not say which Docker daemon this install used, so a probe here says "
                          "nothing about its volumes"]
        if here is None:
            return None, ["could not tell which Docker daemon this is, so nothing can be pinned to it"]
        return here, []
    if here is None or not all(_install_mod.daemon_matches(entry, here) for entry in recorded):
        return None, ["the Docker daemon is not the one this install used (it used "
                      f"{', '.join(recorded)}; now {here or 'unknown'}), so a probe here says nothing about "
                      "its volumes"]
    return here, []


def _engine_state_used(state: Dict[str, Any]) -> bool:
    from briefcase import engine as _engine

    return _engine.compose_history(state)


def plan_deletion(state: Dict[str, Any], purge: bool = False, force_without_docker: bool = False,
                  down: Optional[str] = None, version: Optional[str] = None) -> Deletion:
    """The one place that decides what a destructive engine command removes. ``state`` must have been read under
    the install lock. ``down`` is ``"stop"`` or ``"volumes"`` for ``engine down``; otherwise this is an uninstall
    (``purge`` adds the shared volumes and secrets when nothing else would remain installed).

    ``version`` is the release the person was shown before the lock was taken (``None``: none was installed). It
    only names which release to remove: whether it is still installed, what else is, whether volumes and secrets
    may go, and on which daemon are all read from ``state``. Anything that appeared meanwhile (an install landing
    while a purge of the leftovers waited) makes the command stop rather than guess.

    Volumes are touched only when the record says a stack was ever run (``compose_used``) and the daemon docker
    talks to is the one recorded for it. A client-only record never authorises their removal, whatever flags say."""
    installs = {v: r for v, r in (state.get("installs") or {}).items() if isinstance(r, dict)}
    used = _engine_state_used(state)
    current = installs.get(state.get("current") or "")
    if not down:
        current = installs.get(version or "")
        if version and current is None:
            plan = Deletion(mode="ambiguous")
            plan.problems.append(f"engine {version} is no longer installed")
            return plan
    plan = Deletion()
    if down:
        plan.mode, plan.record = down, current
        if current is None or not current.get("compose"):
            plan.mode = "none"
            return plan
        plan.version, plan.stop = current["version"], True
        plan.volumes = down == "volumes" and used
        plan.steps.append("docker compose down (the engine stops; data volumes are kept)"
                          if down == "stop" else "docker compose down")
        if down == "volumes":
            plan.steps.append("remove the data volumes this engine created (others are left)" if used else
                              "nothing else: this install never ran a stack, so no data volumes were created")
    elif current is None:
        if installs:
            plan.mode = "ambiguous"
            plan.problems.append("engine " + ", ".join(sorted(installs)) + " was installed while this command "
                                 "waited, so it is not what was shown and nothing was removed")
            return plan
        if not (purge and used):
            return plan
        plan.mode, plan.last = "leftovers", True
        plan.volumes, plan.secrets = True, True
        plan.steps.append("remove the data volumes this engine created (others are left)")
    else:
        plan.mode, plan.record, plan.version = "install", current, current["version"]
        plan.others = [v for v in installs if v != current["version"]]
        plan.last = purge and not plan.others
        plan.stop = bool(current.get("compose"))
        plan.images = sorted(set((current.get("images") or {}).values()))
        plan.directory = Path(current.get("manifest") or current.get("compose") or "").parent
        plan.volumes = plan.last and used
        plan.secrets = plan.last
        if plan.stop:
            plan.steps.append("docker compose down (only if this release is the one running)")
        if plan.volumes:
            plan.steps.append("remove the data volumes this engine created (others are left)")
        plan.steps += [f"docker image rm {ref}" for ref in plan.images]
        plan.steps.append(f"delete {plan.directory}")
        if purge and plan.others:
            plan.notes.append(f"engine {', '.join(plan.others)} is still installed, so the shared secrets, "
                              "certificates and data volumes are kept")
    if plan.secrets:
        from briefcase import engine as _engine

        plan.steps.append(f"delete {_engine.secrets_path()} and {_engine.engine_dir() / 'tls'}")
    if (plan.mode in ("volumes", "leftovers") or plan.last) and not used:
        plan.notes.append("this install never ran a stack, so no data volumes were created and none are touched")
    if force_without_docker and used:
        plan.problems.append("--force-without-docker is only allowed for an install that never created volumes, and "
                             "this one's record says it did")
    touches_docker = used and (plan.stop or plan.volumes or bool(plan.images))
    if touches_docker:
        plan.daemon, found = _daemon_check(state, need_record=True)
        plan.problems += found
    return plan


def _need_token(confirmed: Any) -> None:
    if not isinstance(confirmed, Confirmed):
        raise TypeError("deleting needs a Confirmed token")


def restart_removing_orphans(record: Dict[str, Any], confirmed: Optional[Confirmed]) -> int:
    """The upgrade's restart: ``up -d --remove-orphans`` recreates what changed and removes the containers of services
    the new release no longer has, so it needs the confirmation the install asked for."""
    if not isinstance(confirmed, Confirmed):
        raise TypeError("restarting onto another release removes containers, which needs a Confirmed token")
    return _rc(_run(_compose(record, "up", "-d", "--remove-orphans")))


def teardown(record: Dict[str, Any], remove_volumes: bool, confirmed: Optional[Confirmed] = None) -> int:
    """``compose down`` without ever passing ``-v`` (it would take a same-named volume of another stack with
    it). With ``remove_volumes``, remove only the volumes carrying this engine's label, check they are gone, and
    name any other volume of the same name that is left, with the command to remove it by hand. Non-zero when
    ``down`` failed or a labelled volume remains."""
    if not isinstance(confirmed, Confirmed):
        raise TypeError("stopping the engine removes its containers, which needs a Confirmed token")
    rc = _rc(_run(_compose(record, "down")))
    if rc != 0 or not remove_volumes:
        return rc
    kept, problems = remove_engine_volumes(confirmed)
    if kept:
        print(_kept_message(kept))
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1 if problems else 0


def _rc(result) -> int:
    return getattr(result, "returncode", 1) if result is not None else 1


def _run(argv: List[str], **kw):
    try:
        return _runner()(argv, **kw)
    except FileNotFoundError:
        return None


def port_open(addr: str, timeout: float = 0.3) -> bool:
    """TCP connect to a ``scheme://host:port`` or ``host:port`` address."""
    hostport = addr.split("://", 1)[-1].rstrip("/")
    host, _, port = hostport.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------- install / upgrade


class _Usage(Exception):
    pass


def _options(args):
    """InstallOptions from the flags, or ``_Usage(message, fix)``."""
    from briefcase import engine as _engine
    from briefcase.engine import install as _inst

    platform = _inst.platform_url(getattr(args, "platform_url", None))
    portal = _inst.portal(platform)
    code = getattr(args, "code", None)
    token_flag = getattr(args, "token", None)
    bundle, manifest = getattr(args, "bundle", None), getattr(args, "manifest", None)
    version = getattr(args, "engine_version", None)
    if bundle or manifest:
        if not (bundle and manifest):
            raise _Usage("an offline install needs both --bundle and --manifest",
                         "briefcase-ai engine install --bundle engine-images-<version>-<arch>.tar "
                         "--manifest engine-manifest.json")
        if code or token_flag:
            raise _Usage("--bundle installs offline; it takes no --code or --token",
                         "briefcase-ai engine install --bundle FILE --manifest FILE")
        return _inst.InstallOptions(platform_url=platform, bundle=Path(bundle), manifest=Path(manifest),
                                    signature=Path(args.signature) if getattr(args, "signature", None) else None,
                                    binary=Path(args.binary) if getattr(args, "binary", None) else None,
                                    tls=bool(getattr(args, "tls", False)),
                                    skip_images=bool(getattr(args, "no_images", False)))
    if code and token_flag:
        raise _Usage("give either --code or --token, not both", "briefcase-ai engine install --code bci_...")
    if code:
        if not code.startswith("bci_"):
            hint = " (that looks like a licence token: use --token)" if code.startswith("bcl_") else ""
            raise _Usage(f"install codes start with bci_{hint}", f"copy the command from {portal}")
        if version:
            raise _Usage("an install code is bound to the version picked in the portal; --version is for --token",
                         f"pick the version at {portal}, then run the command it shows")
        token = None
    else:
        token = token_flag or os.environ.get(_engine.ENV_LICENSE)
        if not token:
            raise _Usage("no install code, licence token or offline bundle given",
                         PORTAL_HINT.format(portal=portal) + " (briefcase-ai engine install --code bci_...)")
        if token.startswith("bci_"):
            raise _Usage("that is an install code, not a licence token",
                         f"briefcase-ai engine install --code {token[:8]}...")
    return _inst.InstallOptions(platform_url=platform, code=code, token=token, version=version,
                                tls=bool(getattr(args, "tls", False)),
                                skip_images=bool(getattr(args, "no_images", False)))


def _preflight(opts) -> None:
    """Everything checkable before an install code is redeemed."""
    from briefcase.engine import install as _inst
    from briefcase.engine import trust as _trust

    _trust.require_keys()
    if not _trust.crypto_available() and shutil.which("cosign") is None:
        raise _trust.TrustError("cannot check signatures: neither the `cryptography` package nor `cosign` is "
                                "installed", fix="pip install 'briefcase-ai[engine]'")
    if not opts.skip_images and _inst.Docker(_runner()).available() is None:
        raise _inst.InstallError(
            "Docker is not installed or its daemon is not running",
            fix="install Docker (https://docs.docker.com/get-docker/), start it, then re-run "
                "(or --no-images for the client binary only)")


def _show_plan(args, opts, upgrading: bool, manifest=None) -> None:
    from briefcase.engine import install as _inst

    if getattr(args, "json", False):
        return
    what = f"engine {manifest.version}" if manifest else "the engine"
    where = "the offline bundle" if opts.mode == "bundle" else opts.platform_url
    print(f"{'Upgrade to' if upgrading else 'Install'} {what} from {where}:")
    for step in _inst.plan_steps(opts, manifest):
        print(f"  - {step}")
    if manifest is not None and manifest.data.get("notes"):
        print(f"release notes: {manifest.data['notes'].strip().splitlines()[0]}")


def _install(args, upgrading: bool) -> int:
    from briefcase import engine as _engine
    from briefcase.engine import bundle as _bundle
    from briefcase.engine import install as _inst
    from briefcase.engine import trust as _trust

    errors = (_inst.InstallError, _trust.TrustError, _bundle.BundleError, _engine.StateUnreadable)
    as_json = bool(getattr(args, "json", False))
    try:
        opts = _options(args)
    except _Usage as exc:
        return _ux.fail(str(exc.args[0]), fix=exc.args[1], code=2)
    try:
        from briefcase.engine import compose as _compose

        used = _engine.compose_history(_engine.load_state_strict())
        if not getattr(args, "no_images", False):  # a client-only install writes no secrets
            _compose.check_secrets(_engine.secrets_path(), volumes_may_exist=used)
        else:
            _engine.read_env_file(_engine.secrets_path(), strict=True)
    except _engine.StateUnreadable as exc:
        return _ux.fail(str(exc), fix=exc.fix)
    previous = _engine.current_install()
    previous_compose = None
    if previous and previous.get("compose"):
        try:
            previous_compose = Path(previous["compose"]).read_bytes()
        except OSError:
            previous_compose = None
    if upgrading and previous and not getattr(args, "tls", False) and not getattr(args, "no_tls", False):
        opts.tls = bool(previous.get("tls"))  # an upgrade keeps what was installed unless told otherwise
    # Which release is current is decided after the install, under the install lock, from what actually runs
    # (see _settle); until then an upgrade, or an install next to compose releases, leaves current alone.
    uses_compose_before = any(isinstance(r, dict) and r.get("compose")
                              for r in (_engine.load_state().get("installs") or {}).values())
    opts.make_current = not upgrading and not uses_compose_before
    daemons_before = list(_engine.load_state().get("docker_contexts") or [])
    log = (lambda line: None) if as_json else print
    portal = _inst.portal(opts.platform_url)
    used_up = f"this install code is now used up; get a new one at {portal} and run the command it shows"
    try:
        _preflight(opts)
    except errors as exc:
        return _ux.fail(str(exc), fix=exc.fix)

    prepared = verified = manifest = None
    try:
        if opts.mode == "token":
            prepared = _inst.prepare_online(opts)
            manifest = prepared.manifest
        elif opts.mode == "bundle":
            verified = _inst.verify_offline(opts)
            manifest = verified[0]
    except errors as exc:
        code = exc.code if isinstance(exc, _inst.InstallError) else 1
        return _ux.fail(str(exc), fix=exc.fix, code=code)

    if _dry(args):
        if as_json:
            return _ux.print_json({"dry_run": True, "mode": opts.mode,
                                   "version": manifest.version if manifest else None,
                                   "key_id": manifest.key_id if manifest else None,
                                   "steps": _inst.plan_steps(opts, manifest)})
        _show_plan(args, opts, upgrading, manifest)
        print("dry run: nothing was downloaded or written"
              + ("; the install code was not redeemed." if opts.mode == "code" else "."))
        _ux.print_next(_rerun(opts), "to do it")
        return 0
    _show_plan(args, opts, upgrading, manifest)
    if previous and manifest is not None and previous.get("version") == manifest.version and not upgrading:
        log(f"engine {manifest.version} is already installed; re-checking files and images")
    answer = _ux.confirm("Go ahead?", assume_yes=bool(getattr(args, "yes", False)))
    confirmed = _mint("Go ahead?", answer) if answer else None
    if answer is None:
        return _ux.fail("installing the engine needs confirmation",
                        fix="re-run with --yes (or --dry-run to only look)", code=2)
    if not answer:
        print("cancelled; nothing was changed"
              + (" and the install code was not used." if opts.mode == "code" else "."))
        return 1

    preflight_daemon: Optional[str] = None
    if upgrading and daemons_before and (uses_compose_before or not getattr(args, "no_images", False)):
        # before anything is written: a stack may be running, or this upgrade installs the images that need the daemon
        here = _inst.docker_identity(_runner())
        preflight_daemon = here
        if here is None or not all(_inst.daemon_matches(entry, here) for entry in daemons_before):
            return _ux.fail("the Docker daemon is not the one the installed engine ran on (it ran on "
                            f"{', '.join(daemons_before)}; now {here or 'unknown'}), so nothing was installed",
                            fix="briefcase-ai engine doctor")
    try:
        if opts.mode == "bundle":
            record = _inst.offline_install(opts, runner=_runner(), log=log, verified=verified)
        elif opts.mode == "token":
            assert prepared is not None
            record = _inst.complete_online(opts, prepared, runner=_runner(), log=log)
        else:
            response = _inst.request_install(opts.platform_url, code=opts.code, target=_inst.host_target())
            try:
                prepared = _inst.prepare_online(opts, response=response)
                record = _inst.complete_online(opts, prepared, runner=_runner(), log=log)
            except errors + (RuntimeError,) as exc:
                return _ux.fail(str(exc), fix=used_up)
    except errors as exc:
        fix = exc.fix
        if opts.mode == "token" and fix is None:
            fix = "briefcase-ai engine install --token ...   # re-run; verified downloads are reused"
        elif opts.mode == "bundle" and fix is None:
            fix = "briefcase-ai engine verify " + str(opts.bundle) + " --manifest " + str(opts.manifest)
        return _ux.fail(str(exc), fix=fix)
    except RuntimeError as exc:  # --tls without openssl
        return _ux.fail(str(exc), fix="install openssl, or install without --tls")
    stop_code = _settle(opts, record, previous, upgrading, log, previous_compose, daemons_before, confirmed,
                        preflight_daemon=preflight_daemon)
    if stop_code is not None:
        return stop_code
    if as_json:
        return _ux.print_json({"installed": record})
    where = Path(record["compose"]).parent if record.get("compose") else Path(record["manifest"]).parent
    print(f"engine {record['version']} installed in {where} (verified with key {record['key_id']})")
    if record.get("binary"):
        print(f"  client: {record['binary']}")
    _ux.print_next("briefcase-ai engine status" if upgrading else "briefcase-ai engine up")
    return 0


def _settle(opts, record: Dict[str, Any], previous: Optional[Dict[str, Any]], upgrading: bool, log,
            previous_compose: Optional[bytes] = None, daemons_before: Optional[List[str]] = None,
            confirmed: Optional["Confirmed"] = None,
            preflight_daemon: Optional[str] = None) -> Optional[int]:
    """After an install or upgrade wrote ``record``: decide, under the install lock and from what actually runs,
    which release is current, and restart onto ``record`` when an upgrade (or a TLS change) needs it. Current is
    what runs. Anything docker cannot establish (an error, an engine that matches no install, two installs that fit)
    fails the command and leaves current as it was. Returns an exit code to stop with, or ``None``."""
    from briefcase import engine as _engine
    from briefcase.engine import install as _inst

    try:
        with _inst.engine_lock(opts.env):
            return _settle_locked(opts, record, previous, upgrading, log, previous_compose, daemons_before or [],
                                  confirmed, preflight_daemon)
    except _engine.StateUnreadable as exc:
        return _ux.fail(f"{exc}; engine {record['version']} was installed but not made current", fix=exc.fix)


def _settle_locked(opts, record: Dict[str, Any], previous: Optional[Dict[str, Any]], upgrading: bool, log,
                   previous_compose: Optional[bytes], daemons_before: List[str],
                   confirmed: Optional["Confirmed"] = None,
                   preflight_daemon: Optional[str] = None) -> Optional[int]:
    """The body of :func:`_settle`, run under the install lock."""
    from briefcase import engine as _engine
    from briefcase.engine import install as _inst

    installs = _engine.load_state_strict(opts.env).get("installs") or {}
    if not any(isinstance(r, dict) and r.get("compose") for r in installs.values()):
        # Client-only installs: there is no stack to ask docker about, so this works without docker.
        _inst.set_current(record["version"], opts.env, locked=True)
        return None
    tls_differs = bool(previous and previous.get("version") == record["version"]
                       and bool(previous.get("tls")) != bool(record.get("tls")))

    def commit_history() -> None:
        """The daemon this outcome verified is recorded as the owner of the stack's volumes, under the lock that decided
        it, and only then: an install writes no history of its own, so a refusal has nothing to undo and no other
        command's entry to lose."""
        if not _inst.has_id(daemon):
            return
        fresh = _engine.load_state_strict(opts.env)
        known = list(fresh.get("docker_contexts") or [])
        merged = _inst._merge_daemon(known, str(daemon))
        if merged != known:
            fresh["docker_contexts"] = merged
            _inst.save_state(fresh, opts.env)

    def restore() -> None:
        """The install already wrote the new transport (record and Compose file) and its daemon-history change; put
        back what is running. Reads the state strictly: a record that cannot be read is not rewritten from nothing."""
        if tls_differs and previous:
            try:
                fresh = _engine.load_state_strict(opts.env)
                fresh.setdefault("installs", {})[record["version"]] = previous
                _inst.save_state(fresh, opts.env)
                if previous_compose is not None and previous.get("compose") == record.get("compose"):
                    Path(previous["compose"]).write_bytes(previous_compose)
            except (_engine.StateUnreadable, OSError) as exc:
                print(f"error: could not put the previous transport back ({exc}); the engine record and the "
                      "running server may disagree: run `briefcase-ai engine install` again", file=sys.stderr)

    daemon = _inst.docker_identity(_runner(), opts.env)
    if daemon is None:  # never an unpinned probe: it could answer for a daemon the restart would not use
        restore()
        return _ux.fail("could not tell which Docker daemon this is, so what runs could not be checked and engine "
                        f"{record['version']} was installed but not made current", fix="briefcase-ai engine doctor")

    if preflight_daemon is not None and not _inst.same_daemon_id(preflight_daemon, daemon):
        restore()
        return _ux.fail(f"the Docker daemon is not the one that was verified before the install ({preflight_daemon}; "
                        f"now {daemon}), so engine {record['version']} was installed but not made current",
                        fix="briefcase-ai engine doctor")
    if (upgrading or tls_differs) and daemons_before \
            and not all(_inst.daemon_matches(entry, daemon) for entry in daemons_before):
        restore()
        was = (previous or {}).get("version", "the old release")
        return _ux.fail(f"the Docker daemon is not the one engine {was} ran on (it ran on "
                        f"{', '.join(daemons_before)}; now {daemon}), so what runs there could not be checked and "
                        f"engine {record['version']} was installed but not made current",
                        fix="briefcase-ai engine doctor")
    try:
        with _pinned(daemon):  # what runs is read on the daemon the restart would use
            status, running, why = running_releases()
    except Exception as exc:  # noqa: BLE001 - the install already wrote the new transport
        restore()
        return _ux.fail(f"{type(exc).__name__}: {exc}", fix="briefcase-ai engine doctor")
    current = _engine.current_install(opts.env) or previous
    keep = current["version"] if current else "the old release"
    if status != "ok":
        restore()
        return _ux.fail(f"{why}, so engine {record['version']} was installed but not made current; {keep} is "
                        "still current", fix="briefcase-ai engine doctor")
    others = [v for v in running if v != record["version"]]
    tls_changed = bool(previous and previous.get("version") == record["version"]
                       and bool(previous.get("tls")) != bool(record.get("tls")) and running)
    if upgrading and (others or tls_changed):
        if not record.get("compose"):
            restore()
            return _ux.fail(f"engine {(others or running)[0]} is running and {record['version']} was installed "
                            "without images, so it cannot take over", fix="briefcase-ai engine down")
        try:
            with _pinned(daemon):  # the checks, the record and the start all reach the same daemon
                stop = guard_start(record)
                if stop is not None:
                    restore()
                    return stop
                if not still_the_daemon(daemon):  # the daemon at the pinned endpoint is the one that was identified
                    restore()
                    return _ux.fail("the Docker daemon at the verified endpoint is not the one that was identified "
                                    "(it changed during the checks), so the engine was not restarted",
                                    fix="briefcase-ai engine doctor")
                if _inst.note_daemon(_runner(), opts.env, identity=daemon) is None:  # before compose can create one
                    restore()
                    return _ux.fail("the Docker daemon did not state its id, so the engine was not restarted",
                                    fix="briefcase-ai engine doctor")
                log(f"restarting onto {record['version']}" + (" (TLS changed) " if tls_changed else " ")
                    + "(data volumes are kept)...")
                started = restart_removing_orphans(record, confirmed) == 0
        except Exception as exc:  # noqa: BLE001 - whatever failed, the new transport must not stay half-applied
            restore()
            return _ux.fail(f"{type(exc).__name__}: {exc}", fix="briefcase-ai engine doctor")
        if not started:
            restore()
            return _ux.fail("the new engine did not start", fix="briefcase-ai engine logs")
        commit_history()
        _inst.set_current(record["version"], opts.env, locked=True)
        return None
    if not upgrading and others:  # an install next to a running release leaves that one current
        commit_history()
        _inst.set_current(others[0], opts.env, locked=True)
        log(f"engine {others[0]} is running and stays current; `briefcase-ai engine upgrade` moves to "
            f"{record['version']}")
        return None
    commit_history()
    _inst.set_current(record["version"], opts.env, locked=True)
    return None


def _rerun(opts) -> str:
    if opts.mode == "bundle":
        return f"briefcase-ai engine install --bundle {opts.bundle} --manifest {opts.manifest} --yes"
    if opts.mode == "code":
        return "briefcase-ai engine install --code <your install code> --yes"
    return "briefcase-ai engine install --yes   # with BRIEFCASE_LICENSE_TOKEN set"


def cmd_install(args, store, engine, stack) -> int:
    return _install(args, upgrading=False)


def cmd_upgrade(args, store, engine, stack) -> int:
    if _require_local(args) is None:
        return 3
    return _install(args, upgrading=True)


# --------------------------------------------------------------------------- verify


def _is_index_envelope(path: Path) -> bool:
    """Is ``path`` a release index (one object with its own signature, no ``.sig`` next to it)?"""
    try:
        if path.stat().st_size > 2 * (1 << 20):
            return False
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "payload" in data and "signature" in data and "key_id" in data


def cmd_verify(args, store, engine, stack) -> int:
    from briefcase.engine import install as _inst
    from briefcase.engine import trust as _trust

    as_json = bool(getattr(args, "json", False))
    path = Path(args.file)
    if not path.is_file():
        return _ux.fail(f"{path} does not exist", fix="briefcase-ai engine verify PATH/TO/FILE", code=2)
    result: Dict[str, Any] = {"file": str(path), "ok": False}
    try:
        keys = _trust.require_keys()
        sig_path = Path(args.signature) if args.signature else path.with_name(path.name + ".sig")
        if args.manifest:
            mpath = Path(args.manifest)
            msig = mpath.with_name(mpath.name + ".sig")
            if not mpath.is_file() or not msig.is_file():
                return _ux.fail(f"{mpath if not mpath.is_file() else msig} does not exist",
                                fix="download engine-manifest.json and engine-manifest.json.sig from the portal",
                                code=2)
            manifest = _trust.load_manifest(mpath.read_bytes(), msig.read_bytes(), keys)
            result.update(manifest=str(mpath), version=manifest.version, key_id=manifest.key_id,
                          key_file=manifest.key.label)
            if path.resolve() == mpath.resolve():
                result.update(ok=True, checked=["signature", "schema"])
            else:
                entry = _trust.manifest_entry_for(manifest, path)
                if entry is None:
                    raise _trust.TrustError(f"{path.name} is not a file of engine {manifest.version} "
                                            "(neither its name nor its sha256 is in the signed manifest)",
                                            fix="download it again from the portal; do not install it")
                sig = sig_path.read_bytes() if sig_path.is_file() else None
                if sig is None:
                    other = path.with_name(entry["signature"])
                    sig = other.read_bytes() if other.is_file() else None
                how = _trust.check_file(path, entry, manifest, sig)
                result.update(ok=True, checked=["size", "sha256"] + (["signature"] if how == "signature" else []))
        elif _is_index_envelope(path):
            _, index_key = _trust.open_index(path.read_bytes(), keys)
            result.update(ok=True, key_id=index_key.key_id, key_file=index_key.label,
                          checked=["signature", "schema"])
        else:
            if not sig_path.is_file():
                return _ux.fail(f"no signature for {path.name} ({sig_path} does not exist)",
                                fix=f"briefcase-ai engine verify {path} --signature FILE.sig   "
                                    "# or --manifest engine-manifest.json", code=2)
            raw_sig = sig_path.read_bytes()
            looks_manifest = path.stat().st_size <= _trust.MAX_MANIFEST_BYTES and path.suffix == ".json"
            data = None
            if looks_manifest:
                try:
                    data = json.loads(path.read_bytes())
                except ValueError:
                    data = None
            if isinstance(data, dict) and "schema_version" in data and "versions" not in data:
                manifest = _trust.load_manifest(path.read_bytes(), raw_sig, keys)
                result.update(ok=True, version=manifest.version, key_id=manifest.key_id,
                              key_file=manifest.key.label, checked=["signature", "schema"])
            else:
                key = _trust.verify_with_any(path, raw_sig, keys)
                if key is None:
                    raise _trust.TrustError(f"{path.name}: the signature does not verify with any key pinned in "
                                            "this SDK", fix="download the file and its .sig again; do not use it")
                result.update(ok=True, key_id=key.key_id, key_file=key.label, checked=["signature"])
    except (_trust.TrustError, _inst.InstallError) as exc:
        result["error"] = str(exc)
        if as_json:
            _ux.print_json(result)
            return 1
        return _ux.fail(str(exc), fix=exc.fix)
    if as_json:
        return _ux.print_json(result)
    version = f" (engine {result['version']})" if result.get("version") else ""
    print(f"verified: {path.name}{version}")
    print(f"  key:     {result['key_id']} ({result['key_file']})")
    print(f"  checked: {', '.join(result['checked'])}")
    _ux.print_next("briefcase-ai engine install --bundle FILE --manifest engine-manifest.json",
                   "install a verified offline bundle")
    return 0


def project_containers(stopped: bool = False) -> Tuple[bool, List[Dict[str, str]]]:
    """``(ok, containers)``: every running container of the engine Compose project (with ``stopped``, the stopped ones
    too) as ``{image, service, config, env}`` (image id, Compose service, the Compose file it was started from, and
    its environment as JSON).
    ``ok`` is false when docker could not answer."""
    from briefcase.engine import compose as engine_compose

    res = _run(["docker", "ps", "-q", "--no-trunc", *(["-a"] if stopped else []),
                "--filter", f"label=com.docker.compose.project={engine_compose.PROJECT}"],
               capture_output=True, text=True)
    if res is None or _rc(res) != 0:
        return False, []
    rows = []
    fmt = ('{{.Image}}\t{{index .Config.Labels "com.docker.compose.service"}}\t'
           '{{index .Config.Labels "com.docker.compose.project.config_files"}}\t{{json .Config.Env}}\t'
           '{{index .Config.Labels "com.docker.compose.oneoff"}}')
    for container in (getattr(res, "stdout", "") or "").split():
        inspected = _run(["docker", "inspect", "--format", fmt, container], capture_output=True, text=True)
        if inspected is None or _rc(inspected) != 0:
            return False, []
        image, _, rest = (getattr(inspected, "stdout", "") or "").strip().partition("\t")
        service, _, rest = rest.partition("\t")
        config, _, rest = rest.partition("\t")
        env_json, _, oneoff = rest.partition("\t")
        if oneoff.strip().lower() == "true":  # a `compose run` leftover: Compose's own reconcile ignores it
            continue
        rows.append({"image": image, "service": service, "config": config, "env": env_json})
    return True, rows


def running_releases() -> Tuple[str, List[str], str]:
    """``(status, versions, why)``: which installed release is running. ``status`` is ``ok`` (``versions`` is empty
    when nothing runs), ``error`` (docker could not say), ``unrecognised`` (something runs that matches no install)
    or ``ambiguous`` (more than one install fits). A release fits when its server container runs the image recorded
    for its server, every other container started from one of this SDK's images runs an image of that same release,
    and, to tell releases that share every image apart, the Compose file matches. Sidecars shared between
    releases and labels prove nothing on their own."""
    from briefcase import engine as _engine

    ok, rows = project_containers()
    if not ok:
        return "error", [], "docker could not list the engine containers"
    if not rows:
        return "ok", [], ""
    installs = {v: r for v, r in (_engine.load_state().get("installs") or {}).items()
                if isinstance(r, dict) and r.get("compose")}
    known = {i for r in installs.values() for i in (r.get("image_ids") or {}).values()}
    servers = [row for row in rows if row["service"] == "oci-jj-server"]
    ours = {row["image"] for row in rows if row["image"] in known}
    fits = []
    for version, rec in installs.items():
        ids = set((rec.get("image_ids") or {}).values())
        recorded = rec.get("image_ids") or {}

        def agrees(row: Dict[str, str]) -> bool:
            """A container of a service this release records must run exactly the image it recorded for it (an
            unknown image in the gateway is not this release, whatever the server runs)."""
            key = record_key(recorded, row["service"])
            if key is not None:
                return row["image"] == recorded[key]
            return row["image"] not in known or row["image"] in ids

        if servers and all(row["image"] == recorded.get("server") for row in servers) and ours <= ids \
                and all(agrees(row) for row in rows):
            fits.append(version)
    if len(fits) > 1:
        paths = {c for row in servers for c in row["config"].split(",")}
        narrowed = [v for v in fits if str(Path(installs[v]["compose"])) in paths]
        if len(narrowed) == 1:
            fits = narrowed
    if len(fits) == 1:
        return "ok", fits, ""
    if not fits:
        return "unrecognised", [], ("an engine is running that matches no install (a different image, or a mix of "
                                    "releases): this install did not start it")
    return "ambiguous", [], f"releases {', '.join(sorted(fits))} share the same images and cannot be told apart"


def running_install(record: Dict[str, Any]) -> Optional[bool]:
    """Is this release the one running? Every release uses the Compose project ``briefcase-engine``, so
    ``compose down`` for one takes down whichever is up. ``None`` when that cannot be established (docker failed, an
    unrecognised or ambiguous engine runs), which no caller may treat as "no"."""
    status, versions, _ = running_releases()
    if status != "ok":
        return None
    return record.get("version") in versions


# --------------------------------------------------------------------------- up / down / logs


def _require_stack(args) -> Optional[Dict[str, Any]]:
    record = _require_local(args)
    if record is not None and not record.get("compose"):
        _ux.fail(f"engine {record['version']} was installed without images (--no-images); there is no "
                 "local stack to run", fix="briefcase-ai engine install --code bci_...   # without --no-images")
        return None
    return record


def image_drift(record: Dict[str, Any], docker=None) -> List[str]:
    """Images whose local tag no longer points at the image ID verified at install."""
    from briefcase.engine import install as _inst

    docker = docker or _inst.Docker(_runner())
    out = []
    for name, ref in sorted((record.get("images") or {}).items()):
        expected = (record.get("image_ids") or {}).get(name)
        if not expected:
            continue
        actual = docker.image_id(ref)
        if actual != expected:
            out.append(f"{ref} is {'missing' if actual is None else 'a different image (' + actual[:19] + ')'}, "
                       f"not the verified {expected[:19]}")
    return out


def _reinstall_fix(record: Dict[str, Any]) -> str:
    if record.get("source") == "bundle":
        return "briefcase-ai engine install --bundle FILE --manifest engine-manifest.json --yes"
    if record.get("source") == "token":
        return "briefcase-ai engine install --yes   # with BRIEFCASE_LICENSE_TOKEN set"
    portal = (record.get("platform_url") or "https://observe.briefcasebrain.io").rstrip("/") + "/engine"
    return f"get a new install code at {portal} and run the command it shows"


def record_key(recorded: Dict[str, Any], service: str) -> Optional[str]:
    """The key ``recorded`` (a record's ``image_ids``, which keeps the manifest's spelling, hyphens included) uses for
    a Compose service, or ``None`` when the release does not pin it. The one place service names are mapped."""
    names = ["server"] if service == "oci-jj-server" else [service, service.replace("-", "_"),
                                                           service.replace("_", "-")]
    return next((name for name in names if name in recorded), None)


TRANSPORT_SERVICES = ("oci-jj-server", "gateway", "workers", "reconstructor", "verdict-worker")


def _container_tls(service: str, env: List[str], want_tls: bool) -> Optional[bool]:
    """Does this container's own configuration say gRPC runs over TLS? The server says it by serving a certificate
    (or by allowing plaintext). A client says it by the address it dials, which must be exactly the server's address
    for that transport (the file the release wrote has no other), so a client sent elsewhere counts as the other
    transport. ``None`` when the container says nothing."""
    values = dict(e.partition("=")[::2] for e in env)
    if service == "oci-jj-server":
        if "OCI_JJ_TLS_CERT" in values:
            return True
        return False if values.get("OCI_JJ_ALLOW_PLAINTEXT") == "1" else None
    target = values.get("OCI_JJ_SERVER_GRPC")
    if target is None:
        return None
    wanted = ("https://" if want_tls else "") + "oci-jj-server:50051"
    return want_tls if target == wanted else not want_tls


def _pinned_sidecar_images(record: Dict[str, Any]) -> Dict[str, str]:
    """``{service: image reference}`` for the services the Compose file pins by digest."""
    import re

    try:
        text = Path(record["compose"]).read_text(encoding="utf-8")
    except (OSError, KeyError):
        return {}
    return {service: ref for service, ref in re.findall(r"^  (\S+):\n(?:    .*\n)*?    image: (\S+)", text, re.M)
            if "@sha256:" in ref}


def existing_containers_state(record: Dict[str, Any],
                              surplus_is_a_problem: bool = True) -> Tuple[Optional[str], Set[str]]:
    """``(problem, services that have a container)`` for the containers that already exist, running or stopped.

    ``up`` keeps what exists, so what exists must be what ``record`` describes: a container made from another
    release's image, or serving the other transport, would be reused and reported as started. ``problem`` is ``None``
    when nothing is wrong."""
    ok, rows = project_containers(stopped=True)
    if not ok:
        return "docker could not list the engine's containers", set()
    present = {row["service"] for row in rows}
    recorded = record.get("image_ids") or {}
    want_tls = bool(record.get("tls"))
    counts: Dict[str, int] = {}
    stale: List[str] = []
    pinned = _pinned_sidecar_images(record)
    for row in rows:
        service = row["service"]
        counts[service] = counts.get(service, 0) + 1
        key = record_key(recorded, service)
        if key is None:
            ref = pinned.get(service)  # a sidecar the release does not record: its digest-pinned default is the check
            if ref:
                from briefcase.engine import install as _inst

                expected = _inst.Docker(_runner()).image_id(ref)
                if expected is None:
                    stale.append(f"the pinned image of {service} ({ref}) is not available locally to compare with")
                elif row["image"] != expected:
                    stale.append(f"{service} runs a different image than the one the Compose file pins")
            continue
        if row["image"] != recorded[key]:
            stale.append(f"{service} runs a different image than engine {record['version']} verified")
        if service not in TRANSPORT_SERVICES:
            continue
        try:
            env = [e for e in json.loads(row.get("env") or "[]") if isinstance(e, str)]
        except ValueError:
            env = []
        has_tls = _container_tls(service, env, want_tls)
        if has_tls is None:
            stale.append(f"the transport {service} was created for could not be read from docker")
        elif has_tls != want_tls:
            stale.append(f"{service} was created for {'TLS' if has_tls else 'plaintext'} but engine "
                         f"{record['version']} is installed {'with' if want_tls else 'without'} TLS")
    if stale:
        return "; ".join(stale), present
    extra = sorted(f"{service} ({n})" for service, n in counts.items() if n > 1)
    if extra and surplus_is_a_problem:
        return (f"more than one container exists for {', '.join(extra)}, which `up` does not touch but the engine "
                "was not made to run"), present
    return None, present


def _start_argv(record: Dict[str, Any], present: Set[str],
                wait: bool) -> Tuple[Optional[List[str]], str, Set[str]]:
    """The Compose command that starts the engine without being able to delete anything, or ``(None, why)``.

    With every service already having a container, ``compose start``: it only starts containers that exist (Compose
    v5.5.0 start.go, startService), so a replica another client adds meanwhile is left alone. ``compose up`` runs
    the scale-down of reconcile.go (reconcileService) first, and removes the extra container with its writable
    layer. ``up --no-recreate`` is used only when no service has a container at all, so there is nothing to scale
    down. A partial stack would need both and is refused."""
    res = _run(_compose(record, "config", "--services"), capture_output=True, text=True)
    services = {ln.strip() for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.strip()}
    if res is None or _rc(res) != 0 or not services:
        return None, "docker compose could not list the engine's services, so nothing was started", set()
    if not present:
        wait_flags = ["--wait", "--wait-timeout", "300"] if wait else []
        return _compose(record, "up", "-d", "--no-recreate", *wait_flags), "", services
    missing = sorted(services - present)
    if missing:
        return None, (f"the stack is partial (no container for {', '.join(missing)}); starting it would need Compose "
                      "to create containers beside existing ones, which can remove containers"), services
    return _compose(record, "start"), "", services  # `start` has no --wait before Compose 2.24.7: cmd_up waits


def wait_running(record: Dict[str, Any], services: Set[str], timeout: float = 300.0, interval: float = 2.0) -> bool:
    """Wait until every one of ``services`` has a container, and each of its containers is running and, where it has a
    health check, healthy. One-off containers (`compose run` leftovers) are not the service."""
    deadline = time.monotonic() + timeout
    while True:
        rows = [r for r in (_services(record, detail=True) or []) if not r.get("oneoff")]
        if services and all(
                any(r["service"] == name for r in rows)
                and all(r["state"] == "running" and r["health"] in ("", "healthy") for r in rows
                        if r["service"] == name) for name in services):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _locked(args):
    """The install lock for a command that changes something; a dry run takes none (it would create the lock file
    and the engine directory on a clean home) and reads state as it is."""
    from briefcase.engine import install as _inst

    return contextlib.nullcontext() if _dry(args) else _inst.engine_lock()


def cmd_up(args, store, engine, stack) -> int:
    from briefcase import engine as _engine
    from briefcase.engine import install as _inst

    if _require_stack(args) is None:
        return 3 if _local_install() is None else 1
    with _locked(args):  # not while an upgrade is moving current: the release is chosen under the lock
        record = _require_stack(args)
        if record is None:
            return 1
        # Starting changes nothing that exists (a recreated or removed container loses its writable layer): see
        # _start_argv. Moving to another release is an upgrade, which asks.
        if _dry(args):
            shown = _compose(record, "up", "-d", "--no-recreate") + (
                ["--wait", "--wait-timeout", "300"] if getattr(args, "wait", False) else [])
            print("dry run: would run `docker compose start` over existing containers, or " + " ".join(shown)
                  + " on a fresh stack\nnothing was started, recreated or removed.")
            return 0
        daemon = _inst.docker_identity(_runner())
        if daemon is None:
            return _ux.fail("could not tell which Docker daemon this is, so the engine was not started (its volumes "
                            "could not be traced to a daemon afterwards)", fix="briefcase-ai engine doctor")
        with _pinned(daemon):  # the start checks, the record and compose all reach the daemon that was identified
            stop = guard_start(record)
            if stop is not None:
                return stop
            problem, present = existing_containers_state(record)
            if problem:
                return _ux.fail(f"{problem}; nothing was started, because `up` would reuse them as they are",
                                fix="briefcase-ai engine down --yes   # removes the containers, keeps the volumes")
            command, why, expected = _start_argv(record, present, bool(getattr(args, "wait", False)))
            if command is None:
                return _ux.fail(why, fix="briefcase-ai engine down --yes   # removes the containers, keeps the volumes")
            try:
                if _inst.note_daemon(_runner(), identity=daemon) is None:  # before compose can create a volume
                    return _ux.fail("the Docker daemon did not state its id, so it cannot be told from another one "
                                    "at the same address and nothing was started", fix="briefcase-ai engine doctor")
            except _engine.StateUnreadable as exc:
                return _ux.fail(str(exc), fix=exc.fix)
            if not still_the_daemon(daemon):  # the last look before Compose opens its own connection
                return _ux.fail("the Docker daemon at the verified endpoint is not the one that was inspected (it "
                                "changed after the inventory), so nothing was started",
                                fix="briefcase-ai engine doctor")
            rc = _rc(_run(command))
            if not still_the_daemon(daemon):
                return _ux.fail("the Docker daemon at the verified endpoint changed during the start, so what the "
                                "inventory says cannot be trusted", fix="briefcase-ai engine status")
            if rc == 0:
                # What Compose created or started is checked against the record: every expected service has its
                # container, image and transport. A container another Compose client made Compose remove while it
                # reconciled (v5.5.0 reconcile.go scales a service down to its expected count) is not detected beyond
                # this: the SDK watches nothing and creates or removes nothing of its own.
                after, now = existing_containers_state(record, surplus_is_a_problem=False)  # not ours to judge
                missing = sorted(expected - now)
                if after or missing:
                    return _ux.fail(("after the start, " + after if after else
                                     f"after the start, no container exists for {', '.join(missing)}")
                                    + "; the engine is not in the state it should be", fix="briefcase-ai engine status")
            if rc == 0 and getattr(args, "wait", False) and "start" in command \
                    and not wait_running(record, expected):  # on the pinned daemon, for the services of the file
                return _ux.fail("the engine did not become healthy within 300 seconds", fix="briefcase-ai engine logs")
    if rc != 0:
        return _ux.fail("docker compose up failed (see above)", fix="briefcase-ai engine doctor")
    print(f"engine {record['version']} started: gRPC {record['grpc_addr']}, registry gateway {record['gateway']}")
    _ux.print_next("briefcase-ai engine status")
    return 0


def cmd_down(args, store, engine, stack) -> int:
    from briefcase import engine as _engine

    if _require_stack(args) is None:
        return 3 if _local_install() is None else 1
    volumes = bool(getattr(args, "volumes", False))
    with _locked(args):  # lifecycle commands do not interleave
        try:
            state = _engine.load_state_strict()
        except _engine.StateUnreadable as exc:
            return _ux.fail(str(exc), fix=exc.fix)
        plan = plan_deletion(state, down="volumes" if volumes else "stop")
        if plan.record is None:
            return 1
        print(f"Stop engine {plan.version}:")
        for step in plan.steps:
            print(f"  - {step}")
        for remark in plan.notes:
            print(f"  note: {remark}")
        if plan.problems:
            return _ux.fail(_purge_blocked(plan.problems), fix="briefcase-ai engine doctor", code=1)
        # `compose down` removes the containers and their writable layers, so stopping is confirmed like deleting.
        token, code = _authorize(args, "Delete the engine's data volumes (database, blobs, registry)?" if volumes else
                                 "Stop the engine and remove its containers (data volumes are kept)?",
                                 "briefcase-ai engine down --volumes --yes" if volumes
                                 else "briefcase-ai engine down --yes")
        if token is None:
            return code
        with _pinned(plan.daemon):
            if not still_the_daemon(plan.daemon):
                return _ux.fail("the Docker daemon at the verified endpoint is not the one that was verified (it "
                                "changed while the confirmation was open); nothing was deleted",
                                fix="briefcase-ai engine doctor")
            rc = teardown(plan.record, plan.volumes, token)
    if rc != 0:
        return _ux.fail("docker compose down failed, or an engine volume could not be removed (see above)",
                        fix="briefcase-ai engine doctor")
    _ux.print_next("briefcase-ai engine up", "to start it again")
    return 0


def cmd_logs(args, store, engine, stack) -> int:
    record = _require_stack(args)
    if record is None:
        return 3 if _local_install() is None else 1
    argv = ["logs", "--tail", str(getattr(args, "tail", "200"))]
    if getattr(args, "follow", False):
        argv.append("-f")
    if getattr(args, "service", None):
        argv.append(args.service)
    return _rc(_run(_compose(record, *argv)))


# --------------------------------------------------------------------------- status / doctor


def _services(record: Dict[str, Any], detail: bool = False) -> Optional[List[Dict[str, Any]]]:
    if not record.get("compose"):
        return []
    res = _run(_compose(record, "ps", "--all", "--format", "json"), capture_output=True, text=True)
    if res is None or _rc(res) != 0:
        return None
    text = (getattr(res, "stdout", "") or "").strip()
    rows: List[Dict[str, Any]] = []
    if text.startswith("["):
        try:
            rows = json.loads(text)
        except ValueError:
            rows = []
    else:
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    out = [{"service": r.get("Service") or r.get("Name"), "state": r.get("State"), "health": r.get("Health") or ""}
           for r in rows if isinstance(r, dict)]
    if detail:  # for the wait: which rows are `compose run` leftovers (not part of the public status output)
        for entry, r in zip(out, [r for r in rows if isinstance(r, dict)]):
            entry["oneoff"] = "com.docker.compose.oneoff=True" in str(r.get("Labels") or "")
    return out


def engine_state() -> Dict[str, Any]:
    from briefcase import engine as _engine

    record = _engine.current_install()
    settings = _engine.client_settings()
    remote = _engine.configured_address()
    return {
        "installed": record is not None,
        "version": (record or {}).get("version"),
        "key_id": (record or {}).get("key_id"),
        "install": record,
        "addr": settings["addr"],
        "addr_source": settings["source"],
        "remote": remote,
        "reachable": port_open(settings["addr"]),
    }


def update_check(record: Dict[str, Any], token: Optional[str]) -> Dict[str, Any]:
    """``{"checked": bool, "latest", "update_available", "reason"}``. Only with a licence token:
    an install code is single-use, so status never spends one."""
    from briefcase.engine import install as _inst

    if not token:
        return {"checked": False, "reason": "no licence token (BRIEFCASE_LICENSE_TOKEN); install codes are single-use"}
    platform = _inst.platform_url(None)
    try:
        latest = _inst.latest_version(platform, token)
    except Exception as exc:  # noqa: BLE001 - status must always render
        return {"checked": False, "reason": str(exc)}
    return {"checked": True, "latest": latest["version"],
            "update_available": _inst.newer(latest["version"], record["version"])}


def cmd_status(args, store, engine, stack) -> int:
    from briefcase import engine as _engine

    state = engine_state()
    record = state["install"]
    if record is not None:
        state["services"] = _services(record)
        token = getattr(args, "token", None) or os.environ.get(_engine.ENV_LICENSE)
        state["update"] = update_check(record, token)
    if getattr(args, "json", False):
        shown = _ux.print_json(state)
        return _engine.EXIT_NOT_INSTALLED if record is None and not state["remote"] else shown
    if record is None and not state["remote"]:
        print(_engine.NOT_INSTALLED)
        return _engine.EXIT_NOT_INSTALLED
    if record is not None:
        print(f"engine {record['version']} installed {record.get('installed_at', '')} "
              f"(from {record.get('source', 'platform')})")
        print(f"  verified with: {record.get('key_id') or 'unknown (installed by an older SDK)'}")
        client = record.get("client") or {}
        if client.get("mode") == "container":
            print(f"  oci-bai:       runs from {client.get('image')} (no build for this OS)")
        elif record.get("binary"):
            print(f"  client binary: {record['binary']}")
        if record.get("compose"):
            print(f"  compose:       {record['compose']}")
        services = state.get("services")
        if services is None:
            print("  services:      unknown (docker compose ps failed)")
        elif not services:
            print("  services:      none running")
        else:
            for row in services:
                health = f" ({row['health']})" if row["health"] else ""
                print(f"  {row['service']:<16} {row['state']}{health}")
        update = state["update"]
        if update.get("checked"):
            print(f"  newest:        {update['latest']}"
                  + (" (newer than this install)" if update["update_available"] else " (up to date)"))
        else:
            print(f"  newest:        not checked ({update['reason']})")
    print(f"  gRPC:          {state['addr']} ({state['addr_source']}) "
          f"{'reachable' if state['reachable'] else 'not reachable'}")
    if record is not None and (state.get("update") or {}).get("update_available"):
        _ux.print_next("briefcase-ai engine upgrade", "a newer engine release is available")
    elif record is not None and record.get("compose") and not state["reachable"]:
        _ux.print_next("briefcase-ai engine up")
    else:
        _ux.print_next("briefcase-ai graph log <ref>", "look at what the engine has recorded")
    return 0


def doctor_checks(record: Optional[Dict[str, Any]], client_factory=None) -> List[tuple]:
    from briefcase import engine as _engine
    from briefcase.cli.doctor import FAIL, OK, WARN
    from briefcase.engine import install as _install_mod
    from briefcase.engine import trust as _trust

    out: List[tuple] = []
    docker = _install_mod.Docker(_runner())
    version = docker.available()
    out.append((OK, "docker", version) if version else (FAIL, "docker", "not found or daemon not running"))
    if version:
        res = _run(["docker", "compose", "version", "--short"], capture_output=True, text=True)
        out.append((OK, "docker compose", (getattr(res, "stdout", "") or "").strip())
                   if _rc(res) == 0 else (FAIL, "docker compose", "the compose plugin is missing"))
    if version:
        legacy = legacy_stack()
        if legacy["projects"]:
            out.append((WARN, "4.4 stack", legacy_stack_message(legacy)))
        foreign = foreign_volumes()
        if foreign:
            out.append((FAIL, "engine volumes", foreign_volumes_message(foreign)))
    keys = _trust.load_keys()
    out.append((OK, "release keys", ", ".join(k.key_id[:19] + "..." for k in keys)) if keys
               else (WARN, "release keys", _trust.NO_KEY_MESSAGE))
    if record is None:
        remote = _engine.configured_address()
        out.append((WARN if remote else FAIL, "install",
                    f"no local install; using {remote}" if remote else _engine.NOT_INSTALLED))
    else:
        out.append((OK, "install", f"engine {record['version']} ({record.get('source', '?')})"))
        manifest = Path(record.get("manifest") or "")
        sig = manifest.with_name(manifest.name + ".sig")
        if record.get("manifest") and manifest.is_file() and sig.is_file():
            try:
                verified = _trust.load_manifest(manifest.read_bytes(), sig.read_bytes(), keys, running_sdk="999")
                out.append((OK, "signature", f"manifest verifies with {verified.key_id}"))
            except _trust.TrustError as exc:
                out.append((FAIL, "signature", str(exc)))
        else:
            out.append((WARN, "signature", "no stored manifest (installed by an older SDK); re-install to verify"))
        client = record.get("client") or {}
        binary = Path(record.get("binary") or "")
        if client.get("mode") == "container":
            out.append((OK, "client binary", f"none for this OS; oci-bai runs from {client.get('image')}"))
        else:
            out.append((OK, "client binary", str(binary)) if record.get("binary") and binary.exists()
                       and os.access(binary, os.X_OK)
                       else (FAIL, "client binary", f"missing: {binary} (re-run briefcase-ai engine install)"))
        env_file = Path(record.get("env_file", ""))
        if record.get("compose"):
            if not env_file.exists():
                out.append((FAIL, "secrets", f"missing: {env_file} (re-run briefcase-ai engine install)"))
            elif os.name == "posix" and env_file.stat().st_mode & 0o077:
                out.append((WARN, "secrets", f"{env_file} is readable by others; chmod 600 it"))
            else:
                out.append((OK, "secrets", str(env_file)))
        if version:
            ids = record.get("image_ids") or {}
            for name, ref in sorted((record.get("images") or {}).items()):
                actual = docker.image_id(ref)
                if actual is None:
                    out.append((FAIL, f"image {name}", f"{ref} missing ({_reinstall_fix(record)})"))
                elif ids.get(name) and actual != ids[name]:
                    out.append((FAIL, f"image {name}", f"{ref} is not the image verified at install "
                                                       f"({_reinstall_fix(record)})"))
                else:
                    digest = (record.get("image_digests") or {}).get(name, "")
                    out.append((OK, f"image {name}", f"{ref} {digest[:19]}"))
    settings = _engine.client_settings()
    if not port_open(settings["addr"]):
        out.append((WARN, "gRPC", f"{settings['addr']} not reachable (briefcase-ai engine up)"))
        return out
    try:
        from briefcase.engine.client import EngineClient, EngineError

        client_obj = (client_factory or EngineClient)()
        try:
            # WhoAmI: any valid token may call it; it names the tenant, role and registry namespace.
            me = client_obj.call("WhoAmI", {})
            namespace = me.get("registry_namespace") or "?"
            out.append((OK, "gRPC", f"{settings['addr']}: tenant {me.get('tenant') or '-'}, role "
                                    f"{me.get('role') or '-'}, push to <gateway>/{namespace}/<image>"))
        except EngineError as exc:
            if exc.code == "UNIMPLEMENTED":  # an engine older than WhoAmI: any answer proves auth works
                try:
                    client_obj.call("ListRefs", {"repo": client_obj.repo})
                    out.append((OK, "gRPC", f"{settings['addr']} answered an authenticated call"))
                except EngineError as inner:
                    out.append((OK if inner.code in ("NOT_FOUND", "PERMISSION_DENIED") else FAIL, "gRPC", str(inner)))
            else:
                out.append((FAIL, "gRPC", str(exc)))
    except Exception as exc:  # noqa: BLE001 - e.g. grpcio missing
        out.append((WARN, "gRPC", f"not checked: {exc}"))
    return out


def cmd_doctor(args, store, engine, stack) -> int:
    from briefcase.cli.doctor import _MARK, summarize

    record = _local_install()
    results = doctor_checks(record)
    if getattr(args, "json", False):
        _ux.print_json([{"severity": s, "check": label, "detail": d} for s, label, d in results])
        return summarize(results)
    for sev, label, detail in results:
        print(f"{_MARK[sev]} {label}: {detail}")
    rc = summarize(results)
    if rc == 0:
        _ux.print_next("briefcase-ai engine status")
    return rc


# --------------------------------------------------------------------------- uninstall


def _remove_install(version: Optional[str], purge: bool, force_without_docker: bool,
                    confirmed: Confirmed, shown_daemon: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Run under ``engine_lock``, with a :class:`Confirmed` token. Re-reads installed.json and plans again with
    :func:`plan_deletion`, so what is removed is decided now and not when the plan was shown. ``version`` is the
    release the person confirmed (``None``: the leftovers of an earlier uninstall).

    Returns ``(outcome, note)``: ``removed``, ``gone`` (what was confirmed is not what is current any more) or
    ``blocked`` (nothing was deleted, and ``note`` says why: the daemon is not the recorded one, docker could not
    say whether this release runs, ``compose down`` failed, or a purge could not confirm every engine volume
    absent, in which case the secrets are kept too)."""
    from briefcase import engine as _engine
    from briefcase.engine import install as _install_mod

    _need_token(confirmed)
    try:
        state = _engine.load_state_strict()
    except _engine.StateUnreadable as exc:
        return "blocked", str(exc)
    plan = plan_deletion(state, purge=purge, force_without_docker=force_without_docker, version=version)
    if plan.mode == "ambiguous" or plan.version != version:
        return "gone", (f"engine {version} is no longer the one to remove; nothing was changed" if version
                        else "what an earlier uninstall left is no longer what is there; nothing was changed")
    if plan.problems:
        return "blocked", _purge_blocked(plan.problems)
    if shown_daemon is not None and not _install_mod.same_daemon_id(shown_daemon, plan.daemon):
        return "blocked", ("the Docker daemon is not the one that was shown before the confirmation (it changed "
                           "meanwhile); nothing was removed")
    with _pinned(plan.daemon):
        if plan.daemon is not None and not still_the_daemon(plan.daemon):
            return "blocked", ("the Docker daemon at the verified endpoint is not the one that was verified (it "
                               "changed while the confirmation was open); nothing was removed")
        # Every release shares one Compose project, so only the release whose server is up is taken down.
        if plan.mode == "install" and plan.stop and plan.record is not None:
            running = running_install(plan.record)
            if running is None:
                return "blocked", ("could not tell whether engine " + str(version) + " is running (docker "
                                   "failed), so nothing was removed")
            if running and _rc(_run(_compose(plan.record, "down"))) != 0:
                return "blocked", (f"docker compose down failed (see above), so nothing was removed of engine "
                                   f"{version}")
        if plan.volumes:
            # The volumes go whichever release created them, and the secrets only after every one is confirmed
            # absent.
            kept, problems = remove_engine_volumes(confirmed)
            if problems:
                return "blocked", _purge_blocked(problems)
            if kept:
                print(_kept_message(kept))
        if plan.mode == "install" and plan.record is not None:
            docker = _install_mod.Docker(_pinned_runner())
            for ref in plan.images:
                docker.remove_image(ref)
            if plan.directory is not None and plan.directory.name == version:
                shutil.rmtree(plan.directory, ignore_errors=True)
            history = _engine_state_used(state)  # before the install that implies it is gone
            state["compose_used"] = history
            state["installs"].pop(version, None)
            remaining = sorted(state["installs"])
            if state.get("current") == version or state.get("current") not in state["installs"]:
                # Current is what is running: the release whose server is up, else the newest left.
                state["current"] = next((v for v in remaining if state["installs"][v].get("compose")
                                         and running_install(state["installs"][v])), None) or \
                    (remaining[-1] if remaining else None)
    if plan.secrets:
        for path in (_engine.secrets_path(), _engine.engine_dir() / "tls"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
    if plan.mode == "install" and (state["installs"] or (state.get("compose_used") and not plan.last)):
        # Volumes and secrets outlive an ordinary uninstall, so the record of that (and of which daemon holds them)
        # stays: a tombstone with no installs. Only a purge that confirmed every volume absent forgets it.
        _install_mod.save_state(state)
    else:
        try:
            _engine.installed_path().unlink()
        except FileNotFoundError:
            pass
    return "removed", ("; ".join(plan.notes) or None)


def _purge_blocked(problems: List[str]) -> str:
    """Why a purge stopped. Other volumes may already have been removed; the secrets and the install record are
    kept so the same command can be run again."""
    from briefcase import engine as _engine

    return ("the engine was not purged: " + "; ".join(problems) + f". {_engine.secrets_path()} (the database and "
            "registry passwords those volumes were created with) and the install record were kept; other engine "
            "volumes may already have been removed")


def cmd_uninstall(args, store, engine, stack) -> int:
    from briefcase import engine as _engine

    purge = bool(getattr(args, "purge", False))
    force_wd = bool(getattr(args, "force_without_docker", False))
    shown = (_engine.current_install() or {}).get("version")  # the release the person sees; only names the target
    # Order, for every destructive command: lock, re-read state, decide what is deleted (one function, from that
    # state), verify the daemon, authorise, and only then (and never in a dry run) call anything that deletes.
    with _locked(args):
        try:
            state = _engine.load_state_strict()
        except _engine.StateUnreadable as exc:
            return _ux.fail(str(exc), fix=exc.fix)
        plan = plan_deletion(state, purge=purge, force_without_docker=force_wd, version=shown)
        if plan.mode == "ambiguous":
            return _ux.fail("; ".join(plan.problems) + "; nothing was changed", fix="briefcase-ai engine status",
                            code=1)
        if plan.mode == "none":
            _engine.not_installed(False)
            return 3
        print(f"Uninstall engine {plan.version}:" if plan.record else "Remove what an earlier uninstall left:")
        for step in plan.steps:
            print(f"  - {step}")
        for remark in plan.notes:
            print(f"  note: {remark}")
        if plan.problems:
            return _ux.fail(_purge_blocked(plan.problems),
                            fix="fix what is named above, then: briefcase-ai engine uninstall --purge", code=1)
        token, code = _authorize(args, "Go ahead?", "briefcase-ai engine uninstall --yes")
        if token is None:
            return code
        outcome, note = _remove_install(plan.version, purge, force_wd, token, shown_daemon=plan.daemon)
    if outcome == "gone":
        return _ux.fail(note or "", fix="briefcase-ai engine status", code=1)
    if outcome == "blocked":
        return _ux.fail(note or "", fix="fix what is named above, then: briefcase-ai engine uninstall"
                        + (" --purge" if purge else ""), code=1)
    if plan.record:
        print(f"engine {plan.version} removed.")
    else:
        print("engine volumes, secrets and certificates removed.")
    if note:
        print(note)
    if plan.record:
        _ux.print_next("briefcase-ai engine install", "to install it again")
    return 0


# --------------------------------------------------------------------------- status row


def status_row() -> Dict[str, Any]:
    """The engine's ``briefcase-ai status`` row. Reads installed.json and probes the local gRPC port
    (100 ms). Reported by ``env_commands.status()`` so it is listed under Capture: a hook row in
    the "start" group would sort ahead of the core rows that ``briefcase-ai status --json`` promises
    come first."""
    from briefcase import engine as _engine

    record = _engine.current_install()
    remote = _engine.configured_address()
    if record is None and not remote:
        # `next` stays None: an engine that is not installed must not take over the
        # suggestion every briefcase user sees.
        return {"id": "engine", "title": "Engine (oci-jj)", "state": "off",
                "detail": _engine.NOT_INSTALLED, "next": None}
    addr = _engine.client_settings()["addr"]
    reachable = port_open(addr, timeout=0.1)
    if record is not None and not reachable and not remote:
        return {"id": "engine", "title": "Engine (oci-jj)", "state": "attention",
                "detail": f"engine {record['version']} installed, not running",
                "next": "briefcase-ai engine up", "data": {"version": record["version"], "addr": addr}}
    detail = (f"engine {record['version']} at {addr}" if record and not remote else f"using the engine at {addr}")
    if not reachable:
        detail += " (not reachable)"
    return {"id": "engine", "title": "Engine (oci-jj)", "state": "on" if reachable else "attention",
            "detail": detail, "next": None if reachable else "briefcase-ai engine doctor",
            "data": {"version": (record or {}).get("version"), "addr": addr}}
