"""Budgets enforced through the provider call hooks, driven through each SDK's
own client over a fake transport (nothing leaves the process)."""

from __future__ import annotations

import asyncio
import gc
import json
import time

import pytest

openai = pytest.importorskip("openai")

from briefcase import budgets, pricing  # noqa: E402
from briefcase.budgets import Budget, BudgetExceeded  # noqa: E402
from briefcase.integrations.providers import _base  # noqa: E402

from tests.providers._helpers import drain_gc, httpx_for, sse  # noqa: E402

TABLE = pricing.load(environ={"BRIEFCASE_HOME": "/nonexistent-briefcase-home"})
IN, OUT = 5e-6, 15e-6  # gpt-4o
USAGE = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


@pytest.fixture(autouse=True)
def _clean_hooks():
    budgets.disable()
    yield
    budgets.disable()
    assert _base.call_hooks() == ()


def _chat_body(model="gpt-4o-2024-08-06", usage=USAGE):
    body = {"id": "c1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello"}}]}
    if usage:
        body["usage"] = usage
    return body


def _chunks(with_usage):
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o"}
    out = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": "hel"}}]),
           dict(base, choices=[{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}])]
    if with_usage:
        out.append(dict(base, choices=[], usage=USAGE))
    return out


class Recorder:
    def __init__(self, status=200, usage=USAGE):
        self.requests = []
        self.status = status
        self.usage = usage

    def handler(self, httpx):
        def handle(request):
            body = json.loads(request.content)
            self.requests.append(body)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"message": "no"}})
            if body.get("stream"):
                with_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=sse([(None, c) for c in _chunks(with_usage)],
                                                  done=True))
            return httpx.Response(200, json=_chat_body(body["model"], self.usage))
        return handle

    def client(self, is_async=False):
        httpx = httpx_for(openai.OpenAI(api_key="k"))
        if is_async:
            return openai.AsyncOpenAI(api_key="k", max_retries=0, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler(httpx))))
        return openai.OpenAI(api_key="k", max_retries=0, http_client=httpx.Client(
            transport=httpx.MockTransport(self.handler(httpx))))


MSG = [{"role": "user", "content": "x" * 30}]


def configure(*items, **kw):
    kw.setdefault("pricing", TABLE)
    return budgets.configure(items, **kw)


def counter(eng, name="daily"):
    return [r for r in eng.status() if r["budget"] == name][0]


def test_chat_reserves_then_settles_to_reported_usage(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    rec = Recorder()
    result = rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=100)
    assert isinstance(result, openai.types.chat.ChatCompletion)
    record = mem.records[0]
    assert record["budget_effect"] == "allow"
    assert record["cost_source"] == "usage"
    assert record["cost_usd"] == pytest.approx(20 * IN + 10 * OUT)
    # Reserved: (30 bytes / 3 + 4 framing) input tokens + 100 output tokens.
    assert record["cost_reserved_usd"] == pytest.approx(14 * IN + 100 * OUT)
    assert record["budget_checks"][0]["budget"] == "daily"
    row = counter(eng)
    assert row["used_usd"] == pytest.approx(20 * IN + 10 * OUT)
    assert row["inflight_usd"] == 0 and row["used_tokens"] == 30


def test_block_raises_before_the_request_is_sent(instrument, mem):
    instrument("openai")
    configure(Budget("daily", usd=0.0001))
    rec = Recorder()
    with pytest.raises(BudgetExceeded) as info:
        rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=100)
    assert rec.requests == []
    assert info.value.budget == "daily"
    record = mem.records[0]
    assert record["status"] == "error" and record["error_type"] == "BudgetExceeded"
    assert record["budget_effect"] == "block"
    assert record["cost_source"] == "not_billed" and record["cost_usd"] == 0.0


def test_budgets_apply_without_an_exporter(instrument, monkeypatch):
    inst = instrument("openai", exporter=None)
    monkeypatch.setattr(inst, "_resolve_exporter", lambda: None)
    eng = configure(Budget("daily", usd=0.0001))
    rec = Recorder()
    with pytest.raises(BudgetExceeded):
        rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=100)
    configure(Budget("daily", usd=1))
    rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=100)
    assert budgets.active().status()[0]["used_tokens"] == 30
    assert eng is not budgets.active()


