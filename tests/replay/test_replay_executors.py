"""Live executors driven through each SDK's own client with a fake transport
(openai, anthropic, google-genai) or a botocore Stubber (bedrock), the way
tests/providers does. Records are produced by the real provider
instrumentation with content on, then replayed with the instrumentation
still installed: the replay must not be captured again."""

import json

import pytest

from briefcase.replay.recorded import (
    REPLAY_HEADER,
    AnthropicExecutor,
    BedrockExecutor,
    GoogleGenAIExecutor,
    OpenAIExecutor,
    Overrides,
    analyze,
    make_plan,
    replaying,
    run,
)

from tests.providers._helpers import httpx_for


# -- openai -------------------------------------------------------------------

openai = pytest.importorskip("openai")
from openai.types.chat import ChatCompletion  # noqa: E402


def _chat_body(content=None, tool_calls=None, model="gpt-test-2026-01-01", usage=(11, 7)):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return json.loads(ChatCompletion.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                     "message": message}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                  "total_tokens": sum(usage)},
    }).model_dump_json(exclude_none=True))


def _openai_client(body, seen=None):
    httpx = httpx_for(openai.OpenAI(api_key="k"))

    def handler(request):
        if seen is not None:
            seen.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json=body, headers={"x-request-id": "req_1"})

    return openai.OpenAI(api_key="k", max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _openai_record(instrument, mem, body, **kwargs):
    instrument("openai")
    _openai_client(body).chat.completions.create(**kwargs)
    assert len(mem.records) == 1
    return mem.records[0]


CHAT_KWARGS = dict(model="gpt-test", messages=[{"role": "user", "content": "Capital of France?"}],
                   temperature=0.2, top_p=0.9, seed=7, max_completion_tokens=50)


def test_openai_chat_replay_pins_parameters_and_is_not_recaptured(instrument, mem):
    record = _openai_record(instrument, mem, _chat_body("Paris"), **CHAT_KWARGS)
    call = analyze(record)
    assert call.replayable, call.reasons
    seen = []
    executor = OpenAIExecutor(_openai_client(_chat_body("  paris ", usage=(11, 9)), seen),
                              run_id="r1")
    rows = run(make_plan([call], executor), executor)

    assert len(mem.records) == 1  # instrumentation still installed; replay not recorded
    headers, body = seen[0]
    assert headers[REPLAY_HEADER] == "r1"
    assert body["model"] == "gpt-test"
    assert body["messages"] == CHAT_KWARGS["messages"]
    for key in ("temperature", "top_p", "seed", "max_completion_tokens"):
        assert body[key] == CHAT_KWARGS[key]
    row = rows[0]
    assert row["status"] == "matched" and row["match_level"] == "normalized"
    assert row["tokens"]["delta"]["completion_tokens"] == 2
    assert row["latency_ms"]["replay"] is not None


def test_openai_overrides_change_model_and_params(instrument, mem):
    record = _openai_record(instrument, mem, _chat_body("Paris"), **CHAT_KWARGS)
    seen = []
    executor = OpenAIExecutor(_openai_client(_chat_body("Lyon"), seen))
    overrides = Overrides(model="gpt-other", params={"temperature": 0,
                                                     "response_format": {"type": "json_object"}},
                          drop={"seed"})
    rows = run(make_plan([analyze(record)], executor, overrides), executor)
    _, body = seen[0]
    assert body["model"] == "gpt-other"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert "seed" not in body
    assert rows[0]["status"] == "changed"
    assert "-Paris" in rows[0]["diff"] and "+Lyon" in rows[0]["diff"]


def test_openai_tool_calls_are_compared_structurally_never_run(instrument, mem):
    tool = [{"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "Paris", "unit": "c"}'}}]
    record = _openai_record(
        instrument, mem, _chat_body(None, tool),
        model="gpt-test", messages=[{"role": "user", "content": "weather?"}],
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
    )
    same_args_other_order = [{"id": "call_9", "type": "function", "function": {
        "name": "get_weather", "arguments": '{"unit": "c", "city": "Paris"}'}}]
    executor = OpenAIExecutor(_openai_client(_chat_body(None, same_args_other_order)))
    row = run(make_plan([analyze(record)], executor), executor)[0]
    assert row["status"] == "matched"
    assert row["match_level"] == "normalized"  # raw argument strings differ, structure equal

    other_tool = [{"id": "c", "type": "function",
                   "function": {"name": "get_weather", "arguments": '{"city": "Lyon"}'}}]
    executor = OpenAIExecutor(_openai_client(_chat_body(None, other_tool)))
    row = run(make_plan([analyze(record)], executor), executor)[0]
    assert row["status"] == "changed"


def test_openai_api_error_is_an_error_row(instrument, mem):
    record = _openai_record(instrument, mem, _chat_body("Paris"), **CHAT_KWARGS)
    httpx = httpx_for(openai.OpenAI(api_key="k"))
    client = openai.OpenAI(api_key="k", max_retries=0, http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {}}))))
    executor = OpenAIExecutor(client, max_retries=0)
    row = run(make_plan([analyze(record)], executor), executor)[0]
    assert row["status"] == "error"
    assert "429" in row["error"]
    assert row["match_level"] is None


