"""``briefcase dataset`` / ``secret`` / ``run``: the evaluation-job lifecycle commands.

Parser wiring only; the handlers live in ``briefcase.cli.commands``.
"""
from __future__ import annotations

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
    # Local dataset versions built from captured records (from-records, show,
    # diff, export, import) live beside the engine registry commands.
    from briefcase.cli.dataset_versions import extend_dataset_parser

    extend_dataset_parser(ds)

    # secret
    sec = sub.add_parser("secret", help="Store and list secrets.").add_subparsers(dest="sub")
    sec_set = sec.add_parser("set", help="Store a secret.")
    sec_set.add_argument(
        "assignment", metavar="KEY[=VALUE]",
        help="A bare KEY reads the value from stdin (getpass on a tty), keeping it out of argv.",
    )
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
