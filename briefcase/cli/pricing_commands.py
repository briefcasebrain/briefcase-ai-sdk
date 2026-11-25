"""``briefcase-ai pricing``: see, check and extend the model price table.

    briefcase-ai pricing check --records runs.jsonl     # which of YOUR models have no price?
    briefcase-ai pricing list | show MODEL | set MODEL ... | unset MODEL | import FILE

Layers (lowest to highest): packaged data < ~/.briefcase/pricing.json < $BRIEFCASE_PRICING_FILE.
`BRIEFCASE_HOME` moves ~/.briefcase. Only the user file is ever written.
"""
from __future__ import annotations

import difflib
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from briefcase import pricing as pr

GROUP = "review"
DEFAULT_MAX_AGE_DAYS = 90
SET_EXAMPLE = ("briefcase-ai pricing set MODEL --input USD_PER_MTOK --output USD_PER_MTOK "
               "--context-window N --provider NAME --source-url URL")


def _today() -> date:
    return date.today()


def register(sub) -> None:
    p = sub.add_parser(
        "pricing",
        help="Model prices: list, check which of your models are unpriced, add verified prices.",
        description="Versioned model pricing with provenance. Layers: packaged data < "
                    "~/.briefcase/pricing.json < $BRIEFCASE_PRICING_FILE ($BRIEFCASE_HOME moves ~/.briefcase).",
        epilog="example: briefcase-ai pricing check --records runs.jsonl",
    )
    p.set_defaults(func=cmd_usage, pricing_parser=p)
    ps = p.add_subparsers(dest="pricing_cmd")

    sp = ps.add_parser("list", help="List priced models.",
                       epilog="example: briefcase-ai pricing list --provider anthropic")
    sp.add_argument("--provider", help="Only this provider.")
    sp.add_argument("--json", action="store_true", help="Machine-readable output.")
    sp.set_defaults(func=cmd_list)

    sp = ps.add_parser("show", help="Resolved price, source layer and provenance for one model.",
                       epilog="example: briefcase-ai pricing show claude-sonnet-4-5")
    sp.add_argument("model")
    sp.add_argument("--json", action="store_true", help="Machine-readable output.")
    sp.set_defaults(func=cmd_show)

    sp = ps.add_parser("set", help="Add or replace one model's price in the user file (needs a source URL).",
                       epilog="example: briefcase-ai pricing set my-model --input 2.5 --output 10 "
                              "--context-window 128000 --provider acme --source-url https://acme.example/pricing")
    sp.add_argument("model")
    sp.add_argument("--input", required=True, metavar="USD_PER_MTOK", help="Input price, USD per million tokens.")
    sp.add_argument("--output", required=True, metavar="USD_PER_MTOK", help="Output price, USD per million tokens.")
    sp.add_argument("--cached-input", metavar="USD_PER_MTOK",
                    help="Cached-input price. Recorded; the native engine applies flat rates only for user entries.")
    sp.add_argument("--source-url", required=True, help="Where you read the price (kept as provenance).")
    sp.add_argument("--context-window", type=int, help="Required for a model not already in the table.")
    sp.add_argument("--max-output-tokens", type=int)
    sp.add_argument("--provider", help="Required for a model not already in the table.")
    sp.add_argument("--dry-run", dest="pricing_dry_run", action="store_true", help="Show the diff; write nothing.")
    sp.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    sp.set_defaults(func=cmd_set)

    sp = ps.add_parser("unset", help="Remove a model from the user file (undo for `pricing set`).",
                       epilog="example: briefcase-ai pricing unset my-model")
    sp.add_argument("model")
    sp.add_argument("--dry-run", dest="pricing_dry_run", action="store_true")
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_unset)

    sp = ps.add_parser("import", help="Merge a pricing JSON file into the user file.",
                       epilog="example: briefcase-ai pricing import team-prices.json")
    sp.add_argument("file")
    sp.add_argument("--dry-run", dest="pricing_dry_run", action="store_true")
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_import)

    sp = ps.add_parser("check", help="Validate all layers; flag unverified/old data and unpriced models in records.",
                       epilog="example: briefcase-ai pricing check --records runs.jsonl")
    sp.add_argument("--records", metavar="FILE", help="Records JSONL; reports its models that have no price.")
    sp.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                    help=f"Flag data older than this (default {DEFAULT_MAX_AGE_DAYS}).")
    sp.add_argument("--strict", action="store_true", help="Exit 1 on warnings too.")
    sp.add_argument("--json", action="store_true", help="Machine-readable output.")
    sp.set_defaults(func=cmd_check)