def test_replaying_flag_is_set_only_during_the_call(instrument, mem):
    record = _openai_record(instrument, mem, _chat_body("Paris"), **CHAT_KWARGS)
    states = []
    httpx = httpx_for(openai.OpenAI(api_key="k"))

    def handler(request):
        states.append(replaying())
        return httpx.Response(200, json=_chat_body("Paris"))

    client = openai.OpenAI(api_key="k", max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    executor = OpenAIExecutor(client, run_id="run-x")
    run(make_plan([analyze(record)], executor), executor)
    assert states == ["run-x"]
    assert replaying() is None


# -- anthropic ----------------------------------------------------------------

anthropic = pytest.importorskip("anthropic")
from anthropic.types import Message  # noqa: E402


def _message(content):
    return json.loads(Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
        "content": content, "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 5},
    }).model_dump_json(exclude_none=True))


def _anthropic_client(body, seen=None):
    httpx = httpx_for(anthropic.Anthropic(api_key="k"))

    def handler(request):
        if seen is not None:
            seen.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json=body, headers={"request-id": "req_1"})

    return anthropic.Anthropic(api_key="k", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_anthropic_replay(instrument, mem):
    instrument("anthropic")
    kwargs = dict(model="claude-test", max_tokens=64, system="Be brief.",
                  messages=[{"role": "user", "content": "hi"}])
    _anthropic_client(_message([{"type": "text", "text": '{"a": 1, "b": [2]}'}])
                      ).messages.create(**kwargs)
    call = analyze(mem.records[0])
    assert call.replayable, call.reasons
    seen = []
    executor = AnthropicExecutor(
        _anthropic_client(_message([{"type": "text", "text": '```json\n{"b": [2], "a": 1}\n```'}]),
                          seen), run_id="r2")
    row = run(make_plan([call], executor), executor)[0]
    assert len(mem.records) == 1
    headers, body = seen[0]
    assert headers[REPLAY_HEADER] == "r2"
    assert body["system"] == "Be brief." and body["max_tokens"] == 64
    assert body["model"] == "claude-test"
    assert row["status"] == "matched" and row["match_level"] == "json"


def test_anthropic_sampling_params_unknown_to_the_installed_sdk_block_the_replay():
    record = {
        "decision_type": "llm.anthropic.messages", "status": "ok",
        "function_name": "anthropic.messages.create",
        "inputs": {"messages": [{"role": "user", "content": "hi"}]},
        "outputs": {"content": "hello"},
        "model_parameters": {"provider": "anthropic", "model_name": "claude-test",
                             "max_tokens": 10, "temperature": 0.3},
    }
    call = analyze(record)
    executor = AnthropicExecutor(client=object())
    import inspect
    from anthropic.resources.messages import Messages
    accepts = "temperature" in inspect.signature(Messages.create).parameters
    plan = make_plan([call], executor)
    if accepts:  # pragma: no cover - depends on the installed SDK
        assert not plan.blocked
    else:
        assert "temperature" in plan.blocked[0][0]
        plan = make_plan([call], executor, Overrides(drop={"temperature"}))
        assert not plan.blocked


def test_anthropic_tool_use_without_result_is_not_replayable():
    record = {
        "decision_type": "llm.anthropic.messages", "status": "ok",
        "inputs": {"messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {}}]},
        ]},
        "outputs": {"content": "x"},
        "model_parameters": {"provider": "anthropic", "model_name": "m", "max_tokens": 5},
    }
    call = analyze(record)
    assert not call.replayable
    assert "get_weather" in call.reasons[0] and "never runs tools" in call.reasons[0]


