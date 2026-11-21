"""Replayability analysis, comparison levels, cassettes and planning."""

import json

import pytest

from briefcase.replay.recorded import (
    CassetteExecutor,
    Overrides,
    PlanError,
    analyze,
    compare,
    load_records,
    make_plan,
    run,
    summarize,
)
from briefcase.replay._recorded.executors import ExecutorError


def chat_record(i=0, text="Paris", model="gpt-test", **extra):
    record = {
        "schema_version": "1.1", "decision_id": f"id-{i}", "decision_type": "llm.openai.chat",
        "function_name": "openai.chat.completions.create", "status": "ok",
        "inputs": {"messages": [{"role": "user", "content": f"question {i}"}]},
        "outputs": {"content": text},
        "model_parameters": {"provider": "openai", "model_name": model, "temperature": 0.0,
                             "max_tokens": 20},
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        "execution_time_ms": 120.0, "finish_reason": "stop", "stream": False,
    }
    record.update(extra)
    return record


# -- not replayable, and why -------------------------------------------------

@pytest.mark.parametrize("change, needle", [
    (dict(inputs={}), "content capture was off"),
    (dict(inputs={"sha256": "ab", "chars": 3}), "captured as a hash"),
    (dict(inputs={"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": "<1024 bytes>"}]}]}), "placeholder"),
    (dict(inputs={"messages": [{"role": "user", "content": "<redaction-failed>"}]}),
     "redact hook failed"),
    (dict(inputs={"messages": [{"role": "user", "content": "x" * 8000 + "..."}]}),
     "cut at the capture limit"),
    (dict(inputs={"messages": [{"role": "user", "content": "m"}] * 200}), "exactly 200 items"),
    (dict(status="error", error_type="RateLimitError", outputs={}), "did not complete"),
    (dict(outputs={"content": "Par", "chunk_count": 2, "stream_completed": False}, stream=True),
     "not read to the end"),
    (dict(outputs={"content": "", "chunk_count": 3, "stream_completed": True}, stream=True),
     "recorded output is empty"),
    (dict(outputs={}, stream=True), "streamed tool calls are not captured"),
    (dict(decision_type="llm.openai.embeddings"), "embeddings"),
    (dict(decision_type="llm.bedrock.invoke_model"), "InvokeModel"),
    (dict(decision_type="my_function"), "not a provider call"),
    (dict(inputs={"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
    ]}), "no tool results"),
])
def test_not_replayable_records_say_why(change, needle):
    call = analyze(chat_record(**change))
    assert not call.replayable
    assert any(needle in reason for reason in call.reasons), call.reasons


def test_tool_results_present_make_it_replayable():
    call = analyze(chat_record(inputs={"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "42"},
    ]}))
    assert call.replayable, call.reasons


def test_unrecorded_settings_are_a_warning_not_a_block():
    call = analyze(chat_record())
    assert call.replayable
    assert any("response_format" in w for w in call.warnings)


def test_load_records_reports_unparsable_lines(tmp_path):
    path = tmp_path / "runs.jsonl"
    path.write_text(json.dumps(chat_record()) + "\nnot json\n\n[1]\n")
    calls = load_records(path)
    assert [c.replayable for c in calls] == [True, False, False]
    assert "line 2" in calls[1].reasons[0]


# -- comparison levels -------------------------------------------------------

def out(text=None, tools=()):
    from briefcase.replay._recorded.records import normalize_tool_calls
    return {"text": text, "tool_calls": normalize_tool_calls(list(tools))}


def test_exact():
    assert compare(out("a b"), out("a b"))["match_level"] == "exact"


def test_normalized_ignores_case_whitespace_and_compat_forms():
    result = compare(out("Hello   World\n"), out("hello world"))
    assert result["match_level"] == "normalized"
    assert result["checks"]["exact"] is False
    assert compare(out("ﬁne"), out("fine"))["match_level"] == "normalized"  # NFKC ligature


def test_json_structural_and_not_applicable():
    result = compare(out('{"a": 1, "b": [1, 2]}'), out('```json\n{"b": [1, 2], "a": 1}\n```'))
    assert result["match_level"] == "json"
    assert compare(out('{"a": 1}'), out('{"a": 2}'))["match_level"] is None
    assert compare(out("x"), out("y"))["checks"]["json"] is None


def test_tool_call_difference_is_a_change_even_when_text_matches():
    a = out("ok", [{"name": "f", "input": {"x": 1}}])
    b = out("ok", [{"name": "f", "input": {"x": 2}}])
    result = compare(a, b)
    assert result["match_level"] is None
    assert result["checks"]["normalized"] is False


