"""Concurrency, fail-closed and provenance behaviour of the guardrail framework."""

import asyncio
import logging
import threading
import time

import pytest

from briefcase.guardrails.framework import (
    AuditWrapper,
    BaseGuardrailEnv,
    CacheWrapper,
    Effect,
    EvalRequest,
    EvalResult,
    GuardrailPipeline,
    PipelineMode,
    SamplingWrapper,
    SyncAdapter,
    TimeoutWrapper,
    VectorGuardrailEnv,
)


class Env(BaseGuardrailEnv):
    """Configurable test guardrail."""

    def __init__(self, name="env", effect=Effect.ALLOW, delay=0.0, error=None):
        self._name = name
        self.effect = effect
        self.delay = delay
        self.error = error
        self.calls = 0
        self.threads = set()
        self._lock = threading.Lock()

    def evaluate(self, request):
        with self._lock:
            self.calls += 1
            self.threads.add(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return EvalResult(
            effect=self.effect, guardrail_name=self._name,
            reason=request.resource, eval_time_ms=1.5, metadata={"origin": self._name},
        )


def _request(i=0):
    return EvalRequest(agent="agent", action="access", resource=f"doc-{i}")


# CacheWrapper / TimeoutWrapper

def test_cache_wrapper_is_thread_safe():
    env = Env()
    wrapper = CacheWrapper(env, ttl_seconds=60.0, max_entries=8)
    errors = []

    def work(seed):
        try:
            for i in range(400):
                result = wrapper.evaluate(_request((seed * 7 + i) % 20))
                assert result.effect == Effect.ALLOW
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(wrapper._store) <= 8


def test_cache_does_not_share_mutable_results_with_callers():
    wrapper = CacheWrapper(Env(), ttl_seconds=60.0)
    first = wrapper.evaluate(_request())
    first.eval_time_ms = 999.0
    first.metadata["tampered"] = True
    first.effect = Effect.DENY

    hit = wrapper.evaluate(_request())
    assert hit.effect == Effect.ALLOW
    assert hit.eval_time_ms == 0.0
    assert hit.metadata == {"origin": "env", "cache_hit": True}

    hit.metadata["tampered"] = True
    assert "tampered" not in wrapper.evaluate(_request()).metadata


def test_timeout_wrapper_does_not_mutate_cached_result():
    cache = CacheWrapper(Env(), ttl_seconds=60.0)
    wrapper = TimeoutWrapper(cache, max_ms=1000.0)

    first = wrapper.evaluate(_request())
    assert first.eval_time_ms != 1.5  # stamped with the measured wait

    stored, _ = cache._store[_request().cache_key()]
    assert stored.eval_time_ms == 1.5  # the cache still holds the env's value
    assert stored is not first

    inner = Env()
    passthrough = TimeoutWrapper(inner, max_ms=1000.0)
    original = EvalResult(effect=Effect.ALLOW, guardrail_name="x", eval_time_ms=7.0)
    inner.evaluate = lambda request: original
    returned = passthrough.evaluate(_request())
    assert original.eval_time_ms == 7.0 and returned is not original


# AuditWrapper

def test_audit_wrapper_keeps_most_recent_and_warns_once(caplog):
    wrapper = AuditWrapper(Env(), max_records=3)
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        for i in range(10):
            wrapper.evaluate(_request(i))

    log = wrapper.audit_log
    assert [request.resource for request, _ in log] == ["doc-7", "doc-8", "doc-9"]
    assert wrapper.dropped == 7
    warnings = [r for r in caplog.records if "max_records" in r.getMessage()]
    assert len(warnings) == 1

    flushed = wrapper.flush()
    assert len(flushed) == 3 and wrapper.audit_log == []
    # Recording continues after a flush, and a new overflow warns again.
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        for i in range(4):
            wrapper.evaluate(_request(i))
    assert len(wrapper.audit_log) == 3
    assert len([r for r in caplog.records if "max_records" in r.getMessage()]) == 2

    with pytest.raises(ValueError):
        AuditWrapper(Env(), max_records=0)


# SamplingWrapper

def test_sampling_skips_fail_closed_by_default_and_are_marked():
    env = Env()
    wrapper = SamplingWrapper(env, rate=0.0)
    result = wrapper.evaluate(_request())

    assert env.calls == 0
    assert result.effect == Effect.DENY
    assert result.metadata == {
        "sampled_out": True, "skipped": True, "evaluated": False, "skip_effect": "deny",
    }
    assert "not evaluated" in result.reason


def test_sampling_fail_open_is_an_explicit_choice():
    env = Env(effect=Effect.DENY)
    wrapper = SamplingWrapper(env, rate=0.0, skip_effect=Effect.ALLOW)
    result = wrapper.evaluate(_request())
    assert result.effect == Effect.ALLOW
    assert result.metadata["skipped"] is True and result.metadata["skip_effect"] == "allow"
    assert wrapper.skip_effect == Effect.ALLOW

    always = SamplingWrapper(env, rate=1.0)
    for i in range(50):
        evaluated = always.evaluate(_request(i))
        assert evaluated.effect == Effect.DENY and "sampled_out" not in evaluated.metadata
    assert env.calls == 50

    half = SamplingWrapper(Env(), rate=0.5, seed=1)
    outcomes = [half.evaluate(_request(i)) for i in range(400)]
    skipped = [r for r in outcomes if r.metadata.get("sampled_out")]
    assert 120 < len(skipped) < 280
    assert all(r.effect == Effect.DENY for r in skipped)

    with pytest.raises(ValueError):
        SamplingWrapper(env, rate=1.5)


# VectorGuardrailEnv

def test_vector_env_runs_in_parallel_and_preserves_order():
    env = Env(delay=0.05)
    vector = VectorGuardrailEnv([env], max_workers=8)
    requests = [_request(i) for i in range(8)]

    start = time.monotonic()
    results = vector.evaluate_batch(requests)
    elapsed = time.monotonic() - start

    assert [r.reason for r in results] == [f"doc-{i}" for i in range(8)]
    assert elapsed < 0.3  # sequential would take 0.4s
    assert len(env.threads) > 1


def test_vector_env_order_with_uneven_latency_and_broadcast():
    slow_first = [Env(name=f"e{i}", delay=0.05 if i == 0 else 0.0) for i in range(4)]
    vector = VectorGuardrailEnv(slow_first, max_workers=4)

    by_env = vector.evaluate_batch([_request(9)])  # N envs, 1 request
    assert [r.guardrail_name for r in by_env] == ["e0", "e1", "e2", "e3"]

    paired = vector.evaluate_batch([_request(i) for i in range(4)])  # 1:1
    assert [(r.guardrail_name, r.reason) for r in paired] == [
        (f"e{i}", f"doc-{i}") for i in range(4)
    ]

    with pytest.raises(ValueError):
        vector.evaluate_batch([_request(0), _request(1)])
    assert VectorGuardrailEnv([Env()]).evaluate_batch([]) == []


def test_vector_env_inline_mode_and_error_propagation():
    env = Env()
    inline = VectorGuardrailEnv([env], max_workers=1)
    inline.evaluate_batch([_request(i) for i in range(5)])
    assert env.threads == {threading.get_ident()}
    assert VectorGuardrailEnv.from_single(env, 3).num_envs == 3
    assert VectorGuardrailEnv([env]).max_workers >= 1

    failing = VectorGuardrailEnv([Env(error=RuntimeError("policy store down"))], max_workers=4)
    with pytest.raises(RuntimeError, match="policy store down"):
        failing.evaluate_batch([_request(i) for i in range(4)])
    with pytest.raises(ValueError):
        VectorGuardrailEnv([env], max_workers=0)


# SyncAdapter

def test_sync_adapter_does_not_block_the_event_loop():
    env = Env(delay=0.2)
    adapter = SyncAdapter(env)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        result = await adapter.evaluate(_request())
        task.cancel()
        return result, ticks

    result, ticks = asyncio.run(main())
    assert result.effect == Effect.ALLOW
    assert ticks >= 5  # the loop kept running while the guardrail blocked
    assert env.threads and threading.get_ident() not in env.threads


def test_sync_adapter_inline_mode_and_errors():
    env = Env()
    inline = SyncAdapter(env, run_in_thread=False)
    assert asyncio.run(inline.evaluate(_request())).effect == Effect.ALLOW
    assert env.threads == {threading.get_ident()}
    assert inline.name == "env"

    failing = SyncAdapter(Env(error=ValueError("bad policy")))
    with pytest.raises(ValueError, match="bad policy"):
        asyncio.run(failing.evaluate(_request()))


# GuardrailPipeline

def test_pipeline_stage_exception_denies_and_records_error():
    after = Env(name="after")
    pipeline = GuardrailPipeline([
        Env(name="ok"),
        Env(name="broken", error=ConnectionError("opa unreachable")),
        after,
    ])

    result = pipeline.evaluate(_request())

    assert result.final_effect == Effect.DENY
    assert result.is_allowed is False
    assert result.short_circuited is True
    assert after.calls == 0
    failed = result.individual_results[-1]
    assert failed.guardrail_name == "broken"
    assert failed.effect == Effect.DENY
    assert "ConnectionError: opa unreachable" in failed.reason
    assert failed.metadata["error"] is True
    assert failed.metadata["exception_type"] == "ConnectionError"
    assert failed.metadata["stage_index"] == 1


def test_pipeline_error_counts_as_deny_in_all_and_majority_modes():
    stages = lambda: [  # noqa: E731
        Env(name="a"), Env(name="broken", error=RuntimeError("x")), Env(name="c"),
    ]
    all_mode = GuardrailPipeline(stages(), mode=PipelineMode.ALL).evaluate(_request())
    assert all_mode.final_effect == Effect.DENY
    assert [r.effect for r in all_mode.individual_results] == [
        Effect.ALLOW, Effect.DENY, Effect.ALLOW,
    ]

    majority = GuardrailPipeline(stages(), mode=PipelineMode.MAJORITY).evaluate(_request())
    assert majority.final_effect == Effect.ALLOW  # 2 of 3 allow; the error voted deny
    assert majority.individual_results[1].metadata["error"] is True

    two_broken = GuardrailPipeline(
        [Env(name="a"), Env(error=RuntimeError("x")), Env(error=RuntimeError("y"))],
        mode=PipelineMode.MAJORITY,
    ).evaluate(_request())
    assert two_broken.final_effect == Effect.DENY


def test_pipeline_on_error_raise_propagates():
    pipeline = GuardrailPipeline(
        [Env(error=RuntimeError("boom"))], on_error="raise",
    )
    with pytest.raises(RuntimeError, match="boom"):
        pipeline.evaluate(_request())
    with pytest.raises(ValueError):
        GuardrailPipeline([], on_error="ignore")