# ---------------------------------------------------------------------------------------------

def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _confirm(question: str, assume_yes: bool) -> Optional[bool]:
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _load(args=None):
    """(table, None) or (None, exit code) after printing the validation errors."""
    try:
        return pr.load(), None
    except pr.PricingError as exc:
        _err("Pricing files are invalid:")
        for line in exc.errors:
            _err(f"  {line}")
        _err("Fix the lines above, or move the file aside; `briefcase-ai pricing check` shows every layer.")
        return None, 1


def cmd_usage(args, store=None, engine=None, stack=None) -> int:
    args.pricing_parser.print_help(sys.stderr)
    return 2


def _row(name: str, e: Dict[str, Any]) -> Dict[str, Any]:
    return {"model": name, "provider": e["provider"], "input_usd_per_mtok": pr.mtok(e["input_cost_per_1k_tokens"]),
            "output_usd_per_mtok": pr.mtok(e["output_cost_per_1k_tokens"]), "context_window": e["context_window"],
            "max_output_tokens": e.get("max_output_tokens"), "layer": e["layer"],
            "verified_on": e.get("verified_on"), "source_url": e.get("source_url")}


def cmd_list(args, store=None, engine=None, stack=None) -> int:
    table, rc = _load()
    if table is None:
        return rc
    rows = [_row(n, e) for n, e in table.models.items() if not args.provider or e["provider"] == args.provider]
    if args.json:
        _out(json.dumps({"models": rows, "generated_at": table.generated_at, "notice": table.notice}, indent=2))
        return 0
    if not rows:
        providers = sorted({e["provider"] for e in table.models.values()})
        _err(f"No models for provider {args.provider!r}. Providers: {', '.join(providers)}")
        return 1
    _out(f"{'MODEL':<26} {'PROVIDER':<10} {'IN $/MTok':>10} {'OUT $/MTok':>11}  {'LAYER':<9} VERIFIED")
    for r in rows:
        _out(f"{r['model']:<26} {r['provider']:<10} {r['input_usd_per_mtok']:>10} {r['output_usd_per_mtok']:>11}  "
             f"{r['layer']:<9} {r['verified_on'] or 'no'}")
    unv = sum(1 for r in rows if not r["verified_on"])
    _out(f"\n{len(rows)} models, {unv} unverified. Next: briefcase-ai pricing check --records FILE")
    return 0


def _layer_path(table: pr.PricingTable, layer: str) -> str:
    for lay in table.layers:
        if lay.name == layer:
            return str(lay.path) if lay.path else "packaged with briefcase-ai"
    return layer


def cmd_show(args, store=None, engine=None, stack=None) -> int:
    table, rc = _load()
    if table is None:
        return rc
    resolved = table.resolve(args.model)
    if resolved is None:
        close = table.closest(args.model)
        hint = table.unpriced_hint(args.model)
        info = {"model": args.model, "priced": False, "known_unpriced": bool(hint), "closest": close,
                "add_command": SET_EXAMPLE.replace("MODEL", args.model)}
        if args.json:
            _out(json.dumps(info, indent=2))
            return 1
        _err(f"No price for '{args.model}'.")
        if hint:
            _err(f"It is a known model with no price shipped (provider {hint['provider']}); "
                 "add one with `briefcase-ai pricing set`.")
        if close:
            _err(f"Closest priced ids: {', '.join(close)}")
        _err(f"Add it:  {info['add_command']}")
        return 1
    e, how = resolved
    row = _row(e["model_name"], e)
    info = dict(row, priced=True, matched=how, queried=args.model, aliases=e.get("aliases") or [],
                layer_source=_layer_path(table, e["layer"]), origin=e.get("origin"),
                effective_from=e.get("effective_from"), has_rate_card_data=bool(e.get("rate_card_extras")))
    if args.json:
        _out(json.dumps(info, indent=2))
        return 0
    _out(f"{e['model_name']}  ({e['provider']})" + ("" if how == "exact" else f"   [{args.model} matched by {how}]"))
    _out(f"  input   ${row['input_usd_per_mtok']} per million tokens")
    _out(f"  output  ${row['output_usd_per_mtok']} per million tokens")
    cap = e.get("max_output_tokens")
    _out(f"  limits  context {e['context_window']:,}" + (f", max output {cap:,}" if cap else ", no output cap"))
    _out(f"  layer   {e['layer']} ({info['layer_source']})")
    _out(f"  source  {e.get('source_url') or 'none (unverified)'}")
    _out(f"  verified_on {e.get('verified_on') or 'never'}   effective_from {e.get('effective_from') or 'unknown'}   "
         f"origin {e.get('origin') or 'unknown'}")
    _out(f"  aliases {', '.join(info['aliases']) or 'none'}")
    if e.get("rate_card_extras"):
        _out("  extras  cache/long-context/fast-mode rates exist for this built-in; the pure-Python "
             "estimate refuses them (native extension needed)")
    if not e.get("verified_on"):
        _out("  note    this price has not been checked against a provider page")
    _out("\nNext: briefcase-ai pricing check --records FILE")
    return 0


