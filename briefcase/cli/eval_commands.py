"""``briefcase eval run``: score a function against a dataset version, locally.

    briefcase eval run --dataset triage --target myapp.triage:classify --fail-under 0.9
    briefcase eval run --dataset triage@v2 --target app:triage_v1 --target app:triage_v2

Named ``eval`` because ``run`` is the evaluation-engine job group
(``briefcase run submit``). Results are ``eval.case`` / ``eval.run`` decision
records sent to ``--to``, else ``$BRIEFCASE_EXPORTER`` / the OpenTelemetry
variables, else ``$BRIEFCASE_HOME/evals/records.jsonl``.

Gates (CI). One target: ``--fail-under X`` gates it. Several targets: name what is
gated, because the usual comparison (old prompt vs new) expects the old one to be worse:

    --fail-under X --gate NEW [--gate OTHER ...]   only the named targets must reach X
    --no-regression OLD [--tolerance T]            every other target must score >= OLD - T

Exit codes: 0 gates pass (or none given), 1 a gate failed, 2 usage (unknown dataset,
target or scorer, or a gate that is ambiguous or names an unknown target).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, List, Optional

# `status` is this module's `briefcase status` row (dataset count, last eval result).
from briefcase.cli.dataset_versions import status  # noqa: F401
from briefcase.datasets import core
from briefcase.datasets import evaluate as ev
from briefcase.datasets.core import DatasetError, DatasetStore

GROUP = "review"


def register(sub: argparse._SubParsersAction) -> None:
    if "eval" in sub.choices:
        return
    parser = sub.add_parser(
        "eval", help="Score a function against an evaluation dataset (local, offline).",
        description="Score functions against versioned datasets made with `briefcase dataset from-records`.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  briefcase eval run --dataset triage --target myapp.triage:classify --fail-under 0.9",
    )
    ev_sub = parser.add_subparsers(dest="sub")
    p = ev_sub.add_parser(
        "run", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="Call a function for every case, score it, log eval records.",
        description=(
            "Calls TARGET once per case: an input {\"args\": [...], \"kwargs\": {...}} is spread into the "
            "call, anything else is passed as one argument. Repeat --target to compare (for example two "
            "prompt versions) in one table; rows are split by the prompt_version each call used.\n\n"
            "env: BRIEFCASE_EXPORTER (destination when --to is not given), BRIEFCASE_HOME."
        ),
        epilog="example:\n  briefcase eval run --dataset triage --target myapp.triage:classify "
               "--scorer exact --fail-under 0.9",
    )
    p.add_argument("--dataset", required=True, metavar="NAME[@VERSION]")
    p.add_argument("--target", required=True, action="append", metavar="MODULE:FUNCTION",
                   help="Function to evaluate (repeatable).")
    p.add_argument("--scorer", default="exact", metavar="SCORER",
                   help="exact (default), contains, json, or module:function(output, reference, case) "
                        "returning bool, a number (passes at >= 1.0) or {'score', 'passed'}.")
    p.add_argument("--fail-under", type=float, metavar="RATE",
                   help="Exit 1 when a gated target's pass rate is below RATE (0-1). One --target: gates it. "
                        "Several: also give --gate TARGET. For CI.")
    p.add_argument("--gate", action="append", metavar="TARGET",
                   help="Apply --fail-under only to this --target (repeatable; required with --fail-under "
                        "when there are several targets).")
    p.add_argument("--no-regression", metavar="BASELINE",
                   help="Exit 1 when any other target's pass rate is below BASELINE's (one of the --target "
                        "values) by more than --tolerance.")
    p.add_argument("--tolerance", type=float, default=None, metavar="T",
                   help="Allowed drop below the baseline with --no-regression, as a rate (default 0).")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Cases run in parallel (threads; default 1). Results are identical in any case order.")
    p.add_argument("--to", metavar="DEST",
                   help="Exporter for eval records: a .jsonl path, console, or an http(s) URL.")
    p.add_argument("--capture-content", action="store_true",
                   help="Put case inputs/outputs in the eval records (default: SHA-256 digests only).")
    p.add_argument("--show", type=int, default=5, metavar="N", help="Worst failures to print per target.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    p.set_defaults(func=cmd_eval_run)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _short(value: Any, width: int = 32) -> str:
    if isinstance(value, str):
        text = repr(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 1] + "…"


_EPS = 1e-9


def _check_gates(args) -> Optional[str]:
    """A usage error for contradictory or ambiguous gate flags, else None."""
    targets = list(args.target)
    gate = getattr(args, "gate", None) or []
    baseline = getattr(args, "no_regression", None)
    tol = getattr(args, "tolerance", None)
    if args.fail_under is not None and not 0.0 <= args.fail_under <= 1.0:
        return "--fail-under is a rate between 0 and 1 (for example 0.9)"
    if tol is not None and not 0.0 <= tol <= 1.0:
        return "--tolerance is a rate between 0 and 1 (for example 0.02)"
    if len(set(targets)) != len(targets):
        return "the same --target is given twice; results are reported per target"
    if tol is not None and baseline is None:
        return "--tolerance only applies with --no-regression BASELINE"
    if gate and args.fail_under is None:
        return "--gate names the targets --fail-under applies to; give --fail-under RATE too"
    for name in gate:
        if name not in targets:
            return f"--gate {name} is not one of the --target values ({', '.join(targets)})"
    if baseline is not None:
        if baseline not in targets:
            return f"--no-regression {baseline} is not one of the --target values ({', '.join(targets)})"
        if len(targets) < 2:
            return "--no-regression compares against another target: give at least two --target values"
    if args.fail_under is not None and len(targets) > 1 and not gate:
        base = f"briefcase eval run --dataset {args.dataset} " + " ".join(f"--target {t}" for t in targets)
        return (f"--fail-under with {len(targets)} targets does not say which must reach it (an old prompt is "
                f"expected to score lower than a new one). Name the gated target(s):\n"
                f"  {base} --fail-under {args.fail_under:g} --gate {targets[-1]}\n"
                f"or compare against a baseline:\n"
                f"  {base} --no-regression {targets[0]}\n"
                f"(both flags can be combined; repeat --gate for more targets)")
    return None


def _gate_for(spec: str, args, rates: dict) -> dict:
    """The gate applied to one target, evaluated once every target has a pass rate."""
    gate = getattr(args, "gate", None) or []
    baseline = getattr(args, "no_regression", None)
    tol = getattr(args, "tolerance", None) or 0.0
    clauses = []
    if args.fail_under is not None and (spec in gate or (not gate and len(args.target) == 1)):
        clauses.append({"kind": "fail_under", "min_rate": args.fail_under,
                        "flag": f"--fail-under {args.fail_under:g}"})
    if baseline is not None and spec != baseline:
        floor = max(0.0, rates[baseline] - tol)
        clauses.append({"kind": "no_regression", "baseline": baseline, "baseline_rate": rates[baseline],
                        "tolerance": tol, "min_rate": floor,
                        "flag": f"--no-regression {baseline} --tolerance {tol:g}"})
    rate = rates[spec]
    for c in clauses:
        c["passed"] = rate + _EPS >= c["min_rate"]
    ok = all(c["passed"] for c in clauses)
    if not clauses:
        text = "-"
    else:
        text = " + ".join(f">= {_pct(c['min_rate'])}" + (" (baseline)" if c["kind"] == "no_regression" else "")
                          for c in clauses)
    return {"clauses": clauses, "gated": bool(clauses), "ok": ok, "text": text}


def cmd_eval_run(args, store, engine=None, stack=None) -> int:
    home = Path(getattr(store, "home", None) or core.default_home())
    problem = _check_gates(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    try:
        version = DatasetStore(home).get(args.dataset)
        scorer_name, scorer, needs_ref = ev.resolve_scorer(args.scorer)
        targets = [(spec, ev.load_callable(spec)) for spec in args.target]
        exporter, destination = ev.resolve_exporter(args.to, home)
    except (DatasetError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if needs_ref and all(c.get("reference") is None for c in version.cases):
        print(f"error: {version.ref} has no references (made with --reference none), so --scorer "
              f"{scorer_name} has nothing to compare against. Use --scorer module:function, or add "
              f"references: briefcase dataset export {version.ref} --out {version.name}.jsonl, edit, "
              f"then briefcase dataset import {version.name}.jsonl", file=sys.stderr)
        return 2

    invocation = str(uuid.uuid4())
    rows: List[ev.GroupStats] = []
    per_target = []
    ran = []
    try:
        for spec, fn in targets:
            results = ev.run_cases(version.cases, fn, scorer, needs_reference=needs_ref,
                                   concurrency=max(1, args.concurrency))
            run_record = ev.log_results(
                version, spec, scorer_name, results, exporter, capture_content=args.capture_content)
            ran.append((spec, results, run_record))
    finally:
        ev.flush_exporter(exporter)

    rates = {spec: (rr["outputs"].get("pass_rate") or 0.0) for spec, _, rr in ran}
    ok = True
    for spec, results, run_record in ran:
        summary = run_record["outputs"]
        rate = rates[spec]
        gate = _gate_for(spec, args, rates)
        ok = ok and gate["ok"]
        groups = ev.group_results(spec, results)
        rows.extend(groups)
        per_target.append({"target": spec, "run_id": run_record["run_id"], "cases": len(results),
                           "passed": summary.get("passed"), "pass_rate": rate,
                           "errors": sum(1 for r in results if r.error), "ok": gate["ok"],
                           "gate": {"applied": gate["gated"], "description": gate["text"],
                                    "result": ("pass" if gate["ok"] else "fail") if gate["gated"] else None,
                                    "clauses": gate["clauses"]},
                           "groups": [g.as_dict() for g in groups],
                           "worst": [{"id": r.case_id, "score": r.score, "error": r.error,
                                      "expected": r.reference, "got": r.output}
                                     for r in ev.worst_first(results)[: max(0, args.show)]]})
        ev.append_history(home, {
            "dataset": version.ref, "dataset_hash": version.hash, "target": spec, "scorer": scorer_name,
            "cases": len(results), "passed": summary.get("passed"), "pass_rate": rate,
            "fail_under": next((c["min_rate"] for c in gate["clauses"] if c["kind"] == "fail_under"), None),
            "gate": ", ".join(c["flag"] for c in gate["clauses"]) or None,
            "ok": gate["ok"], "run_id": run_record["run_id"],
            "groups": [g.as_dict() for g in groups], "invocation": invocation,
        })

    records = sum(t["cases"] for t in per_target)
    if args.json:
        print(json.dumps({"dataset": version.ref, "dataset_hash": version.hash, "scorer": scorer_name,
                          "fail_under": args.fail_under, "gate": getattr(args, "gate", None) or [],
                          "no_regression": getattr(args, "no_regression", None),
                          "tolerance": getattr(args, "tolerance", None) or 0.0, "ok": ok, "targets": per_target,
                          "records": {"eval.case": records, "eval.run": len(per_target),
                                      "destination": destination}},
                         indent=2, ensure_ascii=False, default=str))
        return 0 if ok else 1

    print(f"eval {version.ref} ({version.hash[:12]}): {len(version.cases)} cases, scorer {scorer_name}")
    headers = ("target", "prompt", "cases", "passed", "pass rate", "mean score", "errors", "gate", "result")
    table = []
    seen = set()
    by_target = {t["target"]: t for t in per_target}
    for g in rows:
        first = g.target not in seen
        seen.add(g.target)
        gt = by_target[g.target]["gate"]
        table.append((g.target, f"{g.prompt_name}@{g.prompt_version}" if g.prompt_version else "-", str(g.cases),
                      str(g.passed), _pct(g.pass_rate), f"{g.mean_score:.2f}", str(g.errors),
                      gt["description"] if first else "",
                      (gt["result"] or "-").upper() if first else ""))
    widths = [max(len(h), *(len(r[i]) for r in table)) for i, h in enumerate(headers)]
    print(("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths))).rstrip())
    for r in table:
        print(("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths))).rstrip())
    for t in per_target:
        if not t["worst"]:
            continue
        print(f"Worst failures ({t['target']}):")
        for w in t["worst"]:
            if w["error"]:
                print(f"  {w['id']:<16} error     {w['error'][:70]}")
            else:
                print(f"  {w['id']:<16} score {w['score']:.2f}  expected {_short(w['expected'])}  "
                      f"got {_short(w['got'])}")
    print(f"records: {records} eval.case + {len(per_target)} eval.run -> {destination}")
    gated = False
    for t in per_target:
        for c in t["gate"]["clauses"]:
            gated = True
            verdict = "PASS" if c["passed"] else "FAIL"
            cmp = ">=" if c["passed"] else "<"
            if c["kind"] == "fail_under":
                why = f"({c['flag']})"
            else:
                why = f"(baseline {c['baseline']} {_pct(c['baseline_rate'])}, tolerance {c['tolerance']:g})"
            print(f"{verdict}  {t['target']}: pass rate {_pct(t['pass_rate'])} {cmp} {_pct(c['min_rate'])} {why}")
    if ok:
        print(f"Next: briefcase dataset show {version.ref}" if gated else
              ("Next: add --fail-under 0.9 to gate CI on this pass rate" if len(per_target) == 1 else
               f"Next: add --no-regression {per_target[0]['target']} (or --fail-under 0.9 --gate "
               f"{per_target[-1]['target']}) to gate CI"))
    else:
        print(f"Next: briefcase dataset show {version.ref} --cases 20  (inspect the failing inputs)")
    return 0 if ok else 1
