#!/usr/bin/env python3
"""Extract the built-in price table from ``crates/briefcase-core/src/cost.rs`` into JSON.

The table is Rust source with two literal shapes, both parsed here without evaluating
anything:

* ``pricing_table.insert("name".to_string(), ModelPricing { ... })`` (per-1K prices), and
* ``self.insert_model(ModelPricing::from_mtok(name, provider, in, out, ctx, max), PricingExtras {...})``
  (per-MTok prices, divided by 1000.0 exactly as ``ModelPricing::from_mtok`` does).

Anything the parser does not recognise is an error, never a skipped model: the model count is
checked against the number of insert statements, and every field of ``PricingExtras`` must be
one this script knows. Exactness is proven by ``tests/pricing/test_extract_exact.py`` (and,
where cargo is available offline, against ``scripts/pricing/native_oracle``, which dumps the
table through the core's own types).

    python scripts/pricing/extract.py --generated-at 2026-11-21 [--out briefcase/data/pricing/v1.json]

Prices are never edited here. To change a price, edit the JSON (with a source URL) and see
``docs/features/pricing-data.md``.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
COST_RS = ROOT / "crates" / "briefcase-core" / "src" / "cost.rs"
DEFAULT_OUT = ROOT / "briefcase" / "data" / "pricing" / "v1.json"
SCHEMA_VERSION = 1

NOTICE = (
    "UNVERIFIED. These prices were copied mechanically from the hardcoded table in "
    "crates/briefcase-core/src/cost.rs. Nobody has checked them against provider price pages, "
    "so source_url, effective_from and verified_on are null on every model. Treat them as "
    "estimates; set a verified price with `briefcase-ai pricing set MODEL ... --source-url URL`."
)

EXTRAS_FIELDS = {
    "cache_rates", "fast_mode_rates", "long_context_tiers", "priority_multiplier",
    "cache_storage_per_mtok_hour", "flags",
}


class ExtractError(Exception):
    pass


def strip_comments(src: str) -> str:
    out = []
    for line in src.splitlines():
        # No string literal in the price table contains "//", so this is safe there.
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def num(tok: str) -> float:
    return float(tok.replace("_", ""))


def integer(tok: str) -> int:
    return int(tok.replace("_", ""))


def balanced(text: str, open_idx: int) -> int:
    """Index just past the bracket matching the one at ``open_idx``."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack: List[str] = []
    in_str = False
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == '"':
            in_str = not in_str
        if in_str:
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
    raise ExtractError("unbalanced brackets in cost.rs")


def split_args(s: str) -> List[str]:
    args, depth, cur = [], 0, []
    for c in s:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def opt_int(tok: str) -> Optional[int]:
    tok = tok.strip()
    if tok == "None":
        return None
    m = re.fullmatch(r"Some\(([\d_]+)\)", tok)
    if not m:
        raise ExtractError(f"unrecognised Option<usize> literal {tok!r}")
    return integer(m.group(1))


def opt_float(tok: str) -> Optional[float]:
    m = re.fullmatch(r"Some\(([\d_.]+)\)", tok.strip())
    if not m:
        raise ExtractError(f"unrecognised Option<f64> literal {tok!r}")
    return num(m.group(1))


def call_args(tok: str, name: str) -> List[str]:
    tok = tok.strip()
    if not tok.startswith(name + "("):
        raise ExtractError(f"expected {name}(...), got {tok!r}")
    return split_args(tok[len(name) + 1:tok.rindex(")")])


