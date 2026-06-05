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
        except Exception:
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


def cmd_doctor(args, store, engine, stack=None) -> int:
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
    rc = summarize(results, strict=getattr(args, "strict", False))
    if rc != 0:
        print("\ndoctor found problems above.", file=sys.stderr)
    return rc
