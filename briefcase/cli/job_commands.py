"""``briefcase-ai dataset`` / ``secret`` / ``run``: the evaluation-job lifecycle commands.

Parser wiring only; the handlers live in ``briefcase.cli.commands``.
"""
from __future__ import annotations

import argparse

from briefcase.cli import commands

GROUP = "evaluate"


def register(sub) -> None:
    # dataset
    ds = sub.add_parser(
        "dataset",
        help="Evaluation datasets: version captured records, register engine datasets.",
    ).add_subparsers(dest="sub")
    ds_reg = ds.add_parser("register", help="Register a dataset by name.")
    ds_reg.add_argument("name")
    ds_reg.add_argument("--uri", required=True, help="e.g. synthetic://xor, file://…, s3://…")
    ds_reg.set_defaults(func=commands.cmd_dataset_register)
    ds.add_parser("list", help="List registered datasets.").set_defaults(func=commands.cmd_dataset_list)
    from briefcase import engine as _engine

    pub = ds.add_parser(
        "publish",
        help=_engine.help_text("Upload a .parquet/.csv to the local engine's dataset bucket and register it."),
        description="Uploads the file into the local engine's MinIO under the tenant prefix the engine "
                    "requires (s3://oci-jj-datasets/tenants/<tenant>/datasets/<sha256>/<file>) and registers "
                    "NAME -> that URI. The engine presigns it for the verdict worker per job. Engine call: "
                    "none (docker compose exec minio mc pipe); the URI is checked by the engine on run submit.",
        epilog="example:\n  briefcase-ai dataset publish churn ./churn.parquet\n\n"
               "exit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pub.add_argument("name")
    pub.add_argument("path", help="A .parquet or .csv file with a `target` column.")
    pub.add_argument("--dry-run", dest="dataset_dry_run", action="store_true", help="Show the plan only.")
    pub.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    pub.set_defaults(func=commands.cmd_dataset_publish)
    # Local dataset versions built from captured records (from-records, show,
    # diff, export, import) live beside the engine registry commands.
    from briefcase.cli.dataset_versions import extend_dataset_parser

    extend_dataset_parser(ds)

    # secret
    sec = sub.add_parser(
        "secret",
        help="Store engine credentials (OCI_JJ_AUTH_TOKEN); other keys never leave this machine.",
        description="Stores values under ~/.briefcase (mode 0600). The engine uses one of them: "
                    "OCI_JJ_AUTH_TOKEN, sent as the gRPC bearer token. It has no channel that carries "
                    "secrets or env vars into a run, so any other key is kept locally and never sent.",
    ).add_subparsers(dest="sub")
    sec_set = sec.add_parser("set", help="Store a credential, e.g. OCI_JJ_AUTH_TOKEN.")
    sec_set.add_argument(
        "assignment", metavar="KEY[=VALUE]",
        help="A bare KEY reads the value from stdin (getpass on a tty), keeping it out of argv.",
    )
    sec_set.set_defaults(func=commands.cmd_secret_set)
    sec_list = sec.add_parser("list", help="List stored keys and whether the engine uses them (values never shown).")
    sec_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    sec_list.set_defaults(func=commands.cmd_secret_list)

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
    submit.add_argument(
        "--secret", action="append", metavar="KEY[=VALUE]",
        help="Inject a secret; a bare KEY takes its value from $KEY in the caller's environment.",
    )
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

    attach = run.add_parser(
        "attach",
        help=_engine.help_text("Record a verdict.json computed elsewhere against a run or ref."),
        description="Sends the verdict (and optional fold-contract manifest) to the engine, which records "
                    "it as a bench summary plus a benchmarked_against edge. Engine call: "
                    "VcsService.AttachBench with run=false (oci-bai attach-bench --result).",
        epilog="example:\n  briefcase-ai run attach demo --result verdict.json\n\n"
               "exit codes: 0 ok, 1 failed, 2 usage, 3 engine not installed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    attach.add_argument("id", help="Run name (its repository and checkpoint are reused).")
    attach.add_argument("--result", required=True, metavar="VERDICT.json", help="verdictml's audit record.")
    attach.add_argument("--manifest", metavar="MANIFEST.json", help="The repro fold-contract manifest.")
    attach.add_argument("--repository", help="Candidate ref, when ID is not a submitted run.")
    attach.add_argument("--checkpoint", help="Baseline ref (default: the run's, else the first parent).")
    attach.set_defaults(func=commands.cmd_run_attach)
