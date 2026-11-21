"""``python -m scripts.conformance`` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import core, drift
from .drivers import DRIVERS

DEFAULT_REPORT = core.REPO_ROOT / "briefcase" / "connect" / "conformance-report.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return core.new_report()


def cmd_run(args) -> int:
    tools = args.tools or list(DRIVERS)
    unknown = [t for t in tools if t not in DRIVERS]
    if unknown:
        print(f"error: unknown tool(s) {unknown}; known: {', '.join(DRIVERS)}", file=sys.stderr)
        return 2
    results = []
    for tool in tools:
        driver = DRIVERS[tool](existing_binary=args.use_path, log=lambda m: print(m, file=sys.stderr),
                               keep=args.keep, save_dir=args.save_payloads)
        result = driver.conform()
        results.append(result.as_dict())
        print(f"{tool:<16} {result.status:<8} {result.level:<19} {result.version or '-'}  {result.note[:90]}")
        for check in result.checks:
            detail = f"  [{check.detail[:100]}]" if check.detail else ""
            print(f"    {'ok  ' if check.ok else 'FAIL'} {check.name}{detail}")
    if args.results_out:
        args.results_out.parent.mkdir(parents=True, exist_ok=True)
        args.results_out.write_text(json.dumps({"results": results}, indent=2) + "\n", encoding="utf-8")
    previous = _load(args.merge_into) if args.merge_into else core.new_report()
    old = json.loads(json.dumps(previous))
    report = core.merge_results(previous, results)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"report written to {args.out}")
    for line in core.level_changes(old, report):
        print("CHANGE " + line)
    return 1 if any(r["status"] == "failed" for r in results) and not args.allow_failures else 0


def cmd_drift(args) -> int:
    report = drift.check_all(only=args.tools or None, offline_dir=args.pages)
    print(drift.render(report))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if report["summary"]["vanished"] else 0


def cmd_merge(args) -> int:
    report = _load(args.into) if args.into.exists() else core.new_report()
    seen = set()
    for path in args.results:
        data = json.loads(path.read_text(encoding="utf-8"))
        batch = data["results"] if "results" in data else [data]
        seen.update(r["tool"] for r in batch)
        report = core.merge_results(report, batch)
    # a matrix job that died before writing its result is a failure, not silence
    lost = [{"tool": tool, "status": "failed", "level": core.DOCS_ONLY, "version": None, "date": core.today(),
             "note": "no result file: the job crashed or was cancelled before reporting", "install": "", "checks": []}
            for tool in (args.expect or "").split(",") if tool and tool not in seen]
    report = core.merge_results(report, lost)
    args.into.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def cmd_scrub(args) -> int:
    from . import scrub

    count = 0
    for source in sorted(args.source.rglob("*.json")):
        dest = args.dest / source.relative_to(args.source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        scrub.scrub_file(str(source), str(dest))
        count += 1
        print(f"scrubbed {source.name} -> {dest}")
    return 0 if count else 1


def cmd_changes(args) -> int:
    lines = core.level_changes(_load(args.old), _load(args.new))
    print("\n".join(lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.conformance",
                                     description="Check `briefcase connect` / `receive` against real tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="Install each tool in a temp prefix and HOME, connect, verify, report a level.")
    p.add_argument("tools", nargs="*", help=f"Default: all ({', '.join(DRIVERS)}).")
    p.add_argument("--out", type=Path, help="Write the report JSON here (merged into --merge-into if given).")
    p.add_argument("--results-out", type=Path, help="Write raw per-tool results for `merge`.")
    p.add_argument("--merge-into", type=Path, help="Existing report to update instead of starting empty.")
    p.add_argument("--use-path", action="store_true", help="Use the tool already on PATH instead of installing.")
    p.add_argument("--keep", action="store_true", help="Keep the temp directories.")
    p.add_argument("--allow-failures", action="store_true", help="Exit 0 even when a tool failed.")
    p.add_argument("--save-payloads", type=Path, help="Directory for raw (UNSCRUBBED) payloads; scrub them first.")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("drift", help="Fetch each adapter's docs_url and check the expected strings still appear.")
    p.add_argument("tools", nargs="*")
    p.add_argument("--out", type=Path)
    p.add_argument("--pages", type=Path, help="Read <adapter>.html from this directory instead of the network (tests).")
    p.set_defaults(func=cmd_drift)
    p = sub.add_parser("merge", help="Merge per-tool result files (from a CI matrix) into one report.")
    p.add_argument("--into", type=Path, required=True)
    p.add_argument("--expect", help="Comma-separated tools that must have reported; absent ones count as failed.")
    p.add_argument("results", nargs="*", type=Path)
    p.set_defaults(func=cmd_merge)
    p = sub.add_parser("scrub", help="Scrub raw captured payloads into a fixture directory.")
    p.add_argument("source", type=Path)
    p.add_argument("dest", type=Path)
    p.set_defaults(func=cmd_scrub)
    p = sub.add_parser("changes", help="Print level changes and failures between two reports.")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.set_defaults(func=cmd_changes)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
