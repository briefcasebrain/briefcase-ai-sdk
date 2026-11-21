"""``briefcase prompt``: list, show and diff versions of named prompt templates.

Versions are recorded by ``briefcase.prompts.prompt(name, template)`` in
``$BRIEFCASE_HOME/prompts`` (local only):

    briefcase prompt list
    briefcase prompt show triage@3f2a9c01b7d4
    briefcase prompt diff triage@3f2a9c01b7d4 triage@9c1d0e22a6f3
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from briefcase import prompts

GROUP = "review"
_RAW = argparse.RawDescriptionHelpFormatter


def register(sub: argparse._SubParsersAction) -> None:
    if "prompt" in sub.choices:
        return
    parser = sub.add_parser(
        "prompt", help="List, show and diff prompt template versions.", formatter_class=_RAW,
        description="Prompt templates registered with briefcase.prompts.prompt(name, template). Each version "
                    "is the first 12 hex characters of the template's SHA-256.",
        epilog="example:\n  briefcase prompt list",
    )
    ps = parser.add_subparsers(dest="sub")
    p = ps.add_parser("list", help="Every prompt name with its versions.", formatter_class=_RAW,
                      epilog="example:\n  briefcase prompt list --json")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_prompt_list)
    p = ps.add_parser("show", help="Print one version's template.", formatter_class=_RAW,
                      epilog="example:\n  briefcase prompt show triage")
    p.add_argument("ref", metavar="NAME[@VERSION]", help="Latest version when @VERSION is omitted; a prefix works.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_prompt_show)
    p = ps.add_parser("diff", help="Unified diff between two versions.", formatter_class=_RAW,
                      epilog="example:\n  briefcase prompt diff triage@3f2a9c01b7d4 triage@9c1d0e22a6f3")
    p.add_argument("a", metavar="NAME@V1")
    p.add_argument("b", metavar="NAME@V2", nargs="?",
                   help="Default: the latest version. A bare version means the same name.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_prompt_diff)


def _home(store: Any) -> Optional[Path]:
    home = getattr(store, "home", None)
    return Path(home) if home is not None else None


def _resolve(saved: Dict[str, List[dict]], ref: str) -> Tuple[Optional[dict], str]:
    name, _, version = ref.partition("@")
    entries = saved.get(name)
    if not entries:
        known = ", ".join(sorted(saved)) or "none yet"
        return None, (f"no prompt named {name!r} (known: {known}). Register one in code: "
                      f"briefcase.prompts.prompt({name!r}, template)")
    if not version:
        return entries[-1], ""
    hits = [e for e in entries if str(e.get("version", "")).startswith(version)]
    if len(hits) == 1:
        return hits[0], ""
    versions = ", ".join(str(e.get("version")) for e in entries)
    what = "is ambiguous" if hits else "does not exist"
    return None, f"version {version!r} of {name} {what} (versions: {versions})"


def cmd_prompt_list(args, store, engine=None, stack=None) -> int:
    saved = prompts.load_saved(_home(store))
    if args.json:
        print(json.dumps({name: [{k: e.get(k) for k in ("version", "first_seen", "tampered")} for e in entries]
                          for name, entries in saved.items()}, indent=2))
        return 0
    if not saved:
        print("no prompts recorded yet")
        print("Next: in your code, TRIAGE = briefcase.prompts.prompt(\"triage\", \"...{text}\") "
              "and use TRIAGE.render(text=...)")
        return 0
    width = max(len(n) for n in saved)
    print(f"  {'name'.ljust(width)}  versions  latest        first seen")
    for name, entries in saved.items():
        latest = entries[-1]
        flag = "  (modified on disk)" if any(e.get("tampered") for e in entries) else ""
        print(f"  {name.ljust(width)}  {len(entries):<8}  {latest['version']}  "
              f"{str(latest.get('first_seen', ''))[:19]}{flag}")
    first = next(iter(saved))
    print(f"Next: briefcase prompt show {first}")
    return 0


def cmd_prompt_show(args, store, engine=None, stack=None) -> int:
    saved = prompts.load_saved(_home(store))
    entry, error = _resolve(saved, args.ref)
    if entry is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0
    name = entry["name"]
    print(f"{name}@{entry['version']}  first seen {entry.get('first_seen')}  "
          f"{len(entry['template'])} chars" + ("  MODIFIED ON DISK (hash mismatch)" if entry.get("tampered") else ""))
    placeholders = prompts.Prompt(name, entry["template"]).placeholders
    if placeholders:
        print(f"placeholders: {', '.join(placeholders)}")
    print("---")
    print(entry["template"])
    print("---")
    print(f"Next: briefcase dataset from-records runs.jsonl --name {name} --where prompt_name={name}")
    return 0


def cmd_prompt_diff(args, store, engine=None, stack=None) -> int:
    saved = prompts.load_saved(_home(store))
    name = args.a.partition("@")[0]
    b_ref = args.b or name
    if "@" not in b_ref and b_ref != name:
        b_ref = f"{name}@{b_ref}"
    a, err_a = _resolve(saved, args.a)
    b, err_b = _resolve(saved, b_ref)
    if a is None or b is None:
        print(f"error: {err_a or err_b}", file=sys.stderr)
        return 2
    lines = list(difflib.unified_diff(
        a["template"].splitlines(), b["template"].splitlines(),
        fromfile=f"{a['name']}@{a['version']}", tofile=f"{b['name']}@{b['version']}", lineterm=""))
    if args.json:
        print(json.dumps({"from": f"{a['name']}@{a['version']}", "to": f"{b['name']}@{b['version']}",
                          "identical": a["version"] == b["version"], "diff": lines}, indent=2))
        return 0
    if not lines:
        print(f"identical: {a['name']}@{a['version']} and {b['name']}@{b['version']}")
        return 0
    for line in lines:
        print(line)
    print(f"Next: briefcase eval run --dataset {name} --target <old> --target <new>  (compare both versions)")
    return 0
