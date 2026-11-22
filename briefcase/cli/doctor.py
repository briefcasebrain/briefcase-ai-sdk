"""``briefcase doctor`` — an install/compat preflight for the evaluation-run stack.

Runs ordered checks (Docker → bundled compose → compat → image pull access → server reflection + the
structural API floor) and prints the resolved compatibility matrix. Docker calls go through an injected
``runner`` and reflection through an injected ``probe`` so the whole thing unit-tests with fakes — no
Docker, no live server. Exit 0 on ok/expected-warn, 1 on a broken install / floor violation; ``--strict``
escalates warnings to a non-zero exit for CI.
"""
from __future__ import annotations

import os
import subprocess
import sys

from briefcase.cli.stack import compose_path, load_compat

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}
_MARK = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]"}


def _default_probe(server):
    """Reflected service names from a live server (the same path GrpcEngine uses)."""
    from briefcase.cli.grpc_engine import ReflectionTransport

    return ReflectionTransport(server, timeout=3.0).list_services()


class Doctor:
    def __init__(self, runner=None, compat=None, probe=None) -> None:
        self.runner = runner or subprocess.run
        self._compat = compat
        self._probe = probe if probe is not None else _default_probe

    def compat(self) -> dict:
        if self._compat is None:
            self._compat = load_compat()
        return self._compat

    def _run(self, argv):
        try:
            return self.runner(argv, capture_output=True, text=True)
        except FileNotFoundError:
            return None
        except TypeError:  # a fake runner that doesn't accept capture_output/text
            return self.runner(argv)

    def checks(self, server="http://127.0.0.1:50051"):
        """Return a list of ``(severity, label, detail)`` tuples."""
        out = []

        res = self._run(["docker", "version", "--format", "{{.Server.Version}}"])
        if res is None or getattr(res, "returncode", 1) != 0:
            out.append((FAIL, "docker", "not found or daemon not running"))
        else:
            out.append((OK, "docker", (getattr(res, "stdout", "") or "").strip()))

        try:
            with compose_path() as p:
                text = p.read_text()
            if "image: ghcr.io/briefcasebrain/oci-jj-server" in text and "\n    build:" not in text:
                out.append((OK, "bundled compose", "pinned, image-based"))
            else:
                out.append((FAIL, "bundled compose", "not image-based"))
        except Exception as e:
            out.append((FAIL, "bundled compose", f"unreadable ({e})"))

        try:
            compat = self.compat()
            out.append((OK, "compat.json", f"briefcase_ai {compat.get('briefcase_ai')}"))
        except Exception as e:
            out.append((FAIL, "compat.json", str(e)))
            compat = {}

        tag = compat.get("oci_jj_image_tag", "")
        private = compat.get("private_preview", False)
        for img in compat.get("oci_jj_images", []):
            ref = f"{img}:{tag}"
            res = self._run(["docker", "manifest", "inspect", ref])
            if res is not None and getattr(res, "returncode", 1) == 0:
                out.append((OK, "image", ref))
            elif private:
                out.append((WARN, "image", f"{ref} private preview — run: docker login ghcr.io"))
            else:
                out.append((WARN, "image", f"{ref} not pullable yet"))

        scorer = compat.get("oci_jj_scorer_image")
        if scorer:
            ref = f"{scorer}:{tag}"
            res = self._run(["docker", "manifest", "inspect", ref])
            if res is not None and getattr(res, "returncode", 1) == 0:
                out.append((OK, "scorer image", ref))
            else:
                out.append((WARN, "scorer image", f"{ref} private preview (scoring) — docker login ghcr.io"))

        floor = compat.get("oci_jj_api_min", "v1")
        try:
            services = self._probe(server)
            if any(s.startswith(f"oci_jj.{floor}.") for s in services):
                out.append((OK, "server", f"{server} (oci_jj.{floor} reflection)"))
            else:
                out.append((FAIL, "server", f"{server} exposes no oci_jj.{floor}.* service"))
        except Exception as e:
            if "UNAUTHENTICATED" in str(e) or "requires a bearer token" in str(e):
                # Reachable but secured (oci-jj OCI_JJ_AUTH_TOKENS): not a "stack down" problem.
                out.append((FAIL, "server", f"{server} requires a bearer token; set OCI_JJ_AUTH_TOKEN"))
            else:
                out.append((WARN, "server", f"unreachable at {server}; is the stack up? (briefcase stack up)"))

        return out


def summarize(results, strict=False) -> int:
    worst = OK
    for sev, _, _ in results:
        if _RANK[sev] > _RANK[worst]:
            worst = sev
    if worst == FAIL:
        return 1
    if worst == WARN and strict:
        return 1
    return 0


