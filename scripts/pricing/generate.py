#!/usr/bin/env python3
"""Verify that cost.rs and pricing.ts still agree with briefcase/data/pricing/v1.json.

    python scripts/pricing/generate.py --check

The JSON is the reviewed source of truth. TODAY this script only VERIFIES consistency and prints
what it would regenerate; it never rewrites crates/ or js/ (those trees are owned elsewhere).
Exit 0 when consistent, 1 when not.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
JSON_PATH = ROOT / "briefcase" / "data" / "pricing" / "v1.json"
TS_PATH = ROOT / "js" / "controls" / "src" / "bedrock" / "pricing.ts"


def mtok(per_1k: float) -> Decimal:
    return Decimal(f"{per_1k * 1000:.10g}")


def check() -> list:
    problems = []
    doc = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    by_name = {m["model_name"]: m for m in doc["models"]}
    rs_models, aliases = extract.parse_cost_rs(extract.COST_RS.read_text(encoding="utf-8"))
    rs = {m["model_name"]: m for m in rs_models}
    for name in sorted(set(by_name) | set(rs)):
        if name not in rs:
            problems.append(f"cost.rs: would ADD {name} (in JSON, missing from the Rust table)")
        elif name not in by_name:
            problems.append(f"cost.rs: has {name}, which is not in the JSON (would be REMOVED)")
        else:
            for key in ("provider", "input_cost_per_1k_tokens", "output_cost_per_1k_tokens", "context_window",
                        "max_output_tokens", "rate_card_extras"):
                if by_name[name][key] != rs[name][key]:
                    problems.append(f"cost.rs: would UPDATE {name}.{key}: {rs[name][key]!r} -> {by_name[name][key]!r}")
    rows = re.findall(r'\{\s*model:\s*"([^"]+)",\s*input:\s*([\d._]+),\s*output:\s*([\d._]+)\s*\}',
                      TS_PATH.read_text(encoding="utf-8"))
    for model, inp, out in rows:
        if model not in by_name:
            problems.append(f"pricing.ts: has {model}, which is not in the JSON")
            continue
        e = by_name[model]
        pairs = (("input", inp, e["input_cost_per_1k_tokens"]), ("output", out, e["output_cost_per_1k_tokens"]))
        for label, ts, js in pairs:
            if Decimal(ts) != mtok(js):
                problems.append(f"pricing.ts: would UPDATE {model}.{label}: {ts} -> {mtok(js)} USD/MTok")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="Verify consistency (the only mode today).")
    ap.parse_args()
    problems = check()
    if problems:
        print("Out of sync with briefcase/data/pricing/v1.json. Regeneration would make these changes:")
        for p in problems:
            print(f"  {p}")
        print("(nothing was written; regeneration of cost.rs / pricing.ts is not implemented yet)")
        return 1
    print("ok: cost.rs and pricing.ts agree with briefcase/data/pricing/v1.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
