"""briefcase.cli_commands: `briefcase-ai alpha hello --name you`."""

GROUP = "review"   # read by `briefcase-ai plugins`; plugin commands are listed under "Plugins"
BRIEFCASE_PLUGIN_API = 1


def register(subparsers) -> None:
    p = subparsers.add_parser("alpha", help="Say hello from the alpha plugin.")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="alpha_command")
    hello = sub.add_parser("hello", help="Print a greeting.")
    hello.add_argument("--name", default="world")
    hello.set_defaults(func=cmd_hello)


def cmd_hello(args, store, engine, stack) -> int:
    print(f"hello, {args.name} (from bc-plugin-alpha)")
    print("Next: briefcase-ai plugins show alpha")
    return 0


def status():
    return {"id": "alpha", "state": "on", "detail": "alpha plugin is installed", "next": None}
