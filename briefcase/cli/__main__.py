"""``briefcase`` CLI entry point: ``briefcase <command> ...`` (run ``python -m briefcase.cli``).

A Briefcase-native job lifecycle:
``dataset`` / ``secret`` / ``run {submit,list,inspect,logs,results,stop,delete}``.
"""
from __future__ import annotations

import argparse
import os
import sys

from briefcase.cli import commands, doctor, stack_commands
from briefcase.cli.engine import OciJJEngine
from briefcase.cli.state import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="briefcase",
        description="Submit and monitor Briefcase evaluation runs (oci-jj + verdictml).",
    )
    parser.add_argument("--server", help="oci-jj-server gRPC endpoint (default $OCI_JJ_SERVER).")
    parser.add_argument("--repo", help="oci-jj repository / image family (default $OCI_JJ_REPO).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolved oci-jj command for a run submit instead of executing it.",
    )
    sub = parser.add_subparsers(dest="command")

    # dataset
    ds = sub.add_parser("dataset", help="Register and list datasets.").add_subparsers(dest="sub")
    ds_reg = ds.add_parser("register", help="Register a dataset by name.")
    ds_reg.add_argument("name")
    ds_reg.add_argument("--uri", required=True, help="e.g. synthetic://xor, file://…, s3://…")
    ds_reg.set_defaults(func=commands.cmd_dataset_register)
    ds.add_parser("list", help="List registered datasets.").set_defaults(func=commands.cmd_dataset_list)

    # secret
    sec = sub.add_parser("secret", help="Store and list secrets.").add_subparsers(dest="sub")
    sec_set = sec.add_parser("set", help="Store a secret as KEY=VALUE.")
    sec_set.add_argument("assignment", metavar="KEY=VALUE")
    sec_set.set_defaults(func=commands.cmd_secret_set)
    sec.add_parser("list", help="List secret keys (values never shown).").set_defaults(
        func=commands.cmd_secret_list
    )

    # run
    run = sub.add_parser("run", help="Submit and manage evaluation runs.").add_subparsers(dest="sub")
    submit = run.add_parser("submit", help="Submit a verdict run (candidate vs checkpoint).")
    submit.add_argument("name", help="Job name — the run's handle.")
    submit.add_argument("--repository", required=True, help="Candidate environment ref/family.")
    submit.add_argument("--revision", help="Pin a specific commit/ref.")
    submit.add_argument("--dataset", required=True, help="Registered dataset name.")
    submit.add_argument("--checkpoint", help="Baseline ref to score against.")
    submit.add_argument("--metric", default="f1", help="Verdict metric (default f1).")
    submit.add_argument("--mode", choices=("gate", "hunt"), default="gate")
    submit.add_argument("--secret", action="append", metavar="KEY=VALUE", help="Inject a secret.")
    submit.add_argument("--env", action="append", metavar="KEY=VALUE", help="Set an env var.")
    submit.set_defaults(func=commands.cmd_run_submit)

    run.add_parser("list", help="List submitted runs.").set_defaults(func=commands.cmd_run_list)
    for name, func, follow in (
        ("inspect", commands.cmd_run_inspect, False),
        ("logs", commands.cmd_run_logs, True),
        ("results", commands.cmd_run_results, False),
        ("stop", commands.cmd_run_stop, False),
        ("delete", commands.cmd_run_delete, False),
    ):
        p = run.add_parser(name, help=f"{name} a run.")
        p.add_argument("id", help="Run name.")
        if follow:
            p.add_argument("-f", "--follow", action="store_true", help="Stream logs.")
        p.set_defaults(func=func)

    # stack — bring up the bundled, pinned engine images (no oci-jj checkout, no Rust).
    st = sub.add_parser("stack", help="Manage the bundled engine stack (docker compose).").add_subparsers(
        dest="sub"
    )
    st_up = st.add_parser("up", help="Pull and start the pinned engine images.")
    st_up.add_argument(
        "--no-scorer", action="store_true",
        help="Skip the private verdict-worker (runs enqueue but are not scored).",
    )
    st_up.set_defaults(func=stack_commands.cmd_stack_up)
    st_down = st.add_parser("down", help="Stop the stack.")
    st_down.add_argument("--volumes", action="store_true", help="Also remove volumes (destroys data).")
    st_down.set_defaults(func=stack_commands.cmd_stack_down)
    st.add_parser("status", help="Show stack status.").set_defaults(func=stack_commands.cmd_stack_status)
    st_logs = st.add_parser("logs", help="Tail stack logs.")
    st_logs.add_argument("service", nargs="?", help="Service name (default: all services).")
    st_logs.add_argument("-f", "--follow", action="store_true", help="Stream logs.")
    st_logs.set_defaults(func=stack_commands.cmd_stack_logs)

    # doctor — preflight: Docker, image pull access, server reflection, and the resolved compat matrix.
    doc = sub.add_parser("doctor", help="Diagnose the install and print the compatibility matrix.")
    doc.add_argument("--strict", action="store_true", help="Treat warnings as failures (for CI).")
    doc.set_defaults(func=doctor.cmd_doctor)

    return parser


def make_engine(args, *, env=None):
    """Select the engine from ``$BRIEFCASE_ENGINE`` (``grpc`` | ``cli`` | ``auto``, default ``auto``).

    ``cli`` → the subprocess ``OciJJEngine``. ``grpc`` → ``GrpcEngine`` (fails loudly if the server is
    unreachable — an explicit choice). ``auto`` → prefer ``GrpcEngine`` (gRPC reflection), falling back
    to the ``oci-jj`` binary only if reflection is unavailable *and* the binary is on PATH.
    """
    env = env if env is not None else os.environ
    mode = env.get("BRIEFCASE_ENGINE", "auto").lower()
    server, repo = getattr(args, "server", None), getattr(args, "repo", None)
    if mode == "cli":
        return OciJJEngine(server=server, repo=repo)

    from briefcase.cli.grpc_engine import GrpcEngine, grpc_available

    if mode == "grpc":
        return GrpcEngine(server=server, repo=repo)
    # auto: prefer gRPC when its optional [run] deps are installed (reflection resolves lazily on the
    # first real exec, with a clear "is the stack up?" error if the server is unreachable). Otherwise
    # use the subprocess oci-jj engine. Construction is I/O-free either way, so --dry-run stays offline.
    if grpc_available():
        return GrpcEngine(server=server, repo=repo)
    return OciJJEngine(server=server, repo=repo)


class _LazyEngine:
    """Defer engine construction until a handler first touches it, so engine-free commands
    (``dataset``/``secret``/``run list``…) never open a gRPC channel or shell out."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._engine = None

    def _resolve(self):
        if self._engine is None:
            self._engine = self._factory()
        return self._engine

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


def main(argv=None, store=None, engine=None, stack=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    store = store or Store()
    engine = engine or _LazyEngine(lambda: make_engine(args))
    stack = stack or stack_commands.Stack()
    return args.func(args, store, engine, stack)


if __name__ == "__main__":
    sys.exit(main())
