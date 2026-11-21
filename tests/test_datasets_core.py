"""briefcase.datasets: record -> case extraction, filters, scorers, and the real
inspect-ai / lm-eval artifacts produced from an exported dataset.

The fixtures under tests/fixtures/datasets/ were produced by running the
exports through the real tools: inspect-ai 0.3.291 (`inspect eval` with
`mockllm/model`, JSON log, trimmed) and lm-eval 0.4.13 (`lm_eval --model dummy
--include_path ... --log_samples`).
"""
import json
from pathlib import Path

from briefcase.datasets import core
from briefcase.datasets import evaluate as ev
from briefcase.integrations.evals import from_inspect_log, from_lm_eval_results

FIXTURES = Path(__file__).parent / "fixtures" / "datasets"


def test_where_parsing_and_matching():
    conds = core.parse_where(["decision_type=llm.*, status=ok", "model!=gpt-*"])
    assert [str(c) for c in conds] == ["decision_type=llm.*", "status=ok", "model!=gpt-*"]
    rec = {"decision_type": "llm.anthropic.messages", "status": "ok",
           "model_parameters": {"model_name": "claude-x"}}
    assert all(c.matches(rec) for c in conds)
    assert core.record_field({"error": "x"}, "status") == "error"
    assert core.record_field({"a": {"b": 1}}, "a.b") == 1


def test_provider_record_content_and_no_content():
    with_content = {"decision_type": "llm.openai.chat", "status": "ok",
                    "inputs": {"messages": [{"role": "user", "content": "hi"}], "model": "m"},
                    "outputs": {"content": "hello", "chunk_count": 3, "stream_completed": True}}
    hashed = {"decision_type": "llm.openai.chat", "status": "ok",
              "inputs": {"sha256": "ab" * 32, "chars": 10}, "outputs": {"sha256": "cd" * 32, "chars": 5}}
    res = core.promote([(1, with_content), (2, hashed)], allow_unsanitized=True)
    assert len(res.cases) == 1 and res.skipped == {"no_content": 1}
    case = res.cases[0]
    assert case["input"]["messages"][0]["content"] == "hi" and case["reference"] == "hello"
    assert case["content_sha256"] == core.case_content_hash(case["input"], "hello")


def test_capture_repr_decoding():
    rec = {"inputs": {"args": "('a', 2)", "kwargs": "{'k': [1, 2]}"}, "outputs": {"result": "{'x': (1, 2)}"}}
    assert core.extract_input(rec) == ({"args": ["a", 2], "kwargs": {"k": [1, 2]}}, True)
    assert core.extract_output(rec) == ({"x": [1, 2]}, True)
    opaque = {"inputs": {"args": "(<object at 0x1>,)"}}
    assert core.extract_input(opaque) == ({"args_text": "(<object at 0x1>,)"}, False)


def test_empty_promotion_is_explained():
    res = core.promote([(1, {"decision_type": "eval.run", "inputs": {}, "outputs": {"pass_rate": 1}})],
                       allow_unsanitized=True)
    assert res.cases == [] and "evaluation records" in res.explain()[0]


def test_builtin_scorers():
    assert ev.score_exact(" bug ", "bug", {}) is True
    assert ev.score_exact((1, 2), [1, 2], {}) is True
    assert ev.score_contains("label: bug", "bug", {}) is True
    assert ev.score_json('{"a": 1, "b": 2}', {"b": 2, "a": 1}, {})["passed"] is True
    assert ev.score_json('{"a": 1}', '{"a": 2}', {})["passed"] is False
    assert ev.score_json("not json", {"a": 1}, {})["note"] == "output is not JSON"
    assert ev.normalize_score(0.5) == (0.5, False, None)
    assert ev.normalize_score(True) == (1.0, True, None)


def test_call_convention_spreads_args():
    assert ev.call_target(lambda a, b=0: a + b, {"args": [1], "kwargs": {"b": 2}}) == 3
    assert ev.call_target(lambda payload: payload["q"], {"q": 5}) == 5


def _export_ids_and_targets():
    rows = [json.loads(line) for line in (FIXTURES / "inspect_export_triage_v2.jsonl").read_text().splitlines()]
    return rows


def test_real_inspect_log_from_exported_dataset_parses():
    exported = _export_ids_and_targets()
    parsed = from_inspect_log(FIXTURES / "inspect_log_triage_v2.json")
    assert parsed.model == "mockllm/model" and len(parsed.cases) == len(exported)
    # inspect writes samples in completion order, so compare by id.
    by_id = {c["case_id"]: c for c in parsed.cases}
    assert set(by_id) == {f"triage/{s['id']}" for s in exported}
    for s in exported:
        case = by_id[f"triage/{s['id']}"]
        assert (case["inputs"], case["target"]) == (s["input"], s["target"])


def test_real_lm_eval_samples_from_exported_docs_parse():
    docs = [json.loads(line) for line in (FIXTURES / "lm_eval_docs_triage_v2.jsonl").read_text().splitlines()]
    results = next(FIXTURES.glob("results_*.json"))
    samples = next(FIXTURES.glob("samples_briefcase_triage_v2_*.jsonl"))
    parsed = from_lm_eval_results(results, samples)
    assert parsed.name == "briefcase_triage_v2"
    assert [c["inputs"] for c in parsed.cases] == [d["input"] for d in docs]
    assert [c["target"] for c in parsed.cases] == [d["target"] for d in docs]
    assert "exact_match" in parsed.metrics