# ---------------------------------------------------------------------------------------------
# Writing the user file
# ---------------------------------------------------------------------------------------------

def _read_user(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": pr.SCHEMA_VERSION, "models": []}
    return pr.parse_document(path.read_text(encoding="utf-8"), str(path))


def _dump(doc: Dict[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"{path} (current)", f"{path} (proposed)"))


def _apply_change(args, path: Path, new_doc: Dict[str, Any], title: str) -> int:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = _dump(new_doc)
    if old == new:
        _out("Nothing to change.")
        return 0
    _out(f"{title}\n")
    _out(_diff(old, new, path))
    if args.pricing_dry_run:
        _out("Dry run: nothing written.")
        return 0
    ok = _confirm(f"Write {path}?", args.yes)
    if ok is None:
        _err(f"Not writing without confirmation (no terminal). Re-run with --yes to write {path}.")
        return 1
    if not ok:
        _out("Cancelled; nothing written.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)
    _out(f"Wrote {path}")
    return 0


def _mtok_value(label: str, raw: str) -> float:
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number of USD per million tokens, got {raw!r}")
    if not d.is_finite() or d < 0:
        raise ValueError(f"{label} must be finite and not negative, got {raw!r}")
    return float(d) / 1000.0  # per-1K, the same division the Rust core uses for per-MTok literals


def cmd_set(args, store=None, engine=None, stack=None) -> int:
    table, rc = _load()
    if table is None:
        return rc
    if not args.source_url.startswith(("http://", "https://")):
        _err(f"--source-url must be an http(s) URL, got {args.source_url!r}. "
             "User prices carry provenance: link the provider page you read.")
        return 2
    try:
        in1k = _mtok_value("--input", args.input)
        out1k = _mtok_value("--output", args.output)
        cached = _mtok_value("--cached-input", args.cached_input) if args.cached_input is not None else None
    except ValueError as exc:
        _err(str(exc))
        return 2
    existing = table.models.get(args.model)
    ctx = args.context_window or (existing or {}).get("context_window")
    provider = args.provider or (existing or {}).get("provider")
    if not ctx or not provider:
        missing = [f for f, v in (("--context-window N", ctx), ("--provider NAME", provider)) if not v]
        _err(f"'{args.model}' is not in the table, so {' and '.join(missing)} is required. Run:\n"
             f"  {SET_EXAMPLE.replace('MODEL', args.model)}")
        return 2
    if ctx <= 0:
        _err("--context-window must be greater than 0.")
        return 2
    max_out = args.max_output_tokens
    if max_out is None:
        max_out = (existing or {}).get("max_output_tokens")
    entry: Dict[str, Any] = {
        "model_name": args.model, "provider": provider,
        "input_cost_per_1k_tokens": in1k, "output_cost_per_1k_tokens": out1k,
        "context_window": ctx, "max_output_tokens": max_out,
        "aliases": list((existing or {}).get("aliases") or []),
        "source_url": args.source_url, "effective_from": None, "verified_on": _today().isoformat(),
        "origin": "user:briefcase-ai pricing set",
    }
    if cached is not None:
        entry["rate_card_extras"] = {"cache_rates": {
            "read_per_mtok": float(Decimal(args.cached_input)),
            "write_5m_per_mtok": None, "write_1h_per_mtok": None}}
    path = pr.user_file()
    try:
        doc = _read_user(path)
    except pr.PricingError as exc:
        _err("\n".join(exc.errors))
        return 1
    doc["models"] = [m for m in doc["models"] if m["model_name"] != args.model] + [entry]
    rc = _apply_change(args, path, doc, f"Set {args.model}: input ${pr.mtok(in1k)}/MTok, output ${pr.mtok(out1k)}/MTok")
    if rc == 0 and not args.pricing_dry_run:
        _out(f"Next: briefcase-ai pricing show {args.model}")
    return rc


def cmd_unset(args, store=None, engine=None, stack=None) -> int:
    path = pr.user_file()
    try:
        doc = _read_user(path)
    except pr.PricingError as exc:
        _err("\n".join(exc.errors))
        return 1
    if not any(m["model_name"] == args.model for m in doc["models"]):
        _err(f"'{args.model}' is not in {path}. See what it holds: briefcase-ai pricing list")
        return 1
    doc["models"] = [m for m in doc["models"] if m["model_name"] != args.model]
    rc = _apply_change(args, path, doc, f"Remove {args.model} from the user file")
    if rc == 0 and not args.pricing_dry_run:
        _out("Next: briefcase-ai pricing check")
    return rc


def cmd_import(args, store=None, engine=None, stack=None) -> int:
    src = Path(args.file).expanduser()
    try:
        incoming = pr.load_layer("import", src)
    except pr.PricingError as exc:
        _err("Cannot import:")
        for line in exc.errors:
            _err(f"  {line}")
        return 1
    path = pr.user_file()
    try:
        doc = _read_user(path)
    except pr.PricingError as exc:
        _err("\n".join(exc.errors))
        return 1
    names = {m["model_name"] for m in incoming["models"]}
    replaced = sorted(names & {m["model_name"] for m in doc["models"]})
    doc["models"] = [m for m in doc["models"] if m["model_name"] not in names] + incoming["models"]
    try:  # the merged result must still be valid together with the other layers
        pr.parse_document(_dump(doc), str(path))
    except pr.PricingError as exc:
        _err("The merged user file would be invalid:")
        for line in exc.errors:
            _err(f"  {line}")
        return 1
    unsourced = sorted(m["model_name"] for m in incoming["models"] if not m.get("source_url"))
    if unsourced:
        _out(f"warning: {len(unsourced)} imported model(s) have no source_url and will show as unverified: "
             f"{', '.join(unsourced[:5])}{' ...' if len(unsourced) > 5 else ''}")
    rc = _apply_change(args, path, doc, f"Import {len(names)} model(s) from {src}"
                       + (f" (replacing {', '.join(replaced)})" if replaced else ""))
    if rc == 0 and not args.pricing_dry_run:
        _out("Next: briefcase-ai pricing check")
    return rc


# ---------------------------------------------------------------------------------------------
# check / status
# ---------------------------------------------------------------------------------------------

def _validate_layers() -> List[str]:
    errors: List[str] = []
    docs: List[Any] = []
    try:
        docs.append(("packaged", None, pr.parse_document(pr.packaged_text(), "briefcase/data/pricing/v1.json")))
    except pr.PricingError as exc:
        errors += exc.errors
    for name, p in pr.layer_files():
        if name == "user" and not p.exists():
            continue
        try:
            docs.append((name, p, pr.load_layer(name, p)))
        except pr.PricingError as exc:
            errors += exc.errors
    if not errors:
        try:
            pr.merge(docs)
        except pr.PricingError as exc:
            errors += exc.errors
    return errors


def cmd_check(args, store=None, engine=None, stack=None) -> int:
    errors = _validate_layers()
    report: Dict[str, Any] = {"errors": errors, "warnings": [], "unpriced": [], "layers": []}
    table = None
    if not errors:
        table = pr.load()
        report["layers"] = [{"layer": x.name, "path": str(x.path) if x.path else None, "models": len(x.models)}
                            for x in table.layers]
        today = _today()
        unv = table.unverified()
        if unv:
            report["warnings"].append(f"{len(unv)} of {len(table.models)} models have no provenance "
                                      "(source_url/verified_on are null): prices are unverified")
        age = table.age_days(today)
        if age is not None and age > args.max_age_days:
            report["warnings"].append(f"packaged data is {age} days old (limit {args.max_age_days})")
        stale = [n for n, e in table.models.items()
                 if (a := pr._age(e.get("verified_on"), today)) is not None and a > args.max_age_days]
        if stale:
            report["warnings"].append(f"{len(stale)} verified price(s) older than {args.max_age_days} days: "
                                      f"{', '.join(stale[:5])}")
        if args.records:
            try:
                counts, nrec, skipped = pr.record_models(args.records)
            except OSError as exc:
                _err(f"Cannot read records file {args.records}: {exc.strerror or exc}")
                return 1
            report["records"] = {"file": args.records, "records": nrec, "skipped_lines": skipped,
                                 "models": len(counts)}
            for model, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                if table.resolve(model) is None:
                    hint = table.unpriced_hint(model)
                    report["unpriced"].append({
                        "model": model, "records": n, "known_model": bool(hint),
                        "closest": table.closest(model),
                        "add_command": SET_EXAMPLE.replace("MODEL", model)})
            if report["unpriced"]:
                report["warnings"].append(f"{len(report['unpriced'])} model(s) in your records have no price")
    failed = bool(errors) or (args.strict and bool(report["warnings"]))
    if args.json:
        _out(json.dumps(report, indent=2))
        return 1 if failed else 0
    if errors:
        _err("Invalid pricing files:")
        for line in errors:
            _err(f"  {line}")
        _err("Fix those lines, or move the file aside and re-run `briefcase-ai pricing check`.")
        return 1
    for x in report["layers"]:
        _out(f"ok  {x['layer']:<9} {x['models']:>3} models  {x['path'] or 'packaged'}")
    for w in report["warnings"]:
        _out(f"warn {w}")
    if "records" in report:
        r = report["records"]
        _out(f"\nRecords: {r['records']} read from {r['file']} ({r['models']} distinct models"
             + (f", {r['skipped_lines']} unreadable lines skipped" if r["skipped_lines"] else "") + ")")
        if report["unpriced"]:
            _out("Models in your records with NO price:")
            for u in report["unpriced"]:
                _out(f"  {u['model']}  ({u['records']} records)"
                     + ("  [known model, no price shipped]" if u["known_model"] else ""))
                if u["closest"]:
                    _out(f"      closest priced: {', '.join(u['closest'])}")
                _out(f"      {u['add_command']}")
        else:
            _out("Every model in your records has a price.")
    if not report["warnings"]:
        _out("No problems found.")
    first = report["unpriced"][0]["model"] if report["unpriced"] else None
    _out("Next: " + (f"briefcase-ai pricing set {first} --input ... --output ... --source-url URL"
                     if first else "briefcase-ai pricing list"))
    return 1 if failed else 0


def status() -> Dict[str, Any]:
    """Hook for `briefcase-ai status`."""
    base = {"id": "pricing-data", "title": "Pricing data", "next": "briefcase-ai pricing check"}
    try:
        table = pr.load()
    except pr.PricingError as exc:
        return dict(base, state="attention", summary=f"pricing file invalid: {exc.errors[0]}")
    age = table.age_days(_today())
    summary = (f"{len(table.models)} models priced, {len(table.unverified())} unverified, "
               f"data is {age if age is not None else '?'} days old")
    unpriced: List[str] = []
    records = pr.briefcase_home() / "receiver" / "records.jsonl"
    if records.exists():
        try:
            counts, _, _ = pr.record_models(records)
            unpriced = sorted(m for m in counts if table.resolve(m) is None)
        except OSError:
            pass
    if unpriced:
        return dict(base, state="attention", unpriced=unpriced,
                    summary=f"{summary}; {len(unpriced)} model(s) in your records have no price: "
                            f"{', '.join(unpriced[:3])}",
                    next=f"briefcase-ai pricing check --records {records}")
    return dict(base, state="ok", summary=summary, unpriced=[])
