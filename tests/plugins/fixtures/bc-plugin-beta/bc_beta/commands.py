"""Tries to replace the built-in `briefcase-ai status`; the built-in wins."""


def register(subparsers) -> None:
    p = subparsers.add_parser("status", help="not used")
    p.set_defaults(func=lambda *a: 3)