def parse_extras(body: str) -> Optional[Dict[str, Any]]:
    """Parse the inside of ``PricingExtras { ... }`` (per-MTok units, as written)."""
    body = body.strip().rstrip(",")
    body = re.sub(r"\.\.\s*Default::default\(\)\s*$", "", body).strip().rstrip(",")
    if not body:
        return None
    fields: Dict[str, str] = {}
    for part in split_args(body):
        key, _, value = part.partition(":")
        key = key.strip()
        if key not in EXTRAS_FIELDS:
            raise ExtractError(f"unknown PricingExtras field {key!r}")
        fields[key] = value.strip()
    out: Dict[str, Any] = {}
    if "cache_rates" in fields:
        v = re.fullmatch(r"Some\((.*)\)", fields["cache_rates"], re.S).group(1)
        if v.startswith("CacheRates::from_mtok"):
            r, w5, w1 = (num(a) for a in call_args(v, "CacheRates::from_mtok"))
            out["cache_rates"] = {"read_per_mtok": r, "write_5m_per_mtok": w5, "write_1h_per_mtok": w1}
        else:
            (r,) = (num(a) for a in call_args(v, "CacheRates::read_only_mtok"))
            out["cache_rates"] = {"read_per_mtok": r, "write_5m_per_mtok": None, "write_1h_per_mtok": None}
    if "fast_mode_rates" in fields:
        v = re.fullmatch(r"Some\((.*)\)", fields["fast_mode_rates"], re.S).group(1)
        i, o = (num(a) for a in call_args(v, "TokenRates::from_mtok"))
        out["fast_mode_rates"] = {"input_per_mtok": i, "output_per_mtok": o}
    if "long_context_tiers" in fields:
        v = re.fullmatch(r"vec!\[(.*)\]", fields["long_context_tiers"], re.S).group(1)
        tiers = []
        for item in split_args(v):
            t, i, o = call_args(item, "ContextTier::from_mtok")
            tiers.append({"threshold_tokens": integer(t), "input_per_mtok": num(i), "output_per_mtok": num(o)})
        out["long_context_tiers"] = tiers
    if "priority_multiplier" in fields:
        out["priority_multiplier"] = opt_float(fields["priority_multiplier"])
    if "cache_storage_per_mtok_hour" in fields:
        out["cache_storage_per_mtok_hour"] = opt_float(fields["cache_storage_per_mtok_hour"])
    if "flags" in fields:
        m = re.fullmatch(r"ModifierFlags\s*\{(.*)\}", fields["flags"], re.S)
        flags = {}
        for part in split_args(m.group(1)):
            k, _, v = part.partition(":")
            if k.strip() not in ("supports_data_residency", "supports_regional_premium") \
                    or v.strip() not in ("true", "false"):
                raise ExtractError(f"unrecognised ModifierFlags entry {part!r}")
            flags[k.strip()] = v.strip() == "true"
        out["flags"] = flags
    return out or None


