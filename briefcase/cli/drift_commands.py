"""``briefcase drift``: compare recent records against a saved baseline.

    briefcase drift baseline save prod --from runs.jsonl --last 500
    briefcase drift check prod --from today.jsonl --fail-on attention   # CI gate
    briefcase drift baselines

Profiles live in ``~/.briefcase/baselines/<name>.json`` (``$BRIEFCASE_HOME``
overrides the root) and hold aggregates and hashes only, never content.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

GROUP = "review"

_SAVE_EXAMPLE = "Example:\n  briefcase drift baseline save prod --from runs.jsonl --last 500"
_CHECK_EXAMPLE = "Example:\n  briefcase drift check prod --from today.jsonl --fail-on attention"
_LIST_EXAMPLE = "Example:\n  briefcase drift baselines"


def _home() -> Path:
    env = os.environ.get("BRIEFCASE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".briefcase"


def _state_path() -> Path:
    return _home() / "drift" / "last_check.json"


def _selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from", dest="source", required=True, metavar="FILE",
                   help="JSONL file of decision records.")
    p.add_argument("--where", action="append", default=[], metavar="KEY=GLOB",
                   help="Keep records whose dotted KEY matches GLOB (repeatable), e.g. "
                        "model_parameters.model_name=gpt-4o*.")
    p.add_argument("--since", help="Only records started at/after this (ISO time or 30m/24h/7d).")
    p.add_argument("--until", help="Only records started at/before this (ISO time or age).")
    p.add_argument("--last", type=int, metavar="N", help="Only the last N records by start time.")
    p.add_argument("--embed", metavar="MODULE:FUNC",
                   help="Optional embedding hook: FUNC(list_of_texts) -> list of vectors.")


def register(sub) -> None:
    fmt = argparse.RawDescriptionHelpFormatter
    p = sub.add_parser(
        "drift", help="Compare recent records against a saved baseline.",
        description="Compare recent records against a saved baseline.", epilog=_CHECK_EXAMPLE,
        formatter_class=fmt,
    )
    dsub = p.add_subparsers(dest="drift_command")

    base = dsub.add_parser(
        "baseline", help="Save a baseline profile.", description="Save a baseline profile.",
        epilog=_SAVE_EXAMPLE, formatter_class=fmt,
    )
    bsub = base.add_subparsers(dest="baseline_command")
    save = bsub.add_parser(
        "save", help="Build a baseline profile from records and save it.",
        description="Build a baseline profile from records and save it to "
                    "~/.briefcase/baselines/NAME.json. The profile holds aggregates and hashes "
                    "only (no prompts or outputs), so it is safe to commit or share.",
        epilog=_SAVE_EXAMPLE, formatter_class=fmt,
    )
    save.add_argument("name", help="Baseline name (letters, digits, '.', '_', '-').")
    _selection(save)
    save.add_argument("--no-categories", action="store_true",
                      help="Do not keep hashed output labels (short labels can be guessed "
                           "from their hash by trying candidates).")
    save.add_argument("--json", action="store_true", help="Print the saved profile summary as JSON.")
    save.set_defaults(func=cmd_baseline_save)
    base.set_defaults(func=lambda args, *rest: _usage(base))

    chk = dsub.add_parser(
        "check", help="Compare a window of records against a baseline.",
        description="Compare a window of records against a baseline. Each signal reports its "
                    "value, threshold, sample sizes and a plain reading. With --fail-on attention "
                    "the exit code is 1 when any signal crosses its threshold (a CI gate).",
        epilog=_CHECK_EXAMPLE, formatter_class=fmt,
    )
    chk.add_argument("name", help="Baseline name (see `briefcase drift baselines`).")
    _selection(chk)
    chk.add_argument("--threshold", action="append", default=[], metavar="KEY=VALUE",
                     help="Override a threshold (repeatable), e.g. psi=0.1 min_n=50.")
    chk.add_argument("--fail-on", choices=("never", "attention", "insufficient"), default="never",
                     help="Exit 1 on: attention (drift over a threshold), or insufficient (also "
                          "when too few records to score). Default never.")
    chk.add_argument("--all", action="store_true", help="Also list signals that were not scored.")
    chk.add_argument("--json", action="store_true", help="Machine-readable output.")
    chk.set_defaults(func=cmd_check)

    lst = dsub.add_parser(
        "baselines", help="List saved baselines.", description="List saved baselines.",
        epilog=_LIST_EXAMPLE, formatter_class=fmt,
    )
    lst.add_argument("--json", action="store_true", help="Machine-readable output.")
    lst.set_defaults(func=cmd_baselines)

    p.set_defaults(func=lambda args, *rest: _usage(p))


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help(sys.stderr)
    return 2


def _err(message: str, fix: Optional[str] = None) -> int:
    print(f"error: {message}", file=sys.stderr)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)
    return 1


def _pairs(values: List[str], flag: str) -> Dict[str, str]:
    out = {}
    for pair in values:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{flag} expects KEY=VALUE, got {pair!r}")
        out[key.strip()] = value
    return out


def _embed(spec: Optional[str]) -> Optional[Callable[[List[str]], Any]]:
    if not spec:
        return None
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"--embed expects MODULE:FUNC, got {spec!r}")
    return getattr(importlib.import_module(module), name)


def _select(args) -> List[Dict[str, Any]]:
    from briefcase.drift import baseline as bl

    return bl.select_records(args.source, where=_pairs(args.where, "--where"),
                             since=args.since, until=args.until, last=args.last)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def cmd_baseline_save(args, store=None, engine=None, stack=None) -> int:
    from briefcase.drift import baseline as bl

    try:
        bl.check_name(args.name)
    except ValueError as exc:
        return _err(str(exc), "briefcase drift baseline save prod --from runs.jsonl")
    if not Path(args.source).is_file():
        return _err(f"{args.source} not found",
                    "write records to a file first: briefcase.observe('runs.jsonl')")
    try:
        records = _select(args)
        embed = _embed(args.embed)
    except (ValueError, ImportError, AttributeError) as exc:
        return _err(str(exc), "briefcase drift baseline save --help")
    if not records:
        return _err("no records matched the selection",
                    f"briefcase drift baseline save {args.name} --from {args.source}")
    source = {
        "file": Path(args.source).name,  # basename only: host paths are not kept
        "file_sha256": _sha256(args.source),
        "where": _pairs(args.where, "--where"),
        "since": args.since, "until": args.until, "last": args.last,
    }
    profile = bl.build_profile(records, args.name, embed=embed,
                               categories=not args.no_categories, source=source)
    replaced = bl.profile_path(args.name).exists()
    path = bl.save_profile(profile)
    min_n = int(bl.DEFAULT_THRESHOLDS["min_n"])
    info = {
        "name": args.name, "path": str(path), "replaced": replaced, "n": profile["n"],
        "time_range": profile["time_range"], "models": profile["models"],
        "signals_available": sorted(
            list(profile["numeric"]) + list(profile["means"])
            + [k for k in ("categories", "lexical", "embedding") if profile.get(k)]),
    }
    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0
    print(f"{'Replaced' if replaced else 'Saved'} baseline {args.name!r}: {profile['n']} records"
          + (f", {profile['time_range'][0]} .. {profile['time_range'][1]}"
             if profile["time_range"] else ""))
    print(f"  file      {path}")
    print(f"  signals   {', '.join(info['signals_available']) or 'rates only'}")
    print("  content   none stored (aggregates and hashes only)")
    if profile["n"] < min_n:
        print(f"  warning   fewer than {min_n} records: checks against this baseline will "
              "refuse to score until it has more")
    print(f"Next: briefcase drift check {args.name} --from FILE --since 24h")
    return 0


def _save_state(result: Dict[str, Any]) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "baseline": result["baseline"], "state": result["state"],
            "n_window": result["n_window"], "attention": result["attention"],
        }, sort_keys=True))
    except OSError:
        pass


_MARK = {"attention": "ATTENTION", "ok": "ok", "insufficient_data": "not scored",
         "unavailable": "n/a"}


def cmd_check(args, store=None, engine=None, stack=None) -> int:
    from briefcase.drift import baseline as bl

    try:
        profile = bl.load_profile(args.name)
    except FileNotFoundError:
        return _err(f"no baseline named {args.name!r}",
                    f"briefcase drift baseline save {args.name} --from runs.jsonl")
    except ValueError as exc:
        return _err(str(exc), "briefcase drift baselines")
    if not Path(args.source).is_file():
        return _err(f"{args.source} not found", "briefcase drift check --help")
    try:
        thresholds = {k: float(v) for k, v in _pairs(args.threshold, "--threshold").items()}
        unknown = sorted(set(thresholds) - set(bl.DEFAULT_THRESHOLDS))
        if unknown:
            raise ValueError(f"unknown threshold(s) {', '.join(unknown)}; known: "
                             + ", ".join(sorted(bl.DEFAULT_THRESHOLDS)))
        records = _select(args)
        embed = _embed(args.embed)
    except (ValueError, ImportError, AttributeError) as exc:
        return _err(str(exc), "briefcase drift check --help")
    result = bl.check(profile, records, thresholds=thresholds, embed=embed)
    _save_state(result)
    failed = (args.fail_on == "attention" and result["state"] == "attention") or (
        args.fail_on == "insufficient" and result["state"] in ("attention", "insufficient_data"))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if failed else 0
    print(f"Drift check: baseline {args.name!r} ({result['n_baseline']} records) vs window "
          f"({result['n_window']} records): {_MARK.get(result['state'], result['state'])}")
    for signal in result["signals"]:
        if signal["state"] in ("insufficient_data", "unavailable") and not args.all:
            continue
        print(f"  {_MARK[signal['state']]:<10} {signal['signal']:<26} {signal['reading']}")
    skipped = [s for s in result["signals"]
               if s["state"] in ("insufficient_data", "unavailable")]
    if skipped and not args.all:
        print(f"  ({len(skipped)} signal(s) not scored; --all lists them with the reason)")
    if result["state"] == "insufficient_data":
        print(f"  Nothing was scored: each signal needs at least {int(result['thresholds']['min_n'])}"
              " records on both sides.")
        print(f"Next: briefcase drift check {args.name} --from {args.source} --since 7d")
    elif result["state"] == "attention":
        print(f"Next: briefcase replay run {args.source} --sample 20   "
              "(re-run a sample to see what changed)")
    else:
        print(f"Next: briefcase drift check {args.name} --from FILE --fail-on attention   "
              "(add to CI)")
    return 1 if failed else 0


def cmd_baselines(args, store=None, engine=None, stack=None) -> int:
    from briefcase.drift import baseline as bl

    profiles = bl.list_profiles()
    if args.json:
        print(json.dumps({"baselines": profiles, "dir": str(bl.baselines_dir())},
                         indent=2, sort_keys=True))
        return 0
    if not profiles:
        print(f"No baselines in {bl.baselines_dir()}.")
        print("Next: briefcase drift baseline save prod --from runs.jsonl")
        return 0
    for item in profiles:
        span = " .. ".join(item["time_range"]) if item["time_range"] else "no timestamps"
        print(f"  {item['name']:<20} {item['n']:>6} records  {span}  "
              f"models: {', '.join(item['models']) or '-'}")
    print(f"Next: briefcase drift check {profiles[0]['name']} --from FILE")
    return 0


def status() -> Dict[str, Any]:
    """Last drift check, for ``briefcase status`` (content-free)."""
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {"feature": "drift-baseline", "state": "off", "summary": "no drift check yet",
                "next": "briefcase drift baseline save prod --from runs.jsonl"}
    state = data.get("state")
    flagged = data.get("attention") or []
    return {
        "feature": "drift-baseline",
        "state": "attention" if state == "attention" else "on",
        "summary": f"last check {data.get('at')} against {data.get('baseline')!r}: {state}"
                   + (f" ({', '.join(flagged)})" if flagged else ""),
        "next": f"briefcase drift check {data.get('baseline')} --from FILE",
    }


__all__ = ["GROUP", "register", "status"]
