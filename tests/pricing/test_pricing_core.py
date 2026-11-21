import json
from datetime import date

import pytest

from briefcase import pricing as pr


def write(path, models, **top):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict({"models": models}, **top), indent=2))
    return path


def model(name, i=0.001, o=0.002, **extra):
    return dict({"model_name": name, "provider": "acme", "input_cost_per_1k_tokens": i,
                 "output_cost_per_1k_tokens": o, "context_window": 100000}, **extra)


def test_merge_order_packaged_user_env_explicit(home, monkeypatch):
    write(home / ".briefcase" / "pricing.json", [model("gpt-4", 0.01, 0.02), model("only-user")])
    env = write(home / "env.json", [model("gpt-4", 0.5, 0.6), model("only-env")])
    exp = write(home / "x.json", [model("only-env", 9, 9)])
    monkeypatch.setenv("BRIEFCASE_PRICING_FILE", str(env))
    t = pr.load()
    assert t.models["gpt-4"]["layer"] == "env" and t.models["gpt-4"]["input_cost_per_1k_tokens"] == 0.5
    assert t.models["only-user"]["layer"] == "user"
    assert t.models["gpt-4o"]["layer"] == "packaged"
    t = pr.load(exp)
    assert t.models["only-env"]["layer"] == "explicit" and t.models["only-env"]["input_cost_per_1k_tokens"] == 9
    assert [layer.name for layer in t.layers] == ["packaged", "user", "env", "explicit"]


def test_user_override_replaces_whole_entry_including_extras(home):
    write(home / ".briefcase" / "pricing.json", [model("claude-opus-4-8")])
    assert pr.load().models["claude-opus-4-8"].get("rate_card_extras") is None


def test_missing_env_file_is_an_error_but_missing_user_file_is_not(home, monkeypatch):
    assert pr.load().models
    monkeypatch.setenv("BRIEFCASE_PRICING_FILE", str(home / "nope.json"))
    with pytest.raises(pr.PricingError, match="nope.json:1: cannot read"):
        pr.load()


def test_validation_errors_are_line_anchored(tmp_path):
    text = json.dumps({"models": [model("a"), model("b", i=-1), model("a")], "zzz": 1}, indent=2)
    p = tmp_path / "p.json"
    p.write_text(text)
    with pytest.raises(pr.PricingError) as ei:
        pr.load_layer("explicit", p)
    msgs = ei.value.errors
    lines = text.splitlines()
    neg = next(m for m in msgs if "not negative" in m)
    n = int(neg.split(f"{p}:")[1].split(":")[0])
    assert "input_cost_per_1k_tokens" in lines[n - 1]
    dup = next(m for m in msgs if "duplicate model id" in m)
    assert "model 'a'" in dup and f"{p}:" in dup


def test_unknown_fields_are_tolerated(tmp_path):
    p = write(tmp_path / "p.json", [model("a", future_field={"x": 1})], note="hi")
    assert pr.load_layer("explicit", p)["models"][0]["future_field"] == {"x": 1}


