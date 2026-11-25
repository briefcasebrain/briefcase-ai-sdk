"""The packaged JSON is the reviewed source of truth; cost.rs and pricing.ts must agree with it."""
import json
import os
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

import extract  # scripts/pricing/extract.py, via conftest sys.path
from briefcase import pricing as pr

ROOT = Path(__file__).resolve().parents[2]
COST_RS = ROOT / "crates" / "briefcase-core" / "src" / "cost.rs"
TS = ROOT / "js" / "controls" / "src" / "bedrock" / "pricing.ts"
PACKAGED = json.loads(pr.packaged_text())


def test_json_agrees_with_cost_rs_on_every_model_and_field():
    models, aliases = extract.parse_cost_rs(COST_RS.read_text())
    by_name = {m["model_name"]: m for m in PACKAGED["models"]}
    assert set(by_name) == {m["model_name"] for m in models}, "model set differs from cost.rs"
    for m in models:
        got = by_name[m["model_name"]]
        for key in ("provider", "input_cost_per_1k_tokens", "output_cost_per_1k_tokens", "context_window",
                    "max_output_tokens", "rate_card_extras"):
            assert got[key] == m[key], f"{m['model_name']}.{key}: json={got[key]!r} cost.rs={m[key]!r}"
        assert got["aliases"] == sorted(a for a, t in aliases.items() if t == m["model_name"])
    assert len(PACKAGED["models"]) == len(models) == 39


def test_every_rate_in_pricing_ts_appears_in_the_json():
    rows = re.findall(r'\{\s*model:\s*"([^"]+)",\s*input:\s*([\d._]+),\s*output:\s*([\d._]+)\s*\}',
                      TS.read_text())
    assert len(rows) >= 19
    by_name = {m["model_name"]: m for m in PACKAGED["models"]}
    for model, inp, out in rows:
        assert model in by_name, f"{model} is in pricing.ts but not in v1.json"
        e = by_name[model]
        assert Decimal(pr.mtok(e["input_cost_per_1k_tokens"])) == Decimal(inp), model
        assert Decimal(pr.mtok(e["output_cost_per_1k_tokens"])) == Decimal(out), model


def test_json_extraction_is_deterministic_and_unverified(tmp_path):
    doc = extract.build_document(COST_RS, PACKAGED["generated_at"], sha=PACKAGED["models"][0]["origin"].split("@")[1])
    assert doc == PACKAGED
    assert "UNVERIFIED" in PACKAGED["notice"]
    for m in PACKAGED["models"]:
        assert m["source_url"] is None and m["effective_from"] is None and m["verified_on"] is None
        assert m["origin"].startswith("builtin-table@")


@pytest.mark.skipif(os.environ.get("BRIEFCASE_PRICING_ORACLE") != "1" or not shutil.which("cargo"),
                    reason="set BRIEFCASE_PRICING_ORACLE=1 (needs cargo and an offline-built dependency cache)")
def test_json_matches_the_cores_own_serialisation(tmp_path):
    """Oracle: dump CostCalculator::new() through the core's own types and compare every field."""
    r = subprocess.run(["cargo", "run", "--offline", "-q"], cwd=ROOT / "scripts/pricing/native_oracle",
                       env=dict(os.environ, CARGO_TARGET_DIR=str(tmp_path / "t")),
                       capture_output=True, text=True, check=True)
    oracle = {m["model_name"]: m for m in json.loads(r.stdout)["models"]}
    assert set(oracle) == {m["model_name"] for m in PACKAGED["models"]}
    for m in PACKAGED["models"]:
        for key, value in oracle[m["model_name"]].items():
            assert m[key] == value, (m["model_name"], key)


def test_hand_checked_samples_from_cost_rs():
    by = {m["model_name"]: m for m in PACKAGED["models"]}
    assert (by["gpt-4"]["input_cost_per_1k_tokens"], by["gpt-4"]["output_cost_per_1k_tokens"]) == (0.03, 0.06)
    assert by["claude-opus-4-1"]["input_cost_per_1k_tokens"] == 15.0 / 1000.0
    assert by["gemini-2.5-pro"]["rate_card_extras"]["long_context_tiers"] == [
        {"threshold_tokens": 200000, "input_per_mtok": 2.5, "output_per_mtok": 15.0}]
    assert by["claude-haiku-3-5"]["aliases"] == ["claude-3-5-haiku"]


def test_parser_refuses_what_it_does_not_understand():
    src = ('fn install_modern_models(&mut self) { self.insert_model(ModelPricing::from_mtok("x","p",1.0,2.0,10,None),'
           ' PricingExtras { surprise: Some(1.0), ..Default::default() }); }')
    with pytest.raises(extract.ExtractError, match="unknown PricingExtras field"):
        extract.parse_cost_rs(src)


def test_unpriced_list_contains_nothing_that_is_priced():
    ids = pr.unpriced_ids()
    assert len(ids) >= 30 and all(set(u) == {"id", "provider"} for u in ids)
    table = pr.load()
    rs_names = {m["model_name"] for m in extract.parse_cost_rs(COST_RS.read_text())[0]}
    for u in ids:
        assert table.resolve(u["id"]) is None, f"{u['id']} is priced"
        assert u["id"] not in rs_names and pr.normalize_model_id(u["id"]) not in rs_names
    wanted = {"o1", "o3", "o3-mini", "o4-mini", "gpt-4.1", "gpt-5", "gpt-5-mini", "claude-3-7-sonnet",
              "claude-sonnet-4", "claude-opus-4", "claude-opus-5", "gemini-1.5-pro", "gemini-1.5-flash",
              "gemini-2.0-flash"}
    assert wanted <= {u["id"] for u in ids}


def test_data_files_load_through_importlib_resources():
    from importlib import resources
    base = resources.files("briefcase.data.pricing")
    for name in ("v1.json", "unpriced.json"):
        assert json.loads(base.joinpath(name).read_text(encoding="utf-8"))["models"]


def test_generate_check_passes_and_reports_a_changed_price(tmp_path, monkeypatch):
    import generate
    assert generate.check() == []
    doc = json.loads(json.dumps(PACKAGED))
    doc["models"][0]["input_cost_per_1k_tokens"] = 0.5
    changed = tmp_path / "v1.json"
    changed.write_text(json.dumps(doc))
    monkeypatch.setattr(generate, "JSON_PATH", changed)
    problems = generate.check()
    assert any(p.startswith("cost.rs: would UPDATE gpt-4.input_cost_per_1k_tokens") for p in problems)
    assert any(p.startswith("pricing.ts:") for p in problems) is False  # gpt-4 is not in the TS table