# -- google-genai --------------------------------------------------------------

genai = pytest.importorskip("google.genai")
from google.genai import types  # noqa: E402

try:
    import httpx2 as ghttpx
except ImportError:  # pragma: no cover
    import httpx as ghttpx


def _gen_body(text):
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason="STOP", index=0)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=8, candidates_token_count=4, total_token_count=12),
        model_version="gemini-test-001", response_id="r1",
    )
    return json.loads(response.model_dump_json(exclude_none=True, by_alias=True))


def _genai_client(body, seen=None):
    def handler(request):
        if seen is not None:
            seen.append((dict(request.headers), json.loads(request.content)))
        return ghttpx.Response(200, json=body)

    return genai.Client(api_key="k", http_options=types.HttpOptions(
        httpx_client=ghttpx.Client(transport=ghttpx.MockTransport(handler)),
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def test_google_genai_replay(instrument, mem):
    instrument("google-genai")
    _genai_client(_gen_body("positive")).models.generate_content(
        model="gemini-test", contents="Classify: great!",
        config=types.GenerateContentConfig(temperature=0.0, seed=3, max_output_tokens=5,
                                           system_instruction="One word."),
    )
    call = analyze(mem.records[0])
    assert call.replayable, call.reasons
    seen = []
    executor = GoogleGenAIExecutor(_genai_client(_gen_body("negative"), seen), run_id="r3")
    row = run(make_plan([call], executor), executor)[0]
    assert len(mem.records) == 1
    headers, body = seen[0]
    assert headers[REPLAY_HEADER] == "r3"
    assert body["generationConfig"]["seed"] == 3
    assert body["generationConfig"]["temperature"] == 0.0
    assert body["generationConfig"]["maxOutputTokens"] == 5
    assert "One word." in json.dumps(body["systemInstruction"])
    assert row["status"] == "changed"


# -- bedrock -------------------------------------------------------------------

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber  # noqa: E402


def _bedrock():
    return boto3.client("bedrock-runtime", region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test")


def _converse_response(text):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 6, "totalTokens": 16},
        "metrics": {"latencyMs": 42},
    }


def test_bedrock_converse_replay(instrument, mem):
    instrument("bedrock")
    params = dict(modelId="anthropic.claude-test",
                  messages=[{"role": "user", "content": [{"text": "hi"}]}],
                  system=[{"text": "sys"}],
                  inferenceConfig={"maxTokens": 64, "temperature": 0.1})
    client = _bedrock()
    with Stubber(client) as stub:
        stub.add_response("converse", _converse_response("hello"), params)
        client.converse(**params)
    call = analyze(mem.records[0])
    assert call.replayable, call.reasons

    client = _bedrock()
    executor = BedrockExecutor(client)
    with Stubber(client) as stub:
        # Stubber fails the call if the replayed params differ from these.
        stub.add_response("converse", _converse_response("Hello"), params)
        row = run(make_plan([call], executor), executor)[0]
        stub.assert_no_pending_responses()
    assert len(mem.records) == 1
    assert row["status"] == "matched" and row["match_level"] == "normalized"
