"""Adapter from the Briefcase-native lifecycle to the real ``oci-jj`` CLI.

The verbs of the ``briefcase-ai`` façade delegate to existing ``oci-jj`` subcommands (see
``rust/crates/oci-jj-cli/src/main.rs``): a *run* is an ``oci-jj attach-bench … --run`` verdict gate
(or ``oci-jj hunt``) whose scorecard is read back with ``oci-jj diff --depth bench``.

Argv construction is **pure** (the ``argv_*`` methods) and separated from the single side-effecting
``exec`` so the flag mapping is unit-tested without a live stack. The subprocess runner is injected.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence

from briefcase.cli.calls import ArgvCall


# oci-jj's CLI crate builds a binary named ``oci-bai`` (rust/crates/oci-jj-cli/Cargo.toml); older
# builds and docs call it ``oci-jj``. Prefer whichever is on PATH, newest name first.
BINARY_NAMES = ("oci-bai", "oci-jj")


def default_binary(which=shutil.which) -> str:
    for name in BINARY_NAMES:
        if which(name):
            return name
    return BINARY_NAMES[0]


# Where oci-bai lives in the engine's server image (oci-jj docker/rust.Dockerfile, read at 9e8f05a:
# `COPY --from=build .../oci-bai /usr/local/bin/oci-bai`).
CONTAINER_BINARY = "/usr/local/bin/oci-bai"
# `docker compose --project-name briefcase-engine` puts the stack on this network.
COMPOSE_NETWORK = "briefcase-engine_default"
CONTAINER_CA = "/run/briefcase/ca.crt"
# Flags whose value is a host path; other host paths must be absolute or ./-relative.
_PATH_FLAGS = ("--result", "--manifest", "--dataset")
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


class OciJJEngine:
    def __init__(self, binary=None, server=None, repo=None, runner=None) -> None:
        from briefcase.engine import client_settings, current_install

        record = current_install() or {}
        installed_bin = record.get("binary")
        if installed_bin and not os.path.exists(installed_bin):
            installed_bin = None
        explicit = binary or os.environ.get("BRIEFCASE_OCIJJ_BIN")
        client = record.get("client") or {}
        # A release with no oci-bai build for this OS (e.g. macOS): run the one in the server image.
        self.container = client if (not explicit and not installed_bin and client.get("mode") == "container"
                                    and client.get("image")) else None
        self.binary = explicit or installed_bin or ("oci-bai" if self.container else default_binary())
        settings = client_settings(server=server)
        self.server = settings["addr"]
        self._local = settings["source"] == "local install"
        self._ca = settings["ca"]
        # oci-bai reads OCI_JJ_AUTH_TOKEN / OCI_JJ_TLS_CA itself; a local install supplies them.
        self._client_env = {k: v for k, v in (("OCI_JJ_AUTH_TOKEN", settings["token"]),
                                              ("OCI_JJ_TLS_CA", settings["ca"])) if v}
        self.repo = repo or os.environ.get("OCI_JJ_REPO", "rl-gym-env")
        self.runner = runner or subprocess.run

    # ---- container mode ----
    def _container_server(self) -> tuple:
        """(server address as seen from the container, extra docker run flags)."""
        if self._local:
            # Join the stack's network and use the service name (in the --tls certificate's SANs).
            scheme = "https" if self.server.startswith("https://") else "http"
            return f"{scheme}://oci-jj-server:50051", ["--network", COMPOSE_NETWORK]
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.server if "://" in self.server else "http://" + self.server)
        host = parts.hostname or ""
        if host in _LOOPBACK:
            netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
            return (urlunsplit(parts._replace(netloc=netloc)),
                    ["--add-host", "host.docker.internal:host-gateway"])
        return self.server, []

    def container_argv(self, argv: Sequence[str]) -> list[str]:
        """``docker run --rm`` of the pinned server image's oci-bai for a logical oci-bai argv.
        Host paths (absolute, ``./``-relative, or the value of ``--result``/``--manifest``/``--dataset``)
        that exist are bind-mounted at the same absolute path (read-only;
        ``--dest`` read-write); the token reaches the container by name only (``-e NAME``), never
        in argv."""
        assert self.container is not None
        rest = list(argv[1:])
        server, net = self._container_server()
        mounts: list[str] = []
        seen = set()
        out_args: list[str] = []
        prev = None
        for arg in rest:
            if prev == "--server":
                arg = server
            elif prev == "--dest":
                arg = os.path.abspath(arg)
                os.makedirs(arg, exist_ok=True)
                if arg not in seen:
                    seen.add(arg)
                    mounts += ["-v", f"{arg}:{arg}"]
            elif (prev in _PATH_FLAGS or os.path.isabs(arg) or arg.startswith(("./", "../"))) \
                    and os.path.exists(arg):
                arg = os.path.abspath(arg)
                if arg not in seen:
                    seen.add(arg)
                    mounts += ["-v", f"{arg}:{arg}:ro"]
            out_args.append(arg)
            prev = arg if arg.startswith("--") else None
        # The verified image ID, not the tag (which can be re-pointed after install), and never a pull.
        run = ["docker", "run", "--rm", "-i", "--pull", "never", *net]
        if hasattr(os, "getuid"):
            run += ["--user", f"{os.getuid()}:{os.getgid()}"]
        run += ["-e", "OCI_JJ_AUTH_TOKEN"]
        if self._ca:
            run += ["-v", f"{os.path.abspath(self._ca)}:{CONTAINER_CA}:ro", "-e", f"OCI_JJ_TLS_CA={CONTAINER_CA}"]
        run += mounts + ["--entrypoint", CONTAINER_BINARY, self.container.get("image_id") or self.container["image"]]
        return run + out_args

    def command(self, argv: Sequence[str]) -> list[str]:
        """The argv that really runs (``docker run ...`` in container mode)."""
        argv = list(argv)
        if self.container is not None and argv[:1] == [self.binary]:
            return self.container_argv(argv)
        return argv

    def _base(self) -> list[str]:
        # --server/--repo are oci-jj global flags (clap global=true), valid before the subcommand.
        return [self.binary, "--server", self.server, "--repo", self.repo]

    # ---- pure argv builders ----
    def argv_attach_bench(self, candidate: str, baseline, dataset_uri: str, metric: str = "f1") -> list[str]:
        argv = self._base() + ["attach-bench", candidate, "--run"]
        if baseline:
            argv += ["--baseline", baseline]
        argv += ["--dataset", dataset_uri, "--metric", metric]
        return argv

    def argv_attach_result(self, candidate: str, baseline, result_path: str, manifest_path=None) -> list[str]:
        """``oci-bai attach-bench REF --result verdict.json``: record a verdict computed elsewhere."""
        argv = self._base() + ["attach-bench", candidate, "--result", result_path]
        if manifest_path:
            argv += ["--manifest", manifest_path]
        if baseline:
            argv += ["--baseline", baseline]
        return argv

    def argv_hunt(self, candidate: str, dataset_uri: str, base_cols=None, tag_prefix=None) -> list[str]:
        argv = self._base() + ["hunt", candidate, "--dataset", dataset_uri]
        if base_cols:
            argv += ["--base-cols", base_cols]
        if tag_prefix:
            argv += ["--tag-prefix", tag_prefix]
        return argv

    def argv_diff_bench(self, baseline: str, candidate: str) -> list[str]:
        return self._base() + ["diff", baseline, candidate, "--depth", "bench"]

    def argv_log(self, family: str) -> list[str]:
        return self._base() + ["log", family]

    def argv_provenance(self, ref: str) -> list[str]:
        return self._base() + ["provenance", ref]

    def argv_checkout(self, ref: str, paths: Sequence[str] = (), dest=None) -> list[str]:
        argv = self._base() + ["checkout", ref]
        if paths:
            argv += ["--paths", ",".join(paths)]
        if dest:
            argv += ["--dest", dest]
        return argv

    # ---- call builders (Call seam — wrap the pure argv_* so commands.py is transport-neutral) ----
    def call_attach_bench(self, candidate: str, baseline, dataset_uri: str, metric: str = "f1") -> ArgvCall:
        return ArgvCall(self.argv_attach_bench(candidate, baseline, dataset_uri, metric))

    def call_attach_result(self, candidate: str, baseline, result_path: str, manifest_path=None) -> ArgvCall:
        return ArgvCall(self.argv_attach_result(candidate, baseline, result_path, manifest_path))

    def call_hunt(self, candidate: str, dataset_uri: str, base_cols=None, tag_prefix=None) -> ArgvCall:
        return ArgvCall(self.argv_hunt(candidate, dataset_uri, base_cols, tag_prefix))

    def call_diff_bench(self, baseline: str, candidate: str) -> ArgvCall:
        return ArgvCall(self.argv_diff_bench(baseline, candidate))

    def call_provenance(self, ref: str) -> ArgvCall:
        return ArgvCall(self.argv_provenance(ref))

    def call_log(self, family: str) -> ArgvCall:
        return ArgvCall(self.argv_log(family))

    # ---- the single side effect ----
    def exec(self, call, env: dict | None = None):
        # Accept a Call (the new seam) or a bare argv list (back-compat: cmd_run_logs' docker argv,
        # and the existing exec tests). Either way the injected runner sees a plain argv.
        argv = self.command(call.argv if isinstance(call, ArgvCall) else call)
        merged = {**getattr(self, "_client_env", {}), **os.environ, **(env or {})}
        return self.runner(list(argv), env=merged)