def test_scorer_hook_and_threshold():
    a, b = out("the cat sat"), out("a cat was sitting")
    assert compare(a, b)["checks"]["semantic"] is None  # no scorer: not compared
    assert compare(a, b, scorer=lambda x, y: 0.95)["match_level"] == "semantic"
    assert compare(a, b, scorer=lambda x, y: 0.5)["match_level"] is None
    broken = compare(a, b, scorer=lambda x, y: 1 / 0)
    assert broken["match_level"] is None and "ZeroDivisionError" in broken["scorer_error"]


# -- cassette, plan, run ------------------------------------------------------

def test_builtin_cassette_matches_every_replayable_record_and_reports_the_rest():
    records = [chat_record(0), chat_record(1, inputs={}), chat_record(2)]
    executor = CassetteExecutor()
    rows = run(make_plan(load_records(records), executor), executor)
    assert [r["status"] for r in rows] == ["matched", "not_replayable", "matched"]
    assert rows[1]["match_level"] is None
    assert summarize(rows)["status"] == {"matched": 2, "changed": 0, "not_replayable": 1,
                                         "error": 0}


def test_builtin_cassette_refuses_overrides():
    executor = CassetteExecutor()
    plan = make_plan(load_records([chat_record()]), executor, Overrides(model="other"))
    rows = run(plan, executor)
    assert rows[0]["status"] == "not_replayable"
    assert "live executor" in rows[0]["reasons"][0]


def test_cassette_round_trip_and_miss(tmp_path):
    calls = load_records([chat_record(0), chat_record(1)])
    saved = []
    executor = CassetteExecutor()
    run(make_plan(calls, executor), executor, cassette=saved)
    path = tmp_path / "cassette.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in saved))
    assert "Paris" in path.read_text()

    replay = CassetteExecutor(path)
    rows = run(make_plan(calls, replay), replay)
    assert [r["status"] for r in rows] == ["matched", "matched"]

    changed = load_records([chat_record(0, inputs={"messages": [
        {"role": "user", "content": "a different prompt"}]})])
    row = run(make_plan(changed, replay), replay)[0]
    assert row["status"] == "error" and "no cassette entry" in row["error"]
    with pytest.raises(ExecutorError):
        replay.execute(changed[0], Overrides())


class _Live:
    name = "fake-live"
    live = True

    def __init__(self, text="Paris"):
        self.text = text
        self.calls = 0

    def check(self, call, overrides):
        return []

    def execute(self, call, overrides):
        from briefcase.replay.recorded import Execution
        self.calls += 1
        return Execution({"text": self.text, "tool_calls": []},
                         {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                         80.0, "gpt-test", "stop")


def test_plan_counts_models_and_token_bounds():
    records = [chat_record(0), chat_record(1, model="gpt-b"), chat_record(2, inputs={})]
    records[1]["model_parameters"].pop("max_tokens")
    plan = make_plan(load_records(records), _Live()).summary()
    assert plan["records"] == 3 and plan["replayable"] == 2 and plan["calls"] == 2
    assert plan["per_model"] == {"gpt-b": 1, "gpt-test": 1}
    assert plan["tokens"]["upper_bound_tokens"] is None  # one call has no cap
    assert plan["tokens"]["estimate_tokens"] == (10 + 20) + (10 + 3)
    capped = make_plan(load_records([chat_record(0)]), _Live()).summary()
    assert capped["tokens"]["upper_bound_tokens"] == 30


def test_limit_sample_and_max_calls():
    calls = load_records([chat_record(i) for i in range(10)])
    assert len(make_plan(calls, _Live(), limit=3).to_run) == 3
    a = [c.index for c in make_plan(calls, _Live(), sample=4, seed=1).to_run]
    b = [c.index for c in make_plan(calls, _Live(), sample=4, seed=1).to_run]
    assert a == b and len(a) == 4
    live = _Live()
    with pytest.raises(PlanError, match="max-calls"):
        run(make_plan(calls, live), live, max_calls=5)
    assert live.calls == 0


def test_rows_carry_token_cost_and_latency_deltas():
    record = chat_record(cost_usd=0.001)
    live = _Live("Lyon")
    row = run(make_plan(load_records([record]), live), live,
              prices={"gpt-test": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}})[0]
    assert row["status"] == "changed"
    assert row["tokens"]["delta"] == {"prompt_tokens": 0, "completion_tokens": 2,
                                      "total_tokens": 2}
    assert row["latency_ms"]["delta"] == -40.0
    assert row["cost_usd"]["recorded"] == 0.001
    assert row["cost_usd"]["replay"] == pytest.approx(10 / 1e6 + 10 / 1e6)
