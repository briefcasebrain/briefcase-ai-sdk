"""``briefcase-ai budget``: hard and soft spending limits on the LLM calls your app makes.

    briefcase-ai budget set daily --usd 20 --soft-usd 15      # write ~/.briefcase/budgets.toml
    briefcase-ai budget test --model gpt-4o --input-tokens 2000 --max-tokens 500
    briefcase-ai budget status                                 # spend vs limit, time left
    briefcase-ai budget unset daily                            # undo

Budgets are enforced inside your app (``briefcase.budgets.enable()`` reads the file).
``$BRIEFCASE_BUDGETS_FILE`` points at another file; ``$BRIEFCASE_HOME`` moves ~/.briefcase.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from briefcase.cli import _ux

GROUP = "enforce"
ORDER = 40

SET_EXAMPLE = "example: briefcase-ai budget set daily --usd 20 --soft-usd 15 --window day"
TEST_EXAMPLE = "example: briefcase-ai budget test --model gpt-4o --input-tokens 2000 --max-tokens 500"


def register(sub) -> None:
    fmt = argparse.RawDescriptionHelpFormatter
    p = sub.add_parser(
        "budget", help="Spending limits on LLM calls: set, test, see spend vs limit.",
        description="Hard and soft spending limits on the LLM calls your app makes, enforced "
                    "in-process before each call (apps call briefcase.budgets.enable()). File: "
                    "~/.briefcase/budgets.toml or $BRIEFCASE_BUDGETS_FILE.",
        epilog=SET_EXAMPLE, formatter_class=fmt)
    p.set_defaults(func=cmd_budget, budget_parser=p)
    bs = p.add_subparsers(dest="budget_cmd")

    sp = bs.add_parser("set", help="Add or change one budget in budgets.toml.",
                       description="Add or change one budget. Windows are calendar windows in "
                                   "UTC (hour, day, month) or total.",
                       epilog=SET_EXAMPLE, formatter_class=fmt)
    sp.add_argument("name", help="Budget name (letters, digits, '-', '_').")
    sp.add_argument("--usd", type=float, help="Hard limit in USD per window.")
    sp.add_argument("--soft-usd", type=float, help="Soft limit in USD: log and mark records, allow.")
    sp.add_argument("--tokens", type=int, help="Hard limit in tokens (input + output) per window.")
    sp.add_argument("--soft-tokens", type=int, help="Soft limit in tokens.")
    sp.add_argument("--window", choices=("hour", "day", "month", "total"),
                    help="Window (default day).")
    sp.add_argument("--per", help="Count separately per 'model' or per scope key "
                                  "(tenant, user, session, key) set with budget_scope().")
    sp.add_argument("--model", action="append", dest="models", metavar="PATTERN",
                    help="Only calls to models matching this pattern (repeatable, fnmatch).")
    sp.add_argument("--where", action="append", metavar="KEY=VALUE",
                    help="Only calls whose scope has this value (repeatable).")
    sp.add_argument("--on-exceed", choices=("block", "warn", "degrade"),
                    help="At the hard limit: block (default), warn, or degrade to --degrade-to.")
    sp.add_argument("--degrade-to", metavar="MODEL", help="Model to switch to when degrading.")
    sp.add_argument("--store", help="Counter store for all budgets: memory, redis://..., "
                                    "sqlite:///path, postgresql://..., mysql://...")
    sp.add_argument("--unpriced", choices=("block", "allow", "warn"),
                    help="Calls to models with no price (default warn).")
    sp.add_argument("--dry-run", dest="budget_dry_run", action="store_true",
                    help="Show the change; write nothing.")
    sp.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    sp.set_defaults(func=cmd_set)

    sp = bs.add_parser("unset", help="Remove one budget from budgets.toml (undo for set).",
                       epilog="example: briefcase-ai budget unset daily", formatter_class=fmt)
    sp.add_argument("name")
    sp.add_argument("--dry-run", dest="budget_dry_run", action="store_true")
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_unset)

    sp = bs.add_parser("show", help="The budgets and settings in budgets.toml.",
                       epilog="example: briefcase-ai budget show --json", formatter_class=fmt)
    _ux.add_json_flag(sp)
    sp.set_defaults(func=cmd_show)

    sp = bs.add_parser("status", help="Spend vs limit per budget and scope, with time left.",
                       description="Reads the counters from the store named in budgets.toml. "
                                   "With the default in-process store, spend is only visible "
                                   "inside the app (briefcase.budgets.status()).",
                       epilog="example: briefcase-ai budget status", formatter_class=fmt)
    _ux.add_json_flag(sp)
    sp.set_defaults(func=cmd_status)

    sp = bs.add_parser("test", help="Dry-run one call against the budgets and explain the decision.",
                       description="Nothing is reserved or written. Uses the current counters "
                                   "from the configured store.",
                       epilog=TEST_EXAMPLE, formatter_class=fmt)
    sp.add_argument("--model", required=True)
    sp.add_argument("--input-tokens", type=int, required=True)
    sp.add_argument("--max-tokens", type=int, help="Output cap (default: the model's maximum).")
    sp.add_argument("--scope", action="append", metavar="KEY=VALUE",
                    help="Scope values, e.g. tenant=acme (repeatable).")
    _ux.add_json_flag(sp)
    sp.set_defaults(func=cmd_test)


# ---------------------------------------------------------------------------------------------

def _config():
    from briefcase.budgets import config

    return config


def _read() -> Any:
    """``(data, None)`` or ``(None, exit code)``."""
    config = _config()
    try:
        return config.read(), None
    except config.ConfigError as exc:
        return None, _ux.fail(str(exc), fix=f"edit {config.default_path()} or "
                                            "briefcase-ai budget unset NAME")


def _pairs(items: Optional[List[str]], flag: str) -> Optional[Dict[str, str]]:
    out: Dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"{flag} expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _inside_home(path: Path) -> bool:
    from briefcase.pricing import briefcase_home

    try:
        path.resolve().relative_to(briefcase_home().resolve())
        return True
    except ValueError:
        return False


def _write(data: Dict[str, Any], args: Any, verb: str) -> int:
    config = _config()
    path = config.default_path()
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        config.validate(data)
    except config.ConfigError as exc:
        return _ux.fail(str(exc), code=_ux.EXIT_USAGE)
    new = config.dumps(data)
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        str(path) if old else "/dev/null", str(path)))
    if not diff:
        print(f"{path} already says that; nothing to change.")
        return 0
    print(diff, end="" if diff.endswith("\n") else "\n")
    if getattr(args, "budget_dry_run", False):
        print("Dry run: nothing written.")
        return 0
    if not _inside_home(path):
        answer = _ux.confirm(f"Write {path}?", assume_yes=args.yes)
        if answer is None:
            return _ux.fail(f"{path} is outside ~/.briefcase; not writing without confirmation",
                            fix="briefcase " + " ".join(sys.argv[1:] + ["--yes"]))
        if not answer:
            print("Nothing written.")
            return 1
    config.write(data, path)
    print(f"{verb} {path}")
    return 0


def cmd_budget(args, store=None, engine=None, stack=None) -> int:
    args.budget_parser.print_help()
    _ux.print_next("briefcase-ai budget set daily --usd 20", "add a first budget")
    return 0


def cmd_set(args, store=None, engine=None, stack=None) -> int:
    data, code = _read()
    if data is None:
        return code
    data = dict(data)
    budgets = dict(data.get("budgets") or {})
    entry = dict(budgets.get(args.name) or {})
    try:
        where = _pairs(args.where, "--where")
    except ValueError as exc:
        return _ux.fail(str(exc), code=_ux.EXIT_USAGE)
    for key, value in (("usd", args.usd), ("soft_usd", args.soft_usd), ("tokens", args.tokens),
                       ("soft_tokens", args.soft_tokens), ("window", args.window),
                       ("per", args.per), ("models", args.models), ("where", where or None),
                       ("on_exceed", args.on_exceed), ("degrade_to", args.degrade_to)):
        if value is not None:
            entry[key] = value
    entry.setdefault("window", "day")
    budgets[args.name] = entry
    data["budgets"] = budgets
    if args.store is not None:
        data["store"] = args.store
    if args.unpriced is not None:
        data["unpriced"] = args.unpriced
    code = _write(data, args, "Wrote")
    if code == 0 and not args.budget_dry_run:
        print("Apps enforce it after calling briefcase.budgets.enable() (and briefcase.auto()).")
        _ux.print_next("briefcase-ai budget test --model gpt-4o --input-tokens 1000 --max-tokens 500",
                       "see what a call would do")
    return code


def cmd_unset(args, store=None, engine=None, stack=None) -> int:
    data, code = _read()
    if data is None:
        return code
    budgets = dict(data.get("budgets") or {})
    if args.name not in budgets:
        names = ", ".join(budgets) or "none"
        return _ux.fail(f"no budget named {args.name!r} (budgets: {names})",
                        fix="briefcase-ai budget show")
    budgets.pop(args.name)
    data = dict(data, budgets=budgets)
    code = _write(data, args, "Updated")
    if code == 0 and not args.budget_dry_run:
        _ux.print_next("briefcase-ai budget show")
    return code


def cmd_show(args, store=None, engine=None, stack=None) -> int:
    data, code = _read()
    if data is None:
        return code
    path = _config().default_path()
    if args.json:
        return _ux.print_json({"path": str(path), "exists": path.exists(), **data})
    if not data.get("budgets"):
        print(f"No budgets ({path} {'has none' if path.exists() else 'does not exist'}).")
        _ux.print_next("briefcase-ai budget set daily --usd 20", "add a first budget")
        return 0
    print(f"{path}")
    print(f"store: {data.get('store', 'memory')}   unpriced models: {data.get('unpriced', 'warn')}")
    for name, spec in data["budgets"].items():
        print(f"  {name}: {_describe(spec)}")
    _ux.print_next("briefcase-ai budget status")
    return 0


def _describe(spec: Dict[str, Any]) -> str:
    parts = []
    if spec.get("usd") is not None:
        parts.append(f"${spec['usd']:g}")
    if spec.get("tokens") is not None:
        parts.append(f"{spec['tokens']} tokens")
    text = " and ".join(parts) or "no hard limit"
    text += f" per {spec.get('window', 'day')}"
    if spec.get("per"):
        text += f" per {spec['per']}"
    soft = [f"${spec['soft_usd']:g}" if spec.get("soft_usd") is not None else None,
            f"{spec['soft_tokens']} tokens" if spec.get("soft_tokens") is not None else None]
    if any(soft):
        text += ", soft " + " / ".join(s for s in soft if s)
    if spec.get("models"):
        text += ", models " + ",".join(spec["models"])
    if spec.get("where"):
        text += ", where " + ",".join(f"{k}={v}" for k, v in spec["where"].items())
    effect = spec.get("on_exceed", "block")
    text += f", on exceed: {effect}"
    if effect == "degrade":
        text += f" to {spec.get('degrade_to')}"
    return text


def _engine(data: Dict[str, Any], quick: bool = False) -> Any:
    from briefcase.budgets import Budgets, config, store_from_url

    url = data.get("store") or "memory"
    if quick and url.startswith("sqlite:///"):
        # Read-only for the status hook: never create the file or the table.
        import os
        import sqlite3

        from briefcase.budgets import SQLBudgetStore

        path = os.path.expanduser(url[len("sqlite:///"):])
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        store = SQLBudgetStore(lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                                       timeout=0.5), "sqlite", create=False)
    else:
        store = store_from_url(url, timeout=0.5 if quick else None)
    return Budgets(config.budgets_from(data), store=store, unpriced=data.get("unpriced", "warn"),
                   default_max_tokens=data.get("default_max_tokens", 4096)), url


def _left(seconds: Optional[int]) -> str:
    if seconds is None:
        return "never resets"
    hours, rest = divmod(max(0, seconds), 3600)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h left"
    return f"{hours}h {rest // 60}m left"


def cmd_status(args, store=None, engine=None, stack=None) -> int:
    data, code = _read()
    if data is None:
        return code
    if not data.get("budgets"):
        if args.json:
            return _ux.print_json({"budgets": [], "store": data.get("store", "memory")})
        print("No budgets are configured.")
        _ux.print_next("briefcase-ai budget set daily --usd 20", "add a first budget")
        return 0
    try:
        eng, url = _engine(data)
        rows = eng.status()
    except Exception as exc:
        return _ux.fail(f"could not read the budget store {data.get('store')!r}: "
                        f"{type(exc).__name__}: {exc}", fix="briefcase-ai budget show")
    memory = url == "memory"
    if args.json:
        return _ux.print_json({"store": url, "process_local": memory, "budgets": rows})
    if memory:
        print("store: memory - counters live inside each app process, so this shows the limits "
              "only. In the app: briefcase.budgets.status(). For a shared view: "
              "briefcase-ai budget set NAME --store sqlite:///$HOME/.briefcase/budgets.db")
    else:
        print(f"store: {eng.store.describe()}")
    print(f"{'BUDGET':<16} {'SCOPE':<22} {'WINDOW':<12} {'SPENT':>22} {'%':>6}  RESETS")
    for row in rows:
        spent = []
        if row["limit_usd"] is not None or row["used_usd"]:
            limit = f"${row['limit_usd']:g}" if row["limit_usd"] is not None else "-"
            spent.append(f"${row['used_usd']:.4f}/{limit}")
        if row["limit_tokens"] is not None:
            spent.append(f"{row['used_tokens']}/{row['limit_tokens']} tok")
        pct = "-" if row["percent"] is None or memory else f"{row['percent']:.1f}"
        shown = "(in app)" if memory else " ".join(spent)
        print(f"{row['budget']:<16} {row['scope']:<22} {row['window_id']:<12} {shown:>22} "
              f"{pct:>6}  {_left(row['seconds_left'])}")
        if row["inflight_usd"] and not memory:
            print(f"{'':<16} in flight: ${row['inflight_usd']:.4f} reserved by calls not yet settled")
    over = [r for r in rows if r["percent"] is not None and r["percent"] >= 80 and not memory]
    if over:
        _ux.print_next("briefcase-ai budget show",
                       f"{len(over)} budget(s) at 80% or more: raise a limit with budget set")
    else:
        _ux.print_next("briefcase-ai budget test --model gpt-4o --input-tokens 1000 --max-tokens 500")
    return 0


def cmd_test(args, store=None, engine=None, stack=None) -> int:
    data, code = _read()
    if data is None:
        return code
    try:
        scope = _pairs(args.scope, "--scope") or {}
    except ValueError as exc:
        return _ux.fail(str(exc), code=_ux.EXIT_USAGE)
    if args.input_tokens < 0 or (args.max_tokens is not None and args.max_tokens < 0):
        return _ux.fail("token counts must not be negative", code=_ux.EXIT_USAGE)
    try:
        eng, url = _engine(data)
        res = eng.reserve(args.model, args.input_tokens, args.max_tokens, scope=scope,
                          dry_run=True)
    except Exception as exc:
        return _ux.fail(f"could not evaluate: {type(exc).__name__}: {exc}",
                        fix="briefcase-ai budget show")
    from briefcase.budgets._core import reserve_cost

    entry = eng.price_entry(args.model)
    max_out = eng.max_output_for(args.model, args.max_tokens)
    result = {
        "model": args.model, "decided_model": res.model, "scope": scope,
        "input_tokens": args.input_tokens, "max_output_tokens": max_out,
        "priced": entry is not None,
        "reserve_usd": reserve_cost(entry, args.input_tokens, max_out) / 1e9 if entry else None,
        "decided_reserve_usd": res.nano / 1e9 if res.priced else None,
        "price_per_mtok": ({"input": entry["input_cost_per_1k_tokens"] * 1000,
                            "output": entry["output_cost_per_1k_tokens"] * 1000}
                           if entry else None),
        "decision": "block" if res.effect == "block" else (
            "allow" if res.effect in ("allow", "unpriced") else res.effect),
        "store": url, "budgets": [], "reasons": res.reasons,
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    names = {h.budget.name for h in res.holds}
    for budget in eng.budgets:
        hold = next((h for h in res.holds if h.budget.name == budget.name), None)
        item: Dict[str, Any] = {"budget": budget.name, "applies": budget.name in names}
        if hold is not None:
            item.update(scope=hold.scope, window=hold.window, effect=hold.effect,
                        used_usd_after=hold.used_nano / 1e9, limit_usd=budget.usd,
                        used_tokens_after=hold.used_tokens, limit_tokens=budget.tokens)
        else:
            item["why_not"] = _why_not(budget, args.model, scope)
        result["budgets"].append(item)
    if args.json:
        _ux.print_json(result)
        return 0
    print(f"call: {args.model}, {args.input_tokens} input tokens, up to {max_out} "
          f"output tokens{' (model maximum)' if args.max_tokens is None else ''}"
          + (f", scope {', '.join(f'{k}={v}' for k, v in scope.items())}" if scope else ""))
    if entry:
        print(f"price: ${result['price_per_mtok']['input']:g} in / "
              f"${result['price_per_mtok']['output']:g} out per million tokens; "
              f"reserve ${result['reserve_usd']:.6f} before sending")
    else:
        print(f"price: none for {args.model} (unpriced={eng.unpriced}); USD limits cannot count it")
    if url == "memory":
        print("counters: memory store, so every budget starts at 0 here")
    for item in result["budgets"]:
        if not item["applies"]:
            print(f"  {item['budget']}: does not apply ({item['why_not']})")
            continue
        limit = f"${item['limit_usd']:g}" if item["limit_usd"] is not None else "no USD limit"
        print(f"  {item['budget']} [{item['scope']}, {item['window']}]: "
              f"${item['used_usd_after']:.6f} of {limit} after this call -> {item['effect']}")
    for reason in res.reasons:
        print(f"  note: {reason}")
    decision = result["decision"]
    if decision == "degrade":
        reserve = result["decided_reserve_usd"]
        print(f"decision: DEGRADE to {res.model}"
              + (f" (reserve ${reserve:.6f})" if reserve is not None else ""))
    else:
        print(f"decision: {decision.upper()}")
    _ux.print_next("briefcase-ai budget status")
    return 0


def _why_not(budget: Any, model: str, scope: Dict[str, str]) -> str:
    if budget.models and not any(__import__("fnmatch").fnmatchcase(model, p)
                                 for p in budget.models):
        return f"model not in {', '.join(budget.models)}"
    missing = {k: v for k, v in budget.where.items() if scope.get(k) != v}
    if missing:
        return "scope needs " + ", ".join(f"{k}={v}" for k, v in missing.items())
    return "not evaluated: an earlier budget decided the call"


# --- status hook ------------------------------------------------------------------------------

def status() -> Optional[Dict[str, Any]]:
    config = _config()
    path = config.default_path()
    if not path.exists():
        return {"id": "budgets", "title": "Budgets", "state": "off",
                "detail": "no spending limits set", "next": "briefcase-ai budget set daily --usd 20"}
    try:
        data = config.read(path)
    except config.ConfigError as exc:
        return {"id": "budgets", "title": "Budgets", "state": "attention",
                "detail": f"budgets.toml is invalid: {str(exc)[:120]}", "next": "briefcase-ai budget show"}
    count = len(data.get("budgets") or {})
    if not count:
        return {"id": "budgets", "title": "Budgets", "state": "off",
                "detail": "budgets.toml has no budget", "next": "briefcase-ai budget set daily --usd 20"}
    url = str(data.get("store") or "memory")
    detail = f"{count} budget{'s' if count != 1 else ''}, {url.split(':')[0]} store; " \
             "enforced in apps that call briefcase.budgets.enable()"
    rows: List[Dict[str, Any]] = []
    if url.startswith("sqlite:///") or _local_redis(url):
        try:
            eng, _ = _engine(data, quick=True)
            rows = eng.status()
        except Exception:
            rows = []
    over = [r for r in rows if r["percent"] is not None and r["percent"] > 80]
    if over:
        worst = max(over, key=lambda r: r["percent"])
        return {"id": "budgets", "title": "Budgets", "state": "attention",
                "detail": f"{worst['budget']} ({worst['scope']}) at {worst['percent']:.1f}% of its "
                          f"limit; {len(over)} over 80%",
                "next": "briefcase-ai budget status", "data": {"over_80": over}}
    return {"id": "budgets", "title": "Budgets", "state": "on", "detail": detail, "next": None}


def _local_redis(url: str) -> bool:
    from urllib.parse import urlparse

    if not url.startswith("redis://"):
        return False
    host = urlparse(url).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1"):
        return False
    try:
        import redis  # noqa: F401
    except ImportError:
        return False
    return True