def parse_cost_rs(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Return (models in source order, alias->canonical map from ``normalize_model_id``)."""
    code = strip_comments(text.split("#[cfg(test)]")[0])
    models: List[Dict[str, Any]] = []

    # Legacy per-1K struct literals.
    legacy = 0
    for m in re.finditer(r'pricing_table\.insert\(\s*"([^"]+)"\.to_string\(\),\s*ModelPricing\s*\{', code):
        legacy += 1
        start = code.index("{", m.end() - 1)
        body = code[start + 1:balanced(code, start) - 1]
        f = {k.strip(): v.strip() for k, _, v in (p.partition(":") for p in split_args(body))}
        name = re.fullmatch(r'"([^"]+)"\.to_string\(\)', f["model_name"]).group(1)
        if name != m.group(1):
            raise ExtractError(f"key {m.group(1)!r} differs from model_name {name!r}")
        models.append({
            "model_name": name,
            "provider": re.fullmatch(r'"([^"]+)"\.to_string\(\)', f["provider"]).group(1),
            "input_cost_per_1k_tokens": num(f["input_cost_per_1k_tokens"]),
            "output_cost_per_1k_tokens": num(f["output_cost_per_1k_tokens"]),
            "context_window": integer(f["context_window"]),
            "max_output_tokens": opt_int(f["max_output_tokens"]),
            "rate_card_extras": None,
        })
    if legacy != len(re.findall(r'pricing_table\.insert\(\s*"', code)):
        raise ExtractError("a legacy pricing_table.insert was not recognised")

    # Modern from_mtok models with their extras.
    modern = 0
    for m in re.finditer(r"self\.insert_model\(", code):
        modern += 1
        end = balanced(code, m.end() - 1)
        args = split_args(code[m.end():end - 1])
        if len(args) != 2:
            raise ExtractError("insert_model expects (ModelPricing, PricingExtras)")
        a = call_args(args[0], "ModelPricing::from_mtok")
        if len(a) != 6:
            raise ExtractError(f"from_mtok expects 6 arguments, got {a}")
        name = re.fullmatch(r'"([^"]+)"', a[0]).group(1)
        # Same operation as `input_per_mtok / 1000.0` in Rust: IEEE double division.
        models.append({
            "model_name": name,
            "provider": re.fullmatch(r'"([^"]+)"', a[1]).group(1),
            "input_cost_per_1k_tokens": num(a[2]) / 1000.0,
            "output_cost_per_1k_tokens": num(a[3]) / 1000.0,
            "context_window": integer(a[4]),
            "max_output_tokens": opt_int(a[5]),
            "rate_card_extras": None,
        })
        ex = args[1].strip()
        if ex == "PricingExtras::default()":
            pass
        else:
            mm = re.fullmatch(r"PricingExtras\s*\{(.*)\}", ex, re.S)
            if not mm:
                raise ExtractError(f"unrecognised PricingExtras for {name}: {ex[:60]!r}")
            models[-1]["rate_card_extras"] = parse_extras(mm.group(1))
    if modern != code.count("self.insert_model("):
        # the definition `fn insert_model(` has no `self.` prefix, so counts must match
        raise ExtractError("an insert_model call was not recognised")

    names = [m["model_name"] for m in models]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ExtractError(f"duplicate model names in cost.rs: {sorted(dupes)}")

    aliases: Dict[str, str] = {}
    for m in re.finditer(r'"([^"]+)"\s*=>\s*"([^"]+)"\.to_string\(\)', code):
        aliases[m.group(1)] = m.group(2)
    return models, aliases


def cost_rs_sha(path: Path) -> str:
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%H", "--", str(path)], cwd=path.parent,
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()[:12] or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_document(cost_rs: Path, generated_at: str, sha: Optional[str] = None) -> Dict[str, Any]:
    models, aliases = parse_cost_rs(cost_rs.read_text(encoding="utf-8"))
    sha = sha or cost_rs_sha(cost_rs)
    by_name = {m["model_name"]: m for m in models}
    for alias, target in aliases.items():
        if target not in by_name:
            raise ExtractError(f"alias {alias!r} points at {target!r}, which is not in the table")
    out_models = []
    for m in models:
        out_models.append({
            "model_name": m["model_name"],
            "provider": m["provider"],
            "input_cost_per_1k_tokens": m["input_cost_per_1k_tokens"],
            "output_cost_per_1k_tokens": m["output_cost_per_1k_tokens"],
            "context_window": m["context_window"],
            "max_output_tokens": m["max_output_tokens"],
            "aliases": sorted(a for a, t in aliases.items() if t == m["model_name"]),
            "rate_card_extras": m["rate_card_extras"],
            "source_url": None,
            "effective_from": None,
            "verified_on": None,
            "origin": f"builtin-table@{sha}",
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "notice": NOTICE,
        "units": "USD per 1,000 tokens (input_cost_per_1k_tokens, output_cost_per_1k_tokens); "
                 "rate_card_extras fields named *_per_mtok are USD per 1,000,000 tokens",
        "models": out_models,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--generated-at", required=True, help="ISO date written into the file (never read from the clock).")
    ap.add_argument("--cost-rs", type=Path, default=COST_RS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--sha", default=None, help="Override the cost.rs revision recorded in `origin`.")
    args = ap.parse_args(argv)
    try:
        doc = build_document(args.cost_rs, args.generated_at, args.sha)
    except ExtractError as exc:
        print(f"extract failed: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(doc['models'])} models to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