def connect_checks(adapters=None, probe=None):
    """The ``--connect`` section: per connected tool, are our keys still present, is the tool's
    version in the supported range, do managed settings override us, and is the receiver up."""
    from briefcase.connect import engine as connect_engine
    from briefcase.receiver import daemon

    results = connect_engine.doctor_checks(adapters, probe=probe or daemon.probe)
    try:  # the enforcement hooks, when `briefcase policy` is in use
        from briefcase.cli import policy_commands
        results = list(results) + list(policy_commands.doctor_checks())
    except Exception as exc:
        results = list(results) + [(WARN, "policy", f"could not be checked: {exc}")]
    return results + conformance_checks(adapters)


def conformance_checks(adapters=None, report=None):
    """Last known conformance level per installed or connected tool (shipped ``conformance-report.json``)
    next to the version found here, so a tool newer than anything tested is flagged."""
    from briefcase.connect import conformance, engine as connect_engine
    from briefcase.connect.adapters import registry

    adapters = adapters or registry()
    connected = {row["tool"] for row in connect_engine.status(adapters) if row["connected"]}
    installed = [(tool_id, adapter.name, adapter.version()) for tool_id, adapter in adapters.items()
                 if tool_id in connected or adapter.binary()]
    return conformance.rows(installed, report)


def cmd_doctor_connect(args) -> int:
    results = connect_checks()
    print("briefcase connect integrations:")
    for sev, label, detail in results:
        print(f"{_MARK[sev]} {label}: {detail}" if detail else f"{_MARK[sev]} {label}")
    rc = summarize(results, strict=getattr(args, "strict", False))
    if rc != 0:
        print("\ndoctor found problems above.", file=sys.stderr)
    return rc


def plugin_checks(report=None):
    """One ``(severity, label, detail)`` per installed plugin entry point. Each is loaded, so a
    plugin that fails to import, has the wrong type or declares an unsupported plugin API is a
    warning naming its package. Empty when no package declares a plugin."""
    from briefcase import plugins

    rows = report if report is not None else [p.to_dict() for p in plugins.report(load_them=True)]
    out = []
    for row in rows:
        label = f"plugin {row['group'][len('briefcase.'):]}:{row['name']}"
        origin = f"{row['distribution'] or '?'} {row['version'] or ''}".rstrip()
        if row["state"] in ("loaded", "available"):
            out.append((OK, label, origin))
        elif row["state"] in ("disabled", "shadowed"):
            out.append((OK, label, f"{origin}; {row['state']}: {row['reason']}"))
        else:
            out.append((WARN, label, f"{origin}; {row['state']}: {row['reason']}"))
    return out


def _print_results(results) -> None:
    for sev, label, detail in results:
        print(f"{_MARK[sev]} {label}: {detail}" if detail else f"{_MARK[sev]} {label}")


def cmd_doctor_plugins(args) -> int:
    from briefcase import plugins

    results = plugin_checks()
    print(f"briefcase plugins (plugin API {plugins.PLUGIN_API_VERSION}):")
    if not plugins.enabled():
        print(f"  turned off by {plugins.ENV_SWITCH}=off")
    if not results:
        print("  no installed package declares a briefcase plugin")
    _print_results(results)
    rc = summarize(results, strict=getattr(args, "strict", False))
    if rc != 0:
        print("\ndoctor found problems above.", file=sys.stderr)
    return rc


def cmd_doctor(args, store, engine, stack=None) -> int:
    if getattr(args, "connect", False):
        return cmd_doctor_connect(args)
    if getattr(args, "plugins", False):
        return cmd_doctor_plugins(args)
    server = getattr(args, "server", None) or os.environ.get("OCI_JJ_SERVER", "http://127.0.0.1:50051")
    doctor = Doctor()
    results = doctor.checks(server=server)
    for sev, label, detail in results:
        print(f"{_MARK[sev]} {label}: {detail}" if detail else f"{_MARK[sev]} {label}")
    try:
        compat = load_compat()
        print("\nresolved compatibility matrix:")
        for key in ("briefcase_ai", "oci_jj_api_min", "oci_jj_image_tag", "verdictml"):
            print(f"  {key}: {compat.get(key)}")
        if compat.get("private_preview"):
            print("  engine images: private preview — docker login ghcr.io to pull the stack")
    except Exception:
        pass
    # The plugins section appears only when a package declares a briefcase plugin, so the
    # output above is unchanged on a machine without plugins.
    try:
        extra = plugin_checks()
    except Exception as exc:  # noqa: BLE001 - doctor must finish
        extra = [(WARN, "plugins", f"could not be listed: {exc}")]
    if extra:
        print("\nplugins:")
        _print_results(extra)
        results = list(results) + extra
    rc = summarize(results, strict=getattr(args, "strict", False))
    if rc != 0:
        print("\ndoctor found problems above.", file=sys.stderr)
    return rc
