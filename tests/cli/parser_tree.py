"""Dump an argparse parser as plain data, for comparing the CLI surface across refactors.

``parser_tree(build_parser())`` maps each command path (``""`` for the root, ``"run submit"``...)
to its arguments and ``set_defaults`` values. Help text is left out on purpose: wording may
change, names and flags may not.

Regenerate the baseline (only when a command is deliberately changed)::

    python -m tests.cli.parser_tree > tests/cli/fixtures/parser_tree_baseline.json
"""
from __future__ import annotations

import argparse
import json
import sys


def _plain(value):
    if callable(value):
        return f"{getattr(value, '__module__', '?')}.{getattr(value, '__qualname__', repr(value))}"
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _action(action) -> dict:
    return {
        "flags": list(action.option_strings),
        "dest": action.dest,
        "action": type(action).__name__,
        "nargs": _plain(action.nargs),
        "const": _plain(action.const),
        "default": _plain(action.default),
        "type": _plain(action.type),
        "choices": None if action.choices is None else sorted(map(str, action.choices)),
        "required": bool(action.required),
        "metavar": _plain(action.metavar),
    }


def parser_tree(parser: argparse.ArgumentParser, path: str = "") -> dict:
    tree = {}
    arguments, children = [], {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            children = dict(action.choices)
            continue
        if isinstance(action, argparse._HelpAction):
            continue
        arguments.append(_action(action))
    tree[path] = {
        "arguments": sorted(arguments, key=lambda a: (a["flags"], a["dest"])),
        "defaults": {key: _plain(value) for key, value in sorted(parser._defaults.items())},
        "add_help": bool(parser.add_help),
        "subcommands": sorted(children),
    }
    for name, child in children.items():
        tree.update(parser_tree(child, f"{path} {name}".strip()))
    return tree


def files_under(root) -> list:
    """Relative paths of every file below ``root`` (what a command left behind)."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


if __name__ == "__main__":
    from briefcase.cli.__main__ import build_parser

    json.dump(parser_tree(build_parser()), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
