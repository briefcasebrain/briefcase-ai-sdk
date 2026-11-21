"""tests/fixtures/bedrock_rates.json against the Rust cost engine's table.

The fixture is the per-model Bedrock price list shared with the TypeScript
package (js/controls/test/bedrock.test.ts asserts its own table equals it).
This suite reads crates/briefcase-core/src/cost.rs and fails on any fixture
price the engine does not register, so a rate cannot be introduced here first.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURE = json.loads((REPO / "tests" / "fixtures" / "bedrock_rates.json").read_text(encoding="utf-8"))
COST_RS = REPO / "crates" / "briefcase-core" / "src" / "cost.rs"

_NUMBER = r"([0-9][0-9_]*(?:\.[0-9]+)?)"


def _engine_rates():
    """Per-1M-token (input, output) registered in cost.rs, keyed by model name."""
    source = COST_RS.read_text(encoding="utf-8")
    rates = {}
    # ModelPricing::from_mtok("name", "provider", input_per_mtok, output_per_mtok, ...)
    per_million = re.compile(
        r'from_mtok\(\s*"([^"]+)",\s*"[^"]+",\s*' + _NUMBER + r",\s*" + _NUMBER + ","
    )
    for model, inp, out in per_million.findall(source):
        rates[model] = (float(inp.replace("_", "")), float(out.replace("_", "")))
    # ModelPricing { model_name: "name", ..., input_cost_per_1k_tokens: x, output_cost_per_1k_tokens: y }
    per_thousand = re.compile(
        r'model_name:\s*"([^"]+)"\.to_string\(\),\s*provider:[^,]+,\s*'
        r"input_cost_per_1k_tokens:\s*" + _NUMBER + r",\s*output_cost_per_1k_tokens:\s*" + _NUMBER + ","
    )
    for model, inp, out in per_thousand.findall(source):
        rates.setdefault(model, (float(inp.replace("_", "")) * 1000, float(out.replace("_", "")) * 1000))
    return rates


ENGINE = _engine_rates()


def test_engine_table_was_found():
    assert len(ENGINE) > 20
    assert ENGINE["claude-opus-4-1"] == (15.0, 75.0)
    assert ENGINE["nova-micro"] == (0.035, 0.14)


def test_fixture_is_per_model_not_regex_families():
    assert set(FIXTURE) == {
        "description", "currency", "per_tokens", "rates", "aliases",
        "fallback_rate_model", "opt_in_fallback_rate",
    }
    assert FIXTURE["currency"] == "USD" and FIXTURE["per_tokens"] == 1_000_000
    models = [row["model"] for row in FIXTURE["rates"]]
    assert len(models) == len(set(models))
    for row in FIXTURE["rates"]:
        # No "match" regex rows and no catch-all "default" rate.
        assert set(row) == {"model", "input", "output"}, row
        assert re.fullmatch(r"[a-z0-9.-]+", row["model"]), row
    assert "default" not in FIXTURE


@pytest.mark.parametrize("row", FIXTURE["rates"], ids=lambda row: row["model"])
def test_every_fixture_price_is_the_engines(row):
    assert row["model"] in ENGINE, f"{row['model']} is not registered in cost.rs"
    engine_input, engine_output = ENGINE[row["model"]]
    assert row["input"] == pytest.approx(engine_input, abs=1e-9)
    assert row["output"] == pytest.approx(engine_output, abs=1e-9)


def test_opus_generations_keep_their_own_rates():
    # The old fixture priced every "opus" id at 15/75.
    by_model = {row["model"]: (row["input"], row["output"]) for row in FIXTURE["rates"]}
    assert by_model["claude-opus-4-1"] == (15, 75)
    assert by_model["claude-opus-4-5"] == (5, 25)
    assert by_model["claude-haiku-4-5"] == (1, 5)
    assert by_model["claude-3-haiku"] == (0.25, 1.25)


def test_aliases_carry_no_price_and_point_at_priced_models():
    models = {row["model"] for row in FIXTURE["rates"]}
    for alias, target in FIXTURE["aliases"].items():
        assert target in models, alias
        assert alias not in models, alias


def test_fallback_rate_is_an_engine_price():
    model = FIXTURE["fallback_rate_model"]
    rate = FIXTURE["opt_in_fallback_rate"]
    assert (float(rate["input"]), float(rate["output"])) == ENGINE[model]
    assert {"model": model, **rate} in FIXTURE["rates"]
