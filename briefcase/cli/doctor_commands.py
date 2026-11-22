"""``briefcase doctor``: parser wiring for the preflight in ``briefcase.cli.doctor``."""
from __future__ import annotations

from briefcase.cli import doctor

GROUP = "start"
ORDER = 40


def register(sub) -> None:
    # Preflight: Docker, image pull access, server reflection, and the resolved compat matrix.
    doc = sub.add_parser("doctor", help="Diagnose the install and print the compatibility matrix.")
    doc.add_argument("--strict", action="store_true", help="Treat warnings as failures (for CI).")
    doc.add_argument(
        "--connect", action="store_true",
        help="Check the `briefcase connect` integrations instead of the evaluation stack.",
    )
    doc.set_defaults(func=doctor.cmd_doctor)
