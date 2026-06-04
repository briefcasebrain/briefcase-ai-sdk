"""Adapter from the Briefcase-native lifecycle to the real ``oci-jj`` CLI.

The verbs of the ``briefcase`` façade delegate to existing ``oci-jj`` subcommands (see
``rust/crates/oci-jj-cli/src/main.rs``): a *run* is an ``oci-jj attach-bench … --run`` verdict gate
(or ``oci-jj hunt``) whose scorecard is read back with ``oci-jj diff --depth bench``.

Argv construction is **pure** (the ``argv_*`` methods) and separated from the single side-effecting
``exec`` so the flag mapping is unit-tested without a live stack. The subprocess runner is injected.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from briefcase.cli.calls import ArgvCall


class OciJJEngine:
    def __init__(self, binary=None, server=None, repo=None, runner=None) -> None:
        self.binary = binary or os.environ.get("BRIEFCASE_OCIJJ_BIN", "oci-jj")
        self.server = server or os.environ.get("OCI_JJ_SERVER", "http://127.0.0.1:50051")
        self.repo = repo or os.environ.get("OCI_JJ_REPO", "rl-gym-env")
        self.runner = runner or subprocess.run

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
        argv = call.argv if isinstance(call, ArgvCall) else call
        merged = {**os.environ, **(env or {})}
        return self.runner(list(argv), env=merged)