def test_degrade_sends_the_cheaper_model(instrument, mem):
    instrument("openai")
    configure(Budget("daily", usd=0.001, on_exceed="degrade", degrade_to="gpt-4o-mini"))
    rec = Recorder()
    rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=100)
    assert rec.requests[0]["model"] == "gpt-4o-mini"
    record = mem.records[0]
    assert record["budget_effect"] == "degrade"
    assert record["budget_degraded_from"] == "gpt-4o"
    assert record["model_parameters"]["model_name"] == "gpt-4o-mini"


def test_http_429_is_released(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    with pytest.raises(openai.RateLimitError):
        Recorder(status=429).client().chat.completions.create(model="gpt-4o", messages=MSG,
                                                              max_tokens=10)
    assert mem.records[0]["cost_source"] == "not_billed"
    assert counter(eng)["used_usd"] == 0 and counter(eng)["inflight_usd"] == 0


def test_missing_usage_settles_at_the_reservation(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    Recorder(usage=None).client().chat.completions.create(model="gpt-4o", messages=MSG,
                                                          max_tokens=100)
    record = mem.records[0]
    assert record["token_usage"] is None
    assert record["cost_source"] == "reserved"
    assert record["cost_usd"] == record["cost_reserved_usd"]
    assert counter(eng)["used_usd"] == pytest.approx(record["cost_reserved_usd"])


def test_async_client(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))

    async def main():
        client = Recorder().client(is_async=True)
        return await asyncio.gather(*(client.chat.completions.create(
            model="gpt-4o", messages=MSG, max_tokens=10) for _ in range(5)))

    asyncio.run(main())
    assert counter(eng)["used_tokens"] == 150
    assert {r["cost_source"] for r in mem.records} == {"usage"}


def test_stream_with_usage_settles_when_it_ends(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    stream = Recorder().client().chat.completions.create(
        model="gpt-4o", messages=MSG, max_tokens=100, stream=True,
        stream_options={"include_usage": True})
    assert counter(eng)["inflight_usd"] > 0  # reserved while streaming
    chunks = list(stream)
    assert len(chunks) == 3
    assert mem.records[0]["cost_source"] == "usage"
    assert counter(eng)["inflight_usd"] == 0
    assert counter(eng)["used_tokens"] == 30


def test_stream_without_usage_keeps_the_reservation(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    list(Recorder().client().chat.completions.create(model="gpt-4o", messages=MSG,
                                                     max_tokens=100, stream=True))
    assert mem.records[0]["cost_source"] == "reserved"
    assert counter(eng)["used_usd"] == pytest.approx(mem.records[0]["cost_reserved_usd"])


def test_stream_closed_early_settles_at_the_reservation(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    stream = Recorder().client().chat.completions.create(
        model="gpt-4o", messages=MSG, max_tokens=100, stream=True,
        stream_options={"include_usage": True})
    next(iter(stream))
    stream.close()
    record = mem.records[0]
    assert record["status"] == "incomplete"
    assert record["cost_source"] == "reserved"
    assert counter(eng)["inflight_usd"] == 0


def test_abandoned_stream_is_settled_by_the_garbage_collector(instrument, mem):
    instrument("openai")
    eng = configure(Budget("daily", usd=1))

    def start():
        stream = Recorder().client().chat.completions.create(
            model="gpt-4o", messages=MSG, max_tokens=100, stream=True)
        next(iter(stream))

    start()
    drain_gc()
    deadline = time.time() + 5
    while not mem.records and time.time() < deadline:
        gc.collect()
        time.sleep(0.01)
    assert mem.records[0]["cost_source"] == "reserved"
    row = counter(eng)
    assert row["inflight_usd"] == 0 and row["used_usd"] == pytest.approx(
        mem.records[0]["cost_reserved_usd"])


def test_embeddings_reserve_no_output(instrument, mem):
    instrument("openai")
    configure(Budget("daily", tokens=10_000))
    httpx = httpx_for(openai.OpenAI(api_key="k"))
    body = {"object": "list", "model": "text-embedding-3-small",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5}}
    client = openai.OpenAI(api_key="k", max_retries=0, http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    client.embeddings.create(model="text-embedding-3-small", input="hello")
    assert mem.records[0]["budget_checks"][0]["used_tokens"] == 6  # 2 + 4 framing, 0 output


# -- other SDKs ---------------------------------------------------------------------------------

def test_anthropic_create_and_stream_helper(instrument, mem):
    pytest.importorskip("anthropic")
    from tests.providers import test_anthropic as ta

    instrument("anthropic")
    eng = configure(Budget("daily", usd=1))
    kwargs = dict(ta.KWARGS, model="claude-sonnet-4-6")
    ta._client().messages.create(**kwargs)
    record = mem.records[0]
    assert record["cost_source"] == "usage_upper_bound"  # the body reports cache tokens
    # stream() manager dropped without entering: nothing sent, nothing counted.
    before = counter(eng)
    manager = ta._client().messages.stream(**kwargs)
    assert counter(eng)["inflight_usd"] > 0  # stream() binds the request: reserved now
    del manager
    gc.collect()
    assert counter(eng)["inflight_usd"] == 0 and counter(eng)["used_usd"] == before["used_usd"]
    with ta._client().messages.stream(**kwargs) as stream:
        for _ in stream:
            pass
    assert mem.records[-1]["cost_source"] == "usage_upper_bound"
    assert counter(eng)["inflight_usd"] == 0


def test_anthropic_stream_helper_blocked_before_sending(instrument, mem):
    pytest.importorskip("anthropic")
    from tests.providers import test_anthropic as ta

    instrument("anthropic")
    configure(Budget("daily", usd=0.000001))
    with pytest.raises(BudgetExceeded):
        ta._client().messages.stream(**dict(ta.KWARGS, model="claude-sonnet-4-6"))
    assert mem.records[0]["budget_effect"] == "block"


def test_google_generate_content(instrument, mem):
    pytest.importorskip("google.genai")
    from tests.providers import test_google_genai as tg

    instrument("google-genai")
    configure(Budget("daily", usd=1))
    tg._client().models.generate_content(model="gemini-2.5-flash", contents="hi",
                                         config={"max_output_tokens": 50})
    record = mem.records[0]
    assert record["cost_source"] == "usage"
    entry = TABLE.resolve("gemini-2.5-flash")[0]
    # 8 prompt + 4 candidates + 2 thoughts billed as output.
    assert record["cost_usd"] == pytest.approx(
        8 * entry["input_cost_per_1k_tokens"] / 1000 + 6 * entry["output_cost_per_1k_tokens"] / 1000)
    list(tg._client().models.generate_content_stream(model="gemini-2.5-flash", contents="hi"))
    assert mem.records[1]["cost_source"] == "usage"


def test_bedrock_converse(instrument, mem):
    pytest.importorskip("boto3")
    from botocore.stub import Stubber

    from tests.providers import test_bedrock as tb

    instrument("bedrock")
    configure(Budget("daily", usd=1))
    client = tb._client()
    model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    with Stubber(client) as stubber:
        stubber.add_response("converse", tb.CONVERSE_RESPONSE)
        client.converse(**dict(tb.CONVERSE_KWARGS, modelId=model))
    record = mem.records[0]
    assert record["budget_effect"] == "allow"
    assert record["cost_source"] == "usage_upper_bound"  # cacheReadInputTokens reported


# -- the controls gateway ------------------------------------------------------------------------

def test_gateway_reserves_once_and_settles_from_the_provider_calls(instrument, mem):
    from briefcase.controls import FixedWindowQuotaStore, Gateway, GatewayConfig

    instrument("openai")
    eng = budgets.Budgets([Budget("t", usd=1, per="tenant")], pricing=TABLE)
    budgets.configure([], pricing=TABLE)  # the hook is installed, with no budget of its own
    gw = Gateway(GatewayConfig(quota_store=FixedWindowQuotaStore(),
                               buckets={"chat": {"limit": 10, "window_s": 60}}, budgets=eng))
    client = Recorder().client()

    def call():
        assert budgets.gateway_model() == "gpt-4o"
        return client.chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=100)

    outcome = asyncio.run(gw.invoke(tenant_id="acme", bucket="chat", fn=call,
                                    budget=budgets.BudgetRequest("gpt-4o", 14, 100)))
    assert outcome.ok
    assert outcome.budget["cost_source"] == "usage"
    assert outcome.budget["cost_usd"] == pytest.approx(20 * IN + 10 * OUT)
    assert mem.records[0]["budget_effect"] == "gateway"
    rows = eng.status()
    assert rows[0]["scope"] == "tenant=acme"
    assert rows[0]["used_usd"] == pytest.approx(20 * IN + 10 * OUT) and rows[0]["inflight_usd"] == 0

    small = Gateway(GatewayConfig(quota_store=FixedWindowQuotaStore(),
                                  buckets={"chat": {"limit": 10, "window_s": 60}},
                                  budgets=budgets.Budgets([Budget("t", usd=0.00001)],
                                                          pricing=TABLE)))
    blocked = asyncio.run(small.invoke(tenant_id="acme", bucket="chat", fn=call,
                                       budget=budgets.BudgetRequest("gpt-4o", 14, 100)))
    assert not blocked.ok and blocked.reason == "budget_exceeded"
    assert isinstance(blocked.cause, BudgetExceeded)


def test_gateway_usage_of_for_uninstrumented_calls():
    from briefcase.controls import FixedWindowQuotaStore, Gateway, GatewayConfig

    eng = budgets.Budgets([Budget("t", usd=1)], pricing=TABLE)
    gw = Gateway(GatewayConfig(quota_store=FixedWindowQuotaStore(),
                               buckets={"b": {"limit": 10, "window_s": 60}}, budgets=eng))
    req = budgets.BudgetRequest("gpt-4o", 100, 100, usage_of=lambda v: v)
    outcome = asyncio.run(gw.invoke(tenant_id="x", bucket="b",
                                    fn=lambda: {"prompt_tokens": 1, "completion_tokens": 1},
                                    budget=req))
    assert outcome.budget["cost_source"] == "usage"
    assert outcome.budget["cost_usd"] == pytest.approx(IN + OUT)
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(Gateway(GatewayConfig(quota_store=FixedWindowQuotaStore(),
                                          buckets={"b": {"limit": 1, "window_s": 1}}))
                    .invoke(tenant_id="x", bucket="b", fn=lambda: 1, budget=req))


# -- zero overhead when nothing is configured -----------------------------------------------------

def test_no_hook_no_context(instrument, mem, monkeypatch):
    instrument("openai")
    made = []
    original = _base.CallContext.__init__

    def spy(self, *a, **k):
        made.append(1)
        original(self, *a, **k)

    monkeypatch.setattr(_base.CallContext, "__init__", spy)
    Recorder().client().chat.completions.create(model="gpt-4o", messages=MSG)
    assert made == []
    assert "budget_effect" not in mem.records[0] and "cost_usd" not in mem.records[0]


def test_wrapper_overhead_without_hooks_is_negligible():
    """A loose micro-benchmark: the wrapper of an instrumented method whose
    capture is off (no exporter) and with no hook registered adds well under
    20 microseconds per call over calling the original."""
    def original(**kwargs):
        return kwargs

    wrapped = _base.sync_method(original, lambda a, k: None, lambda c, r, a, k: r,
                                spec=object())
    n = 20000
    for fn in (original, wrapped):
        fn(model="m")
    start = time.perf_counter()
    for _ in range(n):
        original(model="m")
    base_time = time.perf_counter() - start
    start = time.perf_counter()
    for _ in range(n):
        wrapped(model="m")
    added = (time.perf_counter() - start - base_time) / n
    assert added < 20e-6, f"{added * 1e6:.2f} us per call"


def test_a_guard_block_releases_the_budget_reservation(instrument, mem):
    from briefcase.guardrails import content

    instrument("openai")
    eng = configure(Budget("daily", usd=1))
    content.guard(content.pii(input="block", output="off"))
    rec = Recorder()
    try:
        with pytest.raises(content.GuardrailBlocked):
            rec.client().chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "password=hunter22"}])
    finally:
        content.disable()
    assert rec.requests == []
    record = mem.records[0]
    assert record["guard_effect"] == "block" and record["cost_source"] == "not_billed"
    assert counter(eng)["used_usd"] == 0 and counter(eng)["inflight_usd"] == 0


class BrokenStore:
    def reserve(self, *a, **k):
        raise ConnectionError("redis down")

    adjust = get = reserve

    def scan(self, prefix):
        return []


def test_store_failure_fails_open_by_default_and_closed_on_request(instrument, mem):
    instrument("openai")
    configure(Budget("daily", usd=1), store=BrokenStore())
    rec = Recorder()
    rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=10)
    assert len(rec.requests) == 1
    assert mem.records[0]["budget_effect"] == "error"
    assert mem.records[0]["budget_error"] == "ConnectionError"
    configure(Budget("daily", usd=1), store=BrokenStore(), on_error="block")
    with pytest.raises(BudgetExceeded) as info:
        rec.client().chat.completions.create(model="gpt-4o", messages=MSG, max_tokens=10)
    assert info.value.reason == "store_error" and len(rec.requests) == 1
