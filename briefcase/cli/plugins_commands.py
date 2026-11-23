"""``briefcase-ai plugins``: what installed packages add to briefcase, and whether it works.

    briefcase-ai plugins                 # same as `plugins list`
    briefcase-ai plugins list --json
    briefcase-ai plugins show acme       # one plugin (NAME or GROUP:NAME)

Plugins are packages that declare ``briefcase.*`` entry points (see ``briefcase/plugins.py``
and ``docs/features/plugins.md``). Listing them imports each one, so the state shown
(loaded / skipped / refused / disabled / shadowed) is what your application would get.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

from briefcase.cli import _ux

GROUP = "connect"
ORDER = 90

_EPILOG = """example:
  briefcase-ai plugins list

env:
  BRIEFCASE_PLUGINS=off               load no plugin at all
  BRIEFCASE_PLUGINS_DISABLE=a,b       skip these (NAME, GROUP:NAME or distribution name)
"""

_STATE_STYLE = {"loaded": "green", "available": "green", "skipped": "red", "refused": "red",
                "disabled": "dim", "shadowed": "yellow"}


def register(sub) -> None:
    p = sub.add_parser(
        "plugins",
        help="List the exporters, backends, commands and other plugins installed packages add.",
        description="Show every plugin installed packages declare (entry point groups "
                    "briefcase.exporters, .bitemporal_backends, .vcs_providers, .receiver_sources, "
                    ".connect_adapters, .guardrails, .cli_commands and .features), the package and "
                    "version it comes from, and whether it loaded.",
        epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ux.add_json_flag(p)
    p.set_defaults(func=cmd_plugins_list)
    psub = p.add_subparsers(dest="plugins_command", metavar="<subcommand>")
    lst = psub.add_parser("list", help="Every plugin, by group, with its status.",
                          epilog="example:\n  briefcase-ai plugins list --json",
                          formatter_class=argparse.RawDescriptionHelpFormatter)
    _ux.add_json_flag(lst)
    lst.set_defaults(func=cmd_plugins_list)
    show = psub.add_parser("show", help="One plugin: where it comes from and why it is or is not used.",
                           epilog="example:\n  briefcase-ai plugins show briefcase.exporters:acme",
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    show.add_argument("name", metavar="NAME", help="Plugin name, or GROUP:NAME when a name is in two groups.")
    _ux.add_json_flag(show)
    show.set_defaults(func=cmd_plugins_show)


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    from briefcase import plugins

    return {
        "plugin_api_version": plugins.PLUGIN_API_VERSION,
        "enabled": plugins.enabled(),
        "disabled_names": plugins.disabled_names(),
        "count": len(rows),
        "plugins": rows,
    }


def collect() -> Dict[str, Any]:
    from briefcase import plugins

    return _summary([plugin.to_dict() for plugin in plugins.report(load_them=True)])


def cmd_plugins_list(args, store=None, engine=None, stack=None) -> int:
    from briefcase import plugins

    data = collect()
    if getattr(args, "json", False):
        return _ux.print_json(data)
    rows = data["plugins"]
    print(_ux.style("briefcase-ai plugins", "bold") + f" · plugin API {plugins.PLUGIN_API_VERSION}"
          + f" · {len(rows)} entry point" + ("" if len(rows) == 1 else "s"))
    if not plugins.enabled():
        print(f"Plugins are turned off ({plugins.ENV_SWITCH}=off); nothing below is loaded.")
    if not rows:
        print("")
        print("No installed package declares a briefcase plugin.")
        print("")
        _ux.print_next("briefcase-ai features plugins", "how to write one")
        return 0
    for group in plugins.GROUPS:
        items = [row for row in rows if row["group"] == group]
        if not items:
            continue
        print("")
        print(_ux.style(group, "bold") + _ux.style(f"  ({plugins.GROUPS[group]})", "dim"))
        for row in items:
            state = _ux.style(row["state"], _STATE_STYLE.get(row["state"], "dim"))
            origin = f"{row['distribution']} {row['version']}" if row["distribution"] else "?"
            print(f"  {row['name']:<20} {state:<8}  {origin}")
            if row["reason"] and row["state"] != "loaded":
                print(f"  {'':<20} {row['reason']}")
    print("")
    broken = [row for row in rows if row["state"] in ("skipped", "refused")]
    if broken:
        _ux.print_next(f"briefcase-ai plugins show {broken[0]['group']}:{broken[0]['name']}", "see why it is not used")
    else:
        _ux.print_next("briefcase-ai features", "plugin features are listed with the built-in ones")
    return 0


def _matches(row: Dict[str, Any], name: str) -> bool:
    if ":" in name:
        group, _, short = name.rpartition(":")
        group = group if group.startswith("briefcase.") else f"briefcase.{group}"
        return row["group"] == group and row["name"] == short
    return row["name"] == name or (row["distribution"] or "").lower() == name.lower()


def cmd_plugins_show(args, store=None, engine=None, stack=None) -> int:
    from briefcase import plugins

    data = collect()
    found = [row for row in data["plugins"] if _matches(row, args.name)]
    if not found:
        return _ux.fail(f"no installed plugin named {args.name!r}", fix="briefcase-ai plugins list",
                        code=_ux.EXIT_USAGE)
    if getattr(args, "json", False):
        return _ux.print_json({"plugin_api_version": plugins.PLUGIN_API_VERSION, "plugins": found})
    for row in found:
        print(_ux.style(f"{row['group']}:{row['name']}", "bold"))
        print(f"  Package:     {row['distribution'] or '?'} {row['version'] or ''}".rstrip())
        print(f"  Entry point: {row['value']}")
        print(f"  Provides:    {plugins.GROUPS.get(row['group'], '')}")
        print(f"  State:       {row['state']}" + (f" ({row['reason']})" if row["reason"] else ""))
        api = row["api"]
        print(f"  Plugin API:  {api if api is not None else 'not declared (read as 1)'}; "
              f"this briefcase-ai supports {plugins.MIN_PLUGIN_API}..{plugins.PLUGIN_API_VERSION}")
        print("")
    _ux.print_next("briefcase-ai plugins list")
    return 0


def status() -> Optional[Dict[str, Any]]:
    """Metadata only: counts entry points without importing any plugin."""
    from briefcase import plugins

    if not plugins.enabled():
        return {"id": "plugins", "state": "off", "detail": f"turned off by {plugins.ENV_SWITCH}=off",
                "next": None}
    found = plugins.all_plugins()
    if not found:
        return None  # nothing installed: no row, so a fresh machine's status stays short
    dists = sorted({plugin.dist or "?" for plugin in found})
    return {"id": "plugins", "state": "on",
            "detail": f"{len(found)} entry point" + ("" if len(found) == 1 else "s")
                      + f" from {', '.join(dists)}",
            "next": None, "data": {"distributions": dists}}
