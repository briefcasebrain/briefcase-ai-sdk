"""``briefcase engine``: install and run the Briefcase engine (oci-jj) on this machine.

    briefcase engine install --platform-url https://YOUR-PLATFORM   # needs BRIEFCASE_LICENSE_TOKEN
    briefcase engine up
    briefcase engine status

The engine is proprietary and is not in this package; ``install`` fetches it with an account
licence token (created on the platform's API Keys page). Every other subcommand works on that
local install. ``briefcase stack`` is the deprecated name for ``up``/``down``/``status``/``logs``.

Exit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from briefcase.cli import _ux

GROUP = "start"
ORDER = 45

_RAW = argparse.RawDescriptionHelpFormatter
ENV_HELP = (
    "environment:\n"
    "  BRIEFCASE_LICENSE_TOKEN   engine licence token (platform: API Keys page)\n"
    "  BRIEFCASE_PLATFORM_URL    the Briefcase platform that issued it\n"
    "  BRIEFCASE_ENGINE_ADDR     use an engine running elsewhere instead of a local install\n"
    "exit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed"
)


def _sub(sp, name: str, help_text: str, example: str):
    return sp.add_parser(name, help=help_text, description=help_text,
                         epilog=f"example:\n  {example}\n\n{ENV_HELP}", formatter_class=_RAW)


def register(sub) -> None:
    from briefcase import engine as _engine

    p = sub.add_parser(
        "engine",
        help=_engine.help_text("Install and run the Briefcase engine (oci-jj) locally."),
        description="Install the engine with your account licence token, then start, check and "
                    "upgrade it. Replaces `briefcase stack`.",
        epilog="example:\n  briefcase engine install --platform-url https://YOUR-PLATFORM\n\n" + ENV_HELP,
        formatter_class=_RAW,
    )
    sp = p.add_subparsers(dest="engine_sub", metavar="<subcommand>")

    inst = _sub(sp, "install", "Download the engine with your licence token and pull its images.",
                "BRIEFCASE_LICENSE_TOKEN=bcl_... briefcase engine install --platform-url https://YOUR-PLATFORM")
    _install_flags(inst)
    inst.set_defaults(func=cmd_install)

    upg = _sub(sp, "upgrade", "Install a newer engine release and restart onto it.",
               "briefcase engine upgrade --version 0.3.0")
    _install_flags(upg)
    upg.set_defaults(func=cmd_upgrade)

    up = _sub(sp, "up", "Start the installed engine (docker compose up).", "briefcase engine up")
    up.add_argument("--wait", action="store_true", help="Wait until every service is healthy.")
    up.set_defaults(func=cmd_up)

    down = _sub(sp, "down", "Stop the engine. Data is kept unless --volumes.", "briefcase engine down")
    down.add_argument("--volumes", action="store_true", help="Also delete the engine's data (asks first).")
    down.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    down.set_defaults(func=cmd_down)

    st = _sub(sp, "status", "Show what is installed and whether it is running.", "briefcase engine status --json")
    _ux.add_json_flag(st)
    st.set_defaults(func=cmd_status)

    logs = _sub(sp, "logs", "Show engine service logs.", "briefcase engine logs oci-jj-server -f")
    logs.add_argument("service", nargs="?", help="One service (default: all).")
    logs.add_argument("-f", "--follow", action="store_true", help="Stream logs.")
    logs.add_argument("--tail", default="200", help="Lines per service (default 200).")
    logs.set_defaults(func=cmd_logs)

    doc = _sub(sp, "doctor", "Check Docker, the install, the images and the engine's gRPC endpoint.",
               "briefcase engine doctor")
    _ux.add_json_flag(doc)
    doc.set_defaults(func=cmd_doctor)

    un = _sub(sp, "uninstall", "Remove the engine install (and with --purge, its data and secrets).",
              "briefcase engine uninstall --dry-run")
    un.add_argument("--purge", action="store_true", help="Also delete data volumes, secrets and certificates.")
    un.add_argument("--dry-run", dest="engine_dry_run", action="store_true", help="Show the plan; change nothing.")
    un.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    un.set_defaults(func=cmd_uninstall)


def _install_flags(p) -> None:
    p.add_argument("--version", dest="engine_version", metavar="X", help="Engine release (default: latest).")
    p.add_argument("--token", metavar="T", help="Licence token (default $BRIEFCASE_LICENSE_TOKEN).")
    p.add_argument("--platform-url", metavar="URL", help="Platform URL (default $BRIEFCASE_PLATFORM_URL).")
    p.add_argument("--tls", action="store_true",
                   help="Serve gRPC over TLS with a self-signed certificate (needs openssl).")
    p.add_argument("--no-images", action="store_true",
                   help="Only the oci-bai client binary; no Docker images (for an engine run elsewhere).")
    p.add_argument("--dry-run", dest="engine_dry_run", action="store_true",
                   help="Ask the platform and show the plan; download and write nothing.")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    _ux.add_json_flag(p)


# --------------------------------------------------------------------------- helpers


def _dry(args) -> bool:
    return bool(getattr(args, "engine_dry_run", False) or getattr(args, "dry_run", False))


def _runner():
    return subprocess.run


def _local_install() -> Optional[Dict[str, Any]]:
    from briefcase import engine as _engine

    return _engine.current_install()


def _require_local(args) -> Optional[Dict[str, Any]]:
    record = _local_install()
    if record is None:
        from briefcase import engine as _engine

        _engine.not_installed(bool(getattr(args, "json", False)))
    return record


def _compose(record: Dict[str, Any], *args: str) -> List[str]:
    from briefcase.engine import compose

    return compose.compose_argv(Path(record["compose"]), Path(record["env_file"]), *args)


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


def _install(args, upgrading: bool) -> int:
    from briefcase import engine as _engine
    from briefcase.engine import install as _install_mod

    as_json = bool(getattr(args, "json", False))
    token = getattr(args, "token", None) or os.environ.get(_engine.ENV_LICENSE)
    platform = getattr(args, "platform_url", None) or os.environ.get(_engine.ENV_PLATFORM)
    previous = _engine.current_install()
    if not platform and previous:
        platform = previous.get("platform_url")
    if not platform:
        return _ux.fail("no platform URL: the engine is installed through your Briefcase platform account",
                        fix="briefcase engine install --platform-url https://YOUR-PLATFORM   "
                            "# or export BRIEFCASE_PLATFORM_URL", code=2)
    if not token:
        return _ux.fail("no licence token",
                        fix=f"create an engine licence token at {platform.rstrip('/')}/settings/api-keys, then "
                            "export BRIEFCASE_LICENSE_TOKEN=bcl_...", code=2)
    target = _install_mod.host_target()
    skip_images = bool(getattr(args, "no_images", False))
    try:
        if not skip_images and _install_mod.Docker(_runner()).available() is None:
            raise _install_mod.InstallError(
                "Docker is not installed or its daemon is not running",
                fix="install Docker (https://docs.docker.com/get-docker/), start it, then re-run "
                    "(or --no-images for the client binary only)")
        creds = _install_mod.request_credentials(platform, token, getattr(args, "engine_version", None), target)
        plan = _install_mod.make_plan(creds, target, skip_images=skip_images)
    except _install_mod.InstallError as exc:
        return _ux.fail(str(exc), fix=exc.fix)
    if previous and previous.get("version") == plan.version and not upgrading and \
            Path(previous.get("binary", "")).exists():
        note = f"engine {plan.version} is already installed; re-checking files and images"
    else:
        note = None
    if as_json and _dry(args):
        return _ux.print_json({"dry_run": True, "plan": plan.to_dict()})
    if not as_json:
        print(f"{'Upgrade to' if upgrading else 'Install'} engine {plan.version} ({plan.target['os']}/"
              f"{plan.target['arch']}) from {platform.rstrip('/')}:")
        for step in plan.steps:
            print(f"  - {step}")
        if note:
            print(note)
    if _dry(args):
        print("dry run: nothing was downloaded or written.")
        _ux.print_next("briefcase engine install --yes", "to do it")
        return 0
    if not skip_images:
        answer = _ux.confirm("Download, pull the images and write the files above?",
                             assume_yes=bool(getattr(args, "yes", False)))
        if answer is None:
            return _ux.fail("this pulls Docker images and needs confirmation",
                            fix="re-run with --yes (or --dry-run to only look)", code=2)
        if not answer:
            print("cancelled; nothing was changed.")
            return 1
    try:
        record = _install_mod.install(
            _install_mod.InstallOptions(token=token, platform_url=platform, version=plan.version,
                                        tls=bool(getattr(args, "tls", False)), skip_images=skip_images),
            creds=creds, runner=_runner(), log=(lambda line: None) if as_json else print)
    except _install_mod.InstallError as exc:
        return _ux.fail(str(exc), fix=exc.fix)
    except RuntimeError as exc:  # --tls without openssl
        return _ux.fail(str(exc), fix="install openssl, or install without --tls")
    if upgrading and previous and previous.get("version") != record["version"] and _running(previous):
        print(f"restarting onto {record['version']} (data volumes are kept)...")
        rc = _rc(_run(_compose(record, "up", "-d", "--remove-orphans")))
        if rc != 0:
            return _ux.fail("the new engine did not start", fix="briefcase engine logs")
    if as_json:
        return _ux.print_json({"installed": record})
    print(f"engine {record['version']} installed in {Path(record['compose']).parent}")
    print(f"  client: {record['binary']}  ({record['signature']})")
    _ux.print_next("briefcase engine status" if upgrading else "briefcase engine up")
    return 0


def cmd_install(args, store, engine, stack) -> int:
    return _install(args, upgrading=False)


def cmd_upgrade(args, store, engine, stack) -> int:
    if _require_local(args) is None:
        return 3
    return _install(args, upgrading=True)


def _running(record: Dict[str, Any]) -> bool:
    res = _run(_compose(record, "ps", "--status", "running", "--quiet"), capture_output=True, text=True)
    return res is not None and _rc(res) == 0 and bool((getattr(res, "stdout", "") or "").strip())


# --------------------------------------------------------------------------- up / down / logs


def cmd_up(args, store, engine, stack) -> int:
    record = _require_local(args)
    if record is None:
        return 3
    argv = _compose(record, "up", "-d", "--remove-orphans")
    if getattr(args, "wait", False):
        argv += ["--wait", "--wait-timeout", "300"]
    rc = _rc(_run(argv))
    if rc != 0:
        return _ux.fail("docker compose up failed (see above)", fix="briefcase engine doctor")
    print(f"engine {record['version']} started: gRPC {record['grpc_addr']}, registry gateway {record['gateway']}")
    _ux.print_next("briefcase engine status")
    return 0


def cmd_down(args, store, engine, stack) -> int:
    record = _require_local(args)
    if record is None:
        return 3
    argv = ["down"]
    if getattr(args, "volumes", False):
        answer = _ux.confirm("Delete the engine's data volumes (database, blobs, registry)?", default=False,
                             assume_yes=bool(getattr(args, "yes", False)))
        if not answer:
            return _ux.fail("not deleting data without confirmation", fix="briefcase engine down --volumes --yes",
                            code=2 if answer is None else 1)
        argv.append("-v")
    rc = _rc(_run(_compose(record, *argv)))
    if rc != 0:
        return _ux.fail("docker compose down failed (see above)", fix="briefcase engine doctor")
    _ux.print_next("briefcase engine up", "to start it again")
    return 0


def cmd_logs(args, store, engine, stack) -> int:
    record = _require_local(args)
    if record is None:
        return 3
    argv = ["logs", "--tail", str(getattr(args, "tail", "200"))]
    if getattr(args, "follow", False):
        argv.append("-f")
    if getattr(args, "service", None):
        argv.append(args.service)
    return _rc(_run(_compose(record, *argv)))


# --------------------------------------------------------------------------- status / doctor


def _services(record: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
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
    return [{"service": r.get("Service") or r.get("Name"), "state": r.get("State"), "health": r.get("Health") or ""}
            for r in rows if isinstance(r, dict)]


def engine_state() -> Dict[str, Any]:
    from briefcase import engine as _engine

    record = _engine.current_install()
    settings = _engine.client_settings()
    remote = _engine.configured_address()
    return {
        "installed": record is not None,
        "version": (record or {}).get("version"),
        "install": record,
        "addr": settings["addr"],
        "addr_source": settings["source"],
        "remote": remote,
        "reachable": port_open(settings["addr"]),
    }


def cmd_status(args, store, engine, stack) -> int:
    from briefcase import engine as _engine

    state = engine_state()
    record = state["install"]
    if record is not None:
        state["services"] = _services(record)
    if getattr(args, "json", False):
        return _ux.print_json(state)
    if record is None and not state["remote"]:
        print(_engine.NOT_INSTALLED)
        return _engine.EXIT_NOT_INSTALLED
    if record is not None:
        print(f"engine {record['version']} installed {record.get('installed_at', '')}")
        print(f"  client binary: {record['binary']} ({record.get('signature', 'unknown')})")
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
    print(f"  gRPC:          {state['addr']} ({state['addr_source']}) "
          f"{'reachable' if state['reachable'] else 'not reachable'}")
    if record is not None and not state["reachable"]:
        _ux.print_next("briefcase engine up")
    else:
        _ux.print_next("briefcase graph log <ref>", "look at what the engine has recorded")
    return 0


def doctor_checks(record: Optional[Dict[str, Any]], client_factory=None) -> List[tuple]:
    from briefcase import engine as _engine
    from briefcase.cli.doctor import FAIL, OK, WARN
    from briefcase.engine import install as _install_mod

    out: List[tuple] = []
    docker = _install_mod.Docker(_runner())
    version = docker.available()
    out.append((OK, "docker", version) if version else (FAIL, "docker", "not found or daemon not running"))
    if version:
        res = _run(["docker", "compose", "version", "--short"], capture_output=True, text=True)
        out.append((OK, "docker compose", (getattr(res, "stdout", "") or "").strip())
                   if _rc(res) == 0 else (FAIL, "docker compose", "the compose plugin is missing"))
    if record is None:
        remote = _engine.configured_address()
        out.append((WARN if remote else FAIL, "install",
                    f"no local install; using {remote}" if remote else _engine.NOT_INSTALLED))
    else:
        out.append((OK, "install", f"engine {record['version']} ({record.get('signature', 'unknown')})"))
        binary = Path(record.get("binary", ""))
        out.append((OK, "client binary", str(binary)) if binary.exists() and os.access(binary, os.X_OK)
                   else (FAIL, "client binary", f"missing: {binary} (re-run briefcase engine install)"))
        env_file = Path(record.get("env_file", ""))
        if not env_file.exists():
            out.append((FAIL, "secrets", f"missing: {env_file} (re-run briefcase engine install)"))
        elif os.name == "posix" and env_file.stat().st_mode & 0o077:
            out.append((WARN, "secrets", f"{env_file} is readable by others; chmod 600 it"))
        else:
            out.append((OK, "secrets", str(env_file)))
        if version:
            for name, ref in sorted((record.get("images") or {}).items()):
                out.append((OK, f"image {name}", ref) if docker.has_image(ref)
                           else (FAIL, f"image {name}", f"{ref} not pulled (briefcase engine install)"))
    settings = _engine.client_settings()
    if not port_open(settings["addr"]):
        out.append((WARN, "gRPC", f"{settings['addr']} not reachable (briefcase engine up)"))
        return out
    try:
        from briefcase.engine.client import EngineClient, EngineError

        client = (client_factory or EngineClient)()
        try:
            # WhoAmI: any valid token may call it; it names the tenant, role and registry namespace.
            me = client.call("WhoAmI", {})
            namespace = me.get("registry_namespace") or "?"
            out.append((OK, "gRPC", f"{settings['addr']}: tenant {me.get('tenant') or '-'}, role "
                                    f"{me.get('role') or '-'}, push to <gateway>/{namespace}/<image>"))
        except EngineError as exc:
            if exc.code == "UNIMPLEMENTED":  # an engine older than WhoAmI: any answer proves auth works
                try:
                    client.call("ListRefs", {"repo": client.repo})
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
        _ux.print_next("briefcase engine status")
    return rc


# --------------------------------------------------------------------------- uninstall


def cmd_uninstall(args, store, engine, stack) -> int:
    from briefcase import engine as _engine
    from briefcase.engine import install as _install_mod

    record = _require_local(args)
    if record is None:
        return 3
    purge = bool(getattr(args, "purge", False))
    images = sorted(set((record.get("images") or {}).values()))
    steps = [f"docker compose down{' -v (deletes the engine data volumes)' if purge else ''}"]
    steps += [f"docker image rm {ref}" for ref in images]
    steps.append(f"delete {Path(record['compose']).parent}")
    if purge:
        steps.append(f"delete {_engine.secrets_path()} and {_engine.engine_dir() / 'tls'}")
    print(f"Uninstall engine {record['version']}:")
    for step in steps:
        print(f"  - {step}")
    if _dry(args):
        print("dry run: nothing was changed.")
        return 0
    answer = _ux.confirm("Go ahead?", default=False, assume_yes=bool(getattr(args, "yes", False)))
    if not answer:
        return _ux.fail("not uninstalling without confirmation", fix="briefcase engine uninstall --yes",
                        code=2 if answer is None else 1)
    _run(_compose(record, "down", *(["-v"] if purge else [])))
    docker = _install_mod.Docker(_runner())
    for ref in images:
        docker.remove_image(ref)
    shutil.rmtree(Path(record["compose"]).parent, ignore_errors=True)
    state = _engine.load_state()
    state.get("installs", {}).pop(record["version"], None)
    remaining = sorted(state.get("installs", {}))
    state["current"] = remaining[-1] if remaining else None
    if purge:
        for path in (_engine.secrets_path(), _engine.engine_dir() / "tls"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
    if state.get("installs"):
        _install_mod.save_state(state)
    else:
        try:
            _engine.installed_path().unlink()
        except FileNotFoundError:
            pass
    print(f"engine {record['version']} removed.")
    _ux.print_next("briefcase engine install", "to install it again")
    return 0


# --------------------------------------------------------------------------- status row


def status_row() -> Dict[str, Any]:
    """The engine's ``briefcase status`` row. Reads installed.json and probes the local gRPC port
    (100 ms). Reported by ``env_commands.status()`` so it is listed under Capture: a hook row in
    the "start" group would sort ahead of the core rows that ``briefcase status --json`` promises
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
                "next": "briefcase engine up", "data": {"version": record["version"], "addr": addr}}
    detail = (f"engine {record['version']} at {addr}" if record and not remote else f"using the engine at {addr}")
    if not reachable:
        detail += " (not reachable)"
    return {"id": "engine", "title": "Engine (oci-jj)", "state": "on" if reachable else "attention",
            "detail": detail, "next": None if reachable else "briefcase engine doctor",
            "data": {"version": (record or {}).get("version"), "addr": addr}}