def test_invalid_json_reports_line(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{\n "models": [\n  {oops}\n ]\n}')
    with pytest.raises(pr.PricingError, match=r"p.json:3: invalid JSON"):
        pr.load_layer("explicit", p)


@pytest.mark.parametrize("patch,msg", [
    ({"context_window": 0}, "context_window"),
    ({"provider": ""}, "provider"),
    ({"output_cost_per_1k_tokens": "1"}, "output_cost_per_1k_tokens is required"),
    ({"max_output_tokens": 0}, "max_output_tokens"),
])
def test_field_validation(tmp_path, patch, msg):
    p = write(tmp_path / "p.json", [dict(model("a"), **patch)])
    with pytest.raises(pr.PricingError, match=msg):
        pr.load_layer("explicit", p)


def test_alias_collisions(tmp_path, home):
    p = write(tmp_path / "p.json", [model("a", aliases=["x"]), model("b", aliases=["x"])])
    with pytest.raises(pr.PricingError, match="alias 'x' is already an alias of 'a'"):
        pr.load_layer("explicit", p)
    p = write(tmp_path / "q.json", [model("a", aliases=["b"]), model("b")])
    with pytest.raises(pr.PricingError, match="collides with model id 'b'"):
        pr.load_layer("explicit", p)
    # across layers: an alias may not shadow a packaged model id
    write(home / ".briefcase" / "pricing.json", [model("mine", aliases=["gpt-4"])])
    with pytest.raises(pr.PricingError, match="collides with model id 'gpt-4'"):
        pr.load()


def test_resolution_alias_and_normalization():
    t = pr.load()
    assert t.resolve("claude-3-5-haiku")[1] == "alias"
    assert t.resolve("anthropic.claude-sonnet-4-5-20250929-v1:0")[0]["model_name"] == "claude-sonnet-4-5"
    assert t.resolve("us.anthropic.claude-3-5-haiku-20241022-v1:0")[0]["model_name"] == "claude-haiku-3-5"
    assert t.resolve("gpt-4o")[1] == "exact"
    assert t.resolve("not-a-model") is None


def test_normalize_matches_the_rust_cases():
    n = pr.normalize_model_id
    assert n("models/gemini-2.5-pro") == "gemini-2.5-pro"
    assert n("claude-sonnet-4-5@20250929") == "claude-sonnet-4-5"
    assert n("eu.amazon.nova-pro-v1:0") == "nova-pro"
    assert n("gpt-4o-2024-08-06") == "gpt-4o"
    assert n("claude-opus-4-1-latest") == "claude-opus-4-1"
    assert n("gpt-4") == "gpt-4"


def test_estimate_matches_hand_computed_values():
    t = pr.load()
    e = t.estimate("gpt-4", 1000, 500)  # 1K in at $0.03/1K + 0.5K out at $0.06/1K
    assert e["input_cost"] == pytest.approx(0.03) and e["output_cost"] == pytest.approx(0.03)
    assert e["total_cost"] == pytest.approx(0.06) and e["cache_cost"] == 0.0 and e["currency"] == "USD"
    e = t.estimate("claude-3-haiku", 2000, 1000)  # 2*0.00025 + 1*0.00125
    assert e["total_cost"] == pytest.approx(0.00175)
    e = t.estimate("gpt-4o-mini", 10, 10)
    assert e["input_cost"] == (10 / 1000.0) * 0.00015  # same float ops as the native engine
    assert t.estimate("anthropic.claude-3-haiku-20240307-v1:0", 1000, 0)["input_cost"] == pytest.approx(0.00025)


def test_estimate_refuses_what_it_cannot_mirror():
    t = pr.load()
    with pytest.raises(pr.NeedsNative, match="needs native extension"):
        t.estimate("gemini-2.5-pro", 250_000, 1000)  # long-context tier
    assert t.estimate("gemini-2.5-pro", 150_000, 1000)["total_cost"] > 0  # below the tier: flat
    with pytest.raises(pr.NeedsNative, match="needs native extension"):
        t.estimate("claude-opus-4-8", 1000, 1000, cache_read_tokens=5)
    with pytest.raises(pr.NeedsNative, match="needs native extension"):
        t.estimate("gpt-4", 1000, 1000, rate_card="batch")
    t.estimate("gpt-4", 1000, 1000, rate_card="standard")
    with pytest.raises(pr.UnknownModel):
        t.estimate("nope", 1, 1)
    with pytest.raises(ValueError):
        t.estimate("gpt-4", 0, 0)
    with pytest.raises(ValueError, match="maximum output"):
        t.estimate("gpt-4o", 10, 16385)
    with pytest.raises(ValueError, match="context window"):
        t.estimate("gpt-4", 8000, 1000)


class FakeCalc:
    def __init__(self, known):
        self.known, self.loaded = list(known), None

    def get_available_models(self):
        return self.known

    def load_pricing_json(self, text):
        self.loaded = json.loads(text)["models"]
        return len(self.loaded)


def test_apply_pushes_only_what_native_lacks_or_user_changed(home):
    t0 = pr.load()
    calc = FakeCalc(t0.models)  # native already has every packaged model
    assert t0.apply(calc) == 0 and calc.loaded is None
    write(home / ".briefcase" / "pricing.json", [model("mine", aliases=["mine-alias"]), model("gpt-4", 1, 2)])
    calc = FakeCalc(t0.models)
    assert pr.load().apply(calc) == 3
    assert {m["model_name"] for m in calc.loaded} == {"mine", "mine-alias", "gpt-4"}
    calc = FakeCalc([])  # an older core missing everything: packaged table is pushed whole
    assert pr.load().apply(calc) == len(pr.load().models) + 1


def test_apply_without_native_returns_none(home, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "briefcase._native", None)
    assert pr.load().apply() is None


def test_staleness():
    t = pr.load()
    gen = date.fromisoformat(t.generated_at)
    assert t.age_days(date(gen.year, gen.month, gen.day)) == 0
    assert t.age_days(date.fromordinal(gen.toordinal() + 10)) == 10
    assert t.model_age_days("gpt-4") is None  # never verified
    t.models["gpt-4"]["verified_on"] = "2026-01-01"
    assert t.model_age_days("gpt-4", date(2026, 1, 31)) == 30


def test_record_models_reads_only_model_fields(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text("\n".join([
        json.dumps({"inputs": {"model": "gpt-4o", "prompt": "SECRET"}}),
        json.dumps({"model_name": "o3", "outputs": {"text": "SECRET"}}),
        "not json",
        json.dumps({"inputs": {"model": "gpt-4o"}}),
    ]))
    counts, n, skipped = pr.record_models(f)
    assert counts == {"gpt-4o": 2, "o3": 1} and n == 3 and skipped == 1
