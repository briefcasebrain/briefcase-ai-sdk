"""``briefcase-ai replay``: re-run recorded LLM calls and see what changed.

    briefcase-ai replay explain runs.jsonl          # which records can be replayed, and why not
    briefcase-ai replay run runs.jsonl              # plan, confirm, replay, compare
    briefcase-ai replay run runs.jsonl --executor cassette     # no network, deterministic
    briefcase-ai replay diff report.jsonl           # show what changed in a saved report

A separate top-level command: ``briefcase-ai run ...`` is the evaluation-job group.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

GROUP = "verify"

_RUN_EXAMPLE = "Example:\n  briefcase-ai replay run runs.jsonl --executor cassette --out report.jsonl"
_EXPLAIN_EXAMPLE = "Example:\n  briefcase-ai replay explain runs.jsonl"
_DIFF_EXAMPLE = "Example:\n  briefcase-ai replay diff report.jsonl"
_CAPTURE_HINT = (
    "capture calls with content first, e.g. in your app: "
    "briefcase.observe('runs.jsonl'); briefcase.auto(capture_content=True)"
)


def _home() -> Path:
    env = os.environ.get("BRIEFCASE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".briefcase"


def _state_path() -> Path:
    return _home() / "replay" / "last_run.json"


def register(sub) -> None:
    fmt = argparse.RawDescriptionHelpFormatter
    p = sub.add_parser(
        "replay", help="Re-run recorded LLM calls and see what changed.",
        description="Re-run recorded LLM calls and see what changed.", epilog=_RUN_EXAMPLE,
        formatter_class=fmt,
    )
    rsub = p.add_subparsers(dest="replay_command")

    run = rsub.add_parser(
        "run", help="Replay the calls in a JSONL file and compare the answers.",
        description="Replay the calls in a JSONL file and compare the answers. Live replays use "
                    "your installed provider SDKs and their usual credentials (OPENAI_API_KEY, "
                    "ANTHROPIC_API_KEY, GOOGLE_API_KEY, AWS_*) and spend your money: a plan is "
                    "shown and confirmed first.",
        epilog=_RUN_EXAMPLE, formatter_class=fmt,
    )
    run.add_argument("file", help="JSONL file of decision records (captured with content on).")
    run.add_argument("--executor", choices=("auto", "cassette"), default="auto",
                     help="auto: call each record's provider (default). cassette: no network.")
    run.add_argument("--cassette", metavar="FILE",
                     help="With --executor cassette: answer from this cassette instead of the "
                          "recorded outputs.")
    run.add_argument("--save-cassette", metavar="FILE",
                     help="Write the replay answers as a cassette for later offline runs.")
    run.add_argument("--model", help="What-if: send every call to this model instead.")
    run.add_argument("--param", action="append", default=[], metavar="K=V",
                     help="What-if: set a request parameter (repeatable). V is parsed as JSON "
                          "when it can be, e.g. --param temperature=0 "
                          "--param 'response_format={\"type\":\"json_object\"}'.")
    run.add_argument("--drop-param", action="append", default=[], metavar="K",
                     help="Do not send this recorded parameter (repeatable).")
    run.add_argument("--served-model", action="store_true",
                     help="Pin the dated model the provider reported, not the requested alias.")
    run.add_argument("--limit", type=int, help="Replay at most the first N replayable calls.")
    run.add_argument("--sample", type=int, help="Replay a random sample of N replayable calls.")
    run.add_argument("--seed", type=int, default=0, help="Seed for --sample (default 0).")
    run.add_argument("--concurrency", type=int, default=2, help="Parallel calls (default 2).")
    run.add_argument("--max-calls", type=int, default=100,
                     help="Refuse a live plan with more calls than this (default 100).")
    run.add_argument("--max-retries", type=int, default=3,
                     help="SDK retries with backoff on 429/5xx (default 3).")
    run.add_argument("--scorer", metavar="MODULE:FUNC",
                     help="Semantic comparison hook: FUNC(recorded_text, replay_text) -> 0..1.")
    run.add_argument("--threshold", type=float, default=0.9,
                     help="Score at or above which --scorer counts as a match (default 0.9).")
    run.add_argument("--prices", metavar="FILE",
                     help='JSON {"model": {"input_per_mtok": N, "output_per_mtok": N}} for cost '
                          "deltas. No price table is bundled.")
    run.add_argument("--out", metavar="FILE", help="Write one JSON line per record (the report).")
    run.add_argument("--plan-only", action="store_true", help="Show the plan and stop.")
    run.add_argument("--fail-on-change", action="store_true",
                     help="Exit 1 when any call changed (for CI), not only on errors.")
    run.add_argument("--yes", "-y", action="store_true", help="Do not ask before live calls.")
    run.add_argument("--json", action="store_true", help="Print the plan, summary and rows as JSON.")
    run.set_defaults(func=cmd_replay_run)

    explain = rsub.add_parser(
        "explain", help="List which records can be replayed and why the others cannot.",
        description="List which records can be replayed and why the others cannot. Reads only "
                    "the file; makes no calls.",
        epilog=_EXPLAIN_EXAMPLE, formatter_class=fmt,
    )
    explain.add_argument("file", help="JSONL file of decision records.")
    explain.add_argument("--json", action="store_true", help="Machine-readable output.")
    explain.set_defaults(func=cmd_replay_explain)

    diff = rsub.add_parser(
        "diff", help="Show what changed in a saved replay report.",
        description="Show what changed in a replay report written by `replay run --out`.",
        epilog=_DIFF_EXAMPLE, formatter_class=fmt,
    )
    diff.add_argument("report", help="Report JSONL written by `briefcase-ai replay run --out`.")
    diff.add_argument("--all", action="store_true", help="Also list matched calls.")
    diff.add_argument("--json", action="store_true", help="Machine-readable output.")
    diff.set_defaults(func=cmd_replay_diff)

    p.set_defaults(func=lambda args, *rest: _usage(p))


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help(sys.stderr)
    return 2


# -- helpers ----------------------------------------------------------------

def _err(message: str, fix: Optional[str] = None) -> int:
    print(f"error: {message}", file=sys.stderr)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)
    return 1


def _parse_params(pairs: List[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--param expects K=V, got {pair!r}")
        try:
            params[key.strip()] = json.loads(value)
        except ValueError:
            params[key.strip()] = value
    return params


def _load_scorer(spec: Optional[str]) -> Optional[Callable[[str, str], float]]:
    if not spec:
        return None
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"--scorer expects MODULE:FUNC, got {spec!r}")
    fn = getattr(importlib.import_module(module), name)
    if not callable(fn):
        raise ValueError(f"--scorer {spec} is not callable")
    return fn


def _confirm(question: str, ask: Optional[Callable[[str], str]] = None) -> Optional[bool]:
    """True/False, or None when there is nobody to ask (no tty)."""
    if ask is None:
        if not sys.stdin or not sys.stdin.isatty():
            return None
        ask = input
    try:
        answer = ask(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


#: Replaced in tests to drive the confirmation prompt.
ASK: Optional[Callable[[str], str]] = None


def _write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def _save_state(summary: Dict[str, Any], source: str, executor: str) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "file": Path(source).name, "executor": executor, "summary": summary,
        }, sort_keys=True))
    except OSError:
        pass


def _rerun(args) -> str:
    """The same `replay run` command without --plan-only."""
    parts = ["briefcase-ai replay run", shlex.quote(args.file)]
    if args.executor != "auto":
        parts.append(f"--executor {args.executor}")
    if args.model:
        parts.append(f"--model {shlex.quote(args.model)}")
    parts += [f"--param {shlex.quote(p)}" for p in args.param]
    parts += [f"--drop-param {shlex.quote(p)}" for p in args.drop_param]
    for flag in ("limit", "sample"):
        if getattr(args, flag) is not None:
            parts.append(f"--{flag} {getattr(args, flag)}")
    return " ".join(parts)


def _print_plan(plan: Dict[str, Any]) -> None:
    print(f"Replay plan ({plan['executor']} executor"
          f"{', live calls: spends your provider credits' if plan['live'] else ', no network'})")
    print(f"  records in file      {plan['records']}")
    print(f"  replayable           {plan['replayable']}"
          + (f"  ({plan['not_replayable']} not replayable: "
             "see `briefcase-ai replay explain`)" if plan["not_replayable"] else ""))
    if plan["blocked_by_executor"]:
        print(f"  blocked by executor  {plan['blocked_by_executor']}")
        for reason, count in plan["blocked_reasons"].items():
            print(f"    {count} x {reason}")
    print(f"  calls to make        {plan['calls']}")
    for model, count in plan["per_model"].items():
        print(f"    {model:<28} {count}")
    tokens = plan["tokens"]
    if plan["calls"]:
        if tokens["upper_bound_tokens"] is not None:
            print(f"  tokens (upper bound) {tokens['upper_bound_tokens']}  "
                  "(recorded prompt tokens + each call's max-tokens cap)")
        else:
            print(f"  tokens (estimate)    {tokens['estimate_tokens']}  "
                  f"(not a bound: {tokens['calls_without_output_cap']} call(s) have no "
                  "max-tokens cap and are counted at their recorded output length)")
        if tokens["calls_without_usage"]:
            print(f"  calls without recorded usage: {tokens['calls_without_usage']} "
                  "(not in the token figure)")


def _print_row(row: Dict[str, Any]) -> None:
    status = row["status"]
    label = f"#{row['index']} {row.get('surface') or '?'} {row.get('replay_model') or row.get('model') or ''}"
    if status == "matched":
        print(f"  matched ({row['match_level']})  {label}")
    elif status == "changed":
        tokens = ((row.get("tokens") or {}).get("delta") or {}).get("total_tokens")
        extra = f", tokens {tokens:+g}" if isinstance(tokens, (int, float)) else ""
        print(f"  changed           {label}{extra}")
    elif status == "error":
        print(f"  error             {label}: {row.get('error')}")


# -- commands ---------------------------------------------------------------

def cmd_replay_run(args, store=None, engine=None, stack=None) -> int:
    from briefcase.replay import recorded as rr

    if not Path(args.file).is_file():
        return _err(f"{args.file} not found", _CAPTURE_HINT)
    try:
        overrides = rr.Overrides(
            model=args.model, params=_parse_params(args.param), drop=set(args.drop_param),
            served_model=args.served_model,
        )
        scorer = _load_scorer(args.scorer)
        prices = json.loads(Path(args.prices).read_text()) if args.prices else None
    except (ValueError, ImportError, AttributeError, OSError) as exc:
        return _err(str(exc), "briefcase-ai replay run --help")

    run_id = f"replay-{uuid.uuid4().hex[:12]}"
    if args.executor == "cassette":
        if args.cassette and not Path(args.cassette).is_file():
            return _err(f"cassette {args.cassette} not found",
                        f"briefcase-ai replay run {args.file} --save-cassette {args.cassette} --yes")
        executor: Any = rr.CassetteExecutor(args.cassette)
    else:
        executor = rr.AutoExecutor(max_retries=args.max_retries, run_id=run_id)

    calls = rr.load_records(args.file)
    plan = rr.make_plan(calls, executor, overrides, limit=args.limit, sample=args.sample,
                        seed=args.seed)
    summary_plan = plan.summary()
    if not args.json:
        _print_plan(summary_plan)
    if args.plan_only:
        if args.json:
            print(json.dumps({"plan": summary_plan}, indent=2, sort_keys=True))
        else:
            print("Next: " + _rerun(args) + (" --yes" if plan.live else ""))
        return 0
    if plan.live and len(plan.to_run) > args.max_calls:
        return _err(
            f"the plan has {len(plan.to_run)} live calls, over --max-calls {args.max_calls}",
            f"briefcase-ai replay run {args.file} --limit {args.max_calls}",
        )
    if plan.live and plan.to_run and not args.yes:
        answer = _confirm(f"Make {len(plan.to_run)} live call(s) with your credentials?", ASK)
        if answer is None:
            return _err("live replay needs confirmation and there is no terminal to ask",
                        f"briefcase-ai replay run {args.file} --yes   (or --executor cassette)")
        if not answer:
            print("Nothing was called.")
            return 1

    cassette: Optional[List[Dict[str, Any]]] = [] if args.save_cassette else None
    try:
        rows = rr.run(plan, executor, concurrency=args.concurrency, max_calls=args.max_calls,
                      scorer=scorer, threshold=args.threshold, prices=prices, cassette=cassette,
                      on_row=None if args.json else _print_row)
    except rr.PlanError as exc:
        return _err(str(exc))
    summary = rr.summarize(rows)
    if args.out:
        _write_jsonl(args.out, rows)
    if cassette is not None and args.save_cassette:
        _write_jsonl(args.save_cassette, cassette)
    _save_state(summary, args.file, plan.executor)

    if args.json:
        print(json.dumps({"plan": summary_plan, "summary": summary, "rows": rows},
                         indent=2, sort_keys=True, default=str))
    else:
        counts = summary["status"]
        print("Result: " + ", ".join(f"{counts[s]} {s}" for s in rr.STATUSES))
        if summary["total_tokens_delta"] is not None:
            print(f"  total tokens delta     {summary['total_tokens_delta']:+g}")
        if summary["mean_latency_delta_ms"] is not None:
            print(f"  mean latency delta     {summary['mean_latency_delta_ms']:+.1f} ms")
        if summary["cost_usd_delta"] is not None:
            print(f"  cost delta             {summary['cost_usd_delta']:+.6f} USD")
        if args.out:
            print(f"Report: {args.out}")
            print(f"Next: briefcase-ai replay diff {args.out}")
        else:
            print(f"Next: briefcase-ai replay run {args.file} --out report.jsonl")
    failed = summary["status"]["error"] > 0 or (
        args.fail_on_change and summary["status"]["changed"] > 0)
    return 1 if failed else 0


def cmd_replay_explain(args, store=None, engine=None, stack=None) -> int:
    from briefcase.replay import recorded as rr

    if not Path(args.file).is_file():
        return _err(f"{args.file} not found", _CAPTURE_HINT)
    calls = rr.load_records(args.file)
    replayable = [c for c in calls if c.replayable]
    if args.json:
        print(json.dumps({
            "records": len(calls), "replayable": len(replayable),
            "calls": [c.summary() for c in calls],
        }, indent=2, sort_keys=True))
        return 0
    print(f"{len(replayable)} of {len(calls)} record(s) can be replayed.")
    for call in calls:
        ident = (call.decision_id or "")[:8] or "-"
        head = f"#{call.index} {ident} {call.surface or call.decision_type or '?'} {call.model or ''}"
        if call.replayable:
            print(f"  ok   {head}")
        else:
            print(f"  no   {head}")
            for reason in call.reasons:
                print(f"         - {reason}")
    notes: Dict[str, List[int]] = {}
    for call in calls:
        for warning in call.warnings:
            notes.setdefault(warning, []).append(call.index)
    for warning, indexes in notes.items():
        print(f"note ({', '.join(f'#{i}' for i in indexes)}): {warning}")
    if replayable:
        print(f"Next: briefcase-ai replay run {args.file} --plan-only")
    else:
        print(f"Next: {_CAPTURE_HINT}")
    return 0


def cmd_replay_diff(args, store=None, engine=None, stack=None) -> int:
    from briefcase.replay import recorded as rr
    from briefcase.replay._recorded.records import iter_jsonl
    from briefcase.replay._recorded.runner import RESULT_TYPE

    if not Path(args.report).is_file():
        return _err(f"{args.report} not found", "briefcase-ai replay run FILE --out report.jsonl")
    rows = [r for r in iter_jsonl(args.report) if r.get("record_type") == RESULT_TYPE]
    if not rows:
        return _err(f"{args.report} holds no replay results",
                    "briefcase-ai replay run FILE --out report.jsonl")
    summary = rr.summarize(rows)
    if args.json:
        shown = rows if args.all else [r for r in rows if r["status"] != "matched"]
        print(json.dumps({"summary": summary, "rows": shown}, indent=2, sort_keys=True, default=str))
        return 0
    counts = summary["status"]
    print(f"{summary['rows']} call(s): " + ", ".join(f"{counts[s]} {s}" for s in rr.STATUSES))
    for row in rows:
        status = row["status"]
        if status == "matched" and not args.all:
            continue
        print(f"\n#{row['index']} {row.get('surface')} {row.get('model')} -> "
              f"{row.get('replay_model') or row.get('model')}: {status}"
              + (f" ({row['match_level']})" if row.get("match_level") else ""))
        if status == "changed":
            checks = row.get("checks") or {}
            print("  checks: " + ", ".join(
                f"{k}={'n/a' if v is None else ('yes' if v else 'no')}" for k, v in checks.items()))
            tokens = (row.get("tokens") or {}).get("delta")
            if tokens:
                print("  tokens: " + ", ".join(
                    f"{k} {v:+g}" for k, v in tokens.items() if isinstance(v, (int, float))))
            latency = (row.get("latency_ms") or {}).get("delta")
            if isinstance(latency, (int, float)):
                print(f"  latency: {latency:+.1f} ms")
            if row.get("diff"):
                print("\n".join("  " + line for line in row["diff"].splitlines()))
        elif status == "error":
            print(f"  {row.get('error')}")
        elif status == "not_replayable":
            for reason in row.get("reasons") or []:
                print(f"  - {reason}")
    print("\nNext: briefcase-ai replay explain FILE   (why records were not replayable)"
          if counts["not_replayable"] else
          "\nNext: briefcase-ai drift check NAME --from FILE   (watch production for the same change)")
    return 0


def status() -> Dict[str, Any]:
    """Last replay run, for ``briefcase-ai status`` (content-free)."""
    base = {"id": "replay-recorded", "title": "Replay recorded LLM calls"}
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {**base, "state": "off",
                "detail": "no replay run yet",
                "next": "briefcase-ai replay explain runs.jsonl"}
    counts = (data.get("summary") or {}).get("status") or {}
    attention = counts.get("changed", 0) or counts.get("error", 0)
    return {
        **base,
        "state": "attention" if attention else "on",
        "detail": f"last replay {data.get('at')} ({data.get('executor')}, {data.get('file')}): "
                  + ", ".join(f"{counts.get(s, 0)} {s}" for s in
                              ("matched", "changed", "not_replayable", "error")),
        "next": "briefcase-ai replay diff report.jsonl" if attention else "briefcase-ai replay run FILE",
    }


__all__ = ["GROUP", "register", "status"]
