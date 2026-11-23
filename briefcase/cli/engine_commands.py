"""``briefcase engine``: install and run the Briefcase engine (oci-jj) on this machine.

    briefcase engine install --code bci_...     # the one command the portal shows you
    briefcase engine up
    briefcase engine status

The engine is proprietary and is not in this package. Entitled accounts get it from the gated
portal (https://observe.briefcasebrain.io/engine): either the one-command install with a
single-use install code, a licence token for automation, or an offline bundle. Everything is
verified against the release key pinned in this SDK (``briefcase/engine/keys``) before it is
installed. Every other subcommand works on that local install. ``briefcase stack`` is the
deprecated name for ``up``/``down``/``status``/``logs``.

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
                    "token or offline bundle), verify it, then start, check and upgrade it. "
                    "Replaces `briefcase stack`.",
        epilog="example:\n  briefcase engine install --code bci_...   "
               "# from https://observe.briefcasebrain.io/engine\n\n" + ENV_HELP,
        formatter_class=_RAW,
    )
    sp = p.add_subparsers(dest="engine_sub", metavar="<subcommand>")

    inst = _sub(sp, "install", "Install the engine: one-command code, licence token, or offline bundle.",
                "briefcase engine install --code bci_...   # copy it from https://observe.briefcasebrain.io/engine")
    _install_flags(inst)
    inst.set_defaults(func=cmd_install)

    upg = _sub(sp, "upgrade", "Install the newest engine release and restart onto it.",
               "briefcase engine upgrade --token bcl_...")
    _install_flags(upg)
    upg.set_defaults(func=cmd_upgrade)

    ver = _sub(sp, "verify", "Verify a downloaded engine file against the release key pinned in this SDK.",
               "briefcase engine verify engine-images-0.4.0-x86_64.tar --manifest engine-manifest.json")
    ver.add_argument("file", help="The downloaded file (binary, bundle, engine-manifest.json or index.json).")
    ver.add_argument("--signature", metavar="FILE", help="Its signature (default FILE.sig).")
    ver.add_argument("--manifest", metavar="FILE",
                     help="engine-manifest.json to check FILE's sha256 and size against (its .sig next to it).")
    _ux.add_json_flag(ver)
    ver.set_defaults(func=cmd_verify)

    up = _sub(sp, "up", "Start the installed engine (docker compose up).", "briefcase engine up")
    up.add_argument("--wait", action="store_true", help="Wait until every service is healthy.")
    up.set_defaults(func=cmd_up)

    down = _sub(sp, "down", "Stop the engine. Data is kept unless --volumes.", "briefcase engine down")
    down.add_argument("--volumes", action="store_true", help="Also delete the engine's data (asks first).")
    down.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    down.set_defaults(func=cmd_down)

    st = _sub(sp, "status", "Show what is installed, the key it was verified with, and whether it runs.",
              "briefcase engine status --json")
    st.add_argument("--token", metavar="T",
                    help="Licence token to check for a newer release (default $BRIEFCASE_LICENSE_TOKEN).")
    _ux.add_json_flag(st)
    st.set_defaults(func=cmd_status)

    logs = _sub(sp, "logs", "Show engine service logs.", "briefcase engine logs oci-jj-server -f")
    logs.add_argument("service", nargs="?", help="One service (default: all).")
    logs.add_argument("-f", "--follow", action="store_true", help="Stream logs.")
    logs.add_argument("--tail", default="200", help="Lines per service (default 200).")
    logs.set_defaults(func=cmd_logs)

    doc = _sub(sp, "doctor", "Check Docker, the install, its signature, the images and the gRPC endpoint.",
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
    p.add_argument("--tls", action="store_true",
                   help="Serve gRPC over TLS with a self-signed certificate (needs openssl).")
    p.add_argument("--no-images", action="store_true",
                   help="Only the oci-bai client binary; no Docker images (for an engine run elsewhere).")
    p.add_argument("--dry-run", dest="engine_dry_run", action="store_true",
                   help="Show the plan; change nothing (an install code is not redeemed).")
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
                         "briefcase engine install --bundle engine-images-<version>-<arch>.tar "
                         "--manifest engine-manifest.json")
        if code or token_flag:
            raise _Usage("--bundle installs offline; it takes no --code or --token",
                         "briefcase engine install --bundle FILE --manifest FILE")
        return _inst.InstallOptions(platform_url=platform, bundle=Path(bundle), manifest=Path(manifest),
                                    signature=Path(args.signature) if getattr(args, "signature", None) else None,
                                    binary=Path(args.binary) if getattr(args, "binary", None) else None,
                                    tls=bool(getattr(args, "tls", False)),
                                    skip_images=bool(getattr(args, "no_images", False)))
    if code and token_flag:
        raise _Usage("give either --code or --token, not both", "briefcase engine install --code bci_...")
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
                         PORTAL_HINT.format(portal=portal) + " (briefcase engine install --code bci_...)")
        if token.startswith("bci_"):
            raise _Usage("that is an install code, not a licence token",
                         f"briefcase engine install --code {token[:8]}...")
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

    errors = (_inst.InstallError, _trust.TrustError, _bundle.BundleError)
    as_json = bool(getattr(args, "json", False))
    try:
        opts = _options(args)
    except _Usage as exc:
        return _ux.fail(str(exc.args[0]), fix=exc.args[1], code=2)
    previous = _engine.current_install()
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
    if answer is None:
        return _ux.fail("installing the engine needs confirmation",
                        fix="re-run with --yes (or --dry-run to only look)", code=2)
    if not answer:
        print("cancelled; nothing was changed"
              + (" and the install code was not used." if opts.mode == "code" else "."))
        return 1

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
            fix = "briefcase engine install --token ...   # re-run; verified downloads are reused"
        elif opts.mode == "bundle" and fix is None:
            fix = "briefcase engine verify " + str(opts.bundle) + " --manifest " + str(opts.manifest)
        return _ux.fail(str(exc), fix=fix)
    except RuntimeError as exc:  # --tls without openssl
        return _ux.fail(str(exc), fix="install openssl, or install without --tls")
    if upgrading and previous and previous.get("version") != record["version"] and record.get("compose") \
            and previous.get("compose") and _running(previous):
        log(f"restarting onto {record['version']} (data volumes are kept)...")
        rc = _rc(_run(_compose(record, "up", "-d", "--remove-orphans")))
        if rc != 0:
            return _ux.fail("the new engine did not start", fix="briefcase engine logs")
    if as_json:
        return _ux.print_json({"installed": record})
    where = Path(record["compose"]).parent if record.get("compose") else Path(record["manifest"]).parent
    print(f"engine {record['version']} installed in {where} (verified with key {record['key_id']})")
    if record.get("binary"):
        print(f"  client: {record['binary']}")
    _ux.print_next("briefcase engine status" if upgrading else "briefcase engine up")
    return 0


def _rerun(opts) -> str:
    if opts.mode == "bundle":
        return f"briefcase engine install --bundle {opts.bundle} --manifest {opts.manifest} --yes"
    if opts.mode == "code":
        return f"briefcase engine install --code {opts.code} --yes"
    return "briefcase engine install --yes   # with BRIEFCASE_LICENSE_TOKEN set"


def cmd_install(args, store, engine, stack) -> int:
    return _install(args, upgrading=False)


def cmd_upgrade(args, store, engine, stack) -> int:
    if _require_local(args) is None:
        return 3
    return _install(args, upgrading=True)


# --------------------------------------------------------------------------- verify


def cmd_verify(args, store, engine, stack) -> int:
    from briefcase.engine import install as _inst
    from briefcase.engine import trust as _trust

    as_json = bool(getattr(args, "json", False))
    path = Path(args.file)
    if not path.is_file():
        return _ux.fail(f"{path} does not exist", fix="briefcase engine verify PATH/TO/FILE", code=2)
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
        else:
            if not sig_path.is_file():
                return _ux.fail(f"no signature for {path.name} ({sig_path} does not exist)",
                                fix=f"briefcase engine verify {path} --signature FILE.sig   "
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
    _ux.print_next("briefcase engine install --bundle FILE --manifest engine-manifest.json",
                   "install a verified offline bundle")
    return 0


def _running(record: Dict[str, Any]) -> bool:
    res = _run(_compose(record, "ps", "--status", "running", "--quiet"), capture_output=True, text=True)
    return res is not None and _rc(res) == 0 and bool((getattr(res, "stdout", "") or "").strip())


# --------------------------------------------------------------------------- up / down / logs


def _require_stack(args) -> Optional[Dict[str, Any]]:
    record = _require_local(args)
    if record is not None and not record.get("compose"):
        _ux.fail(f"engine {record['version']} was installed without images (--no-images); there is no "
                 "local stack to run", fix="briefcase engine install --code bci_...   # without --no-images")
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
        return "briefcase engine install --bundle FILE --manifest engine-manifest.json --yes"
    if record.get("source") == "token":
        return "briefcase engine install --yes   # with BRIEFCASE_LICENSE_TOKEN set"
    portal = (record.get("platform_url") or "https://observe.briefcasebrain.io").rstrip("/") + "/engine"
    return f"get a new install code at {portal} and run the command it shows"


def cmd_up(args, store, engine, stack) -> int:
    record = _require_stack(args)
    if record is None:
        return 3 if _local_install() is None else 1
    drift = image_drift(record)
    if drift:
        for line in drift:
            print(f"  {line}")
        return _ux.fail("an engine image changed since it was verified at install; not starting it",
                        fix=_reinstall_fix(record))
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
    record = _require_stack(args)
    if record is None:
        return 3 if _local_install() is None else 1
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


def _services(record: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
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
        _ux.print_next("briefcase engine upgrade", "a newer engine release is available")
    elif record is not None and record.get("compose") and not state["reachable"]:
        _ux.print_next("briefcase engine up")
    else:
        _ux.print_next("briefcase graph log <ref>", "look at what the engine has recorded")
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
                       else (FAIL, "client binary", f"missing: {binary} (re-run briefcase engine install)"))
        env_file = Path(record.get("env_file", ""))
        if record.get("compose"):
            if not env_file.exists():
                out.append((FAIL, "secrets", f"missing: {env_file} (re-run briefcase engine install)"))
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
        out.append((WARN, "gRPC", f"{settings['addr']} not reachable (briefcase engine up)"))
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
    version_dir = Path(record.get("manifest") or record.get("compose") or "").parent
    steps = []
    if record.get("compose"):
        steps.append(f"docker compose down{' -v (deletes the engine data volumes)' if purge else ''}")
    steps += [f"docker image rm {ref}" for ref in images]
    steps.append(f"delete {version_dir}")
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
    if record.get("compose"):
        _run(_compose(record, "down", *(["-v"] if purge else [])))
    docker = _install_mod.Docker(_runner())
    for ref in images:
        docker.remove_image(ref)
    if version_dir.name == record["version"]:
        shutil.rmtree(version_dir, ignore_errors=True)
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
