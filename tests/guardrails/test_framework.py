"""
Tests for the guardrail framework wrappers and registry helpers.
"""


from briefcase.guardrails.framework import (
    BaseGuardrailEnv,
    CacheWrapper,
    Effect,
    EvalRequest,
    EvalResult,
    _default_registry,
    register,
)


class CountingEnv(BaseGuardrailEnv):
    """Allows everything and counts evaluate() calls."""

    _name = "counting"

    def __init__(self):
        self.calls = 0

    def evaluate(self, request):
        self.calls += 1
        return EvalResult(effect=Effect.ALLOW, guardrail_name=self._name)


def _request(i):
    return EvalRequest(agent="agent", action="access", resource=f"doc-{i}")


def test_cache_wrapper_returns_cached_result():
    env = CountingEnv()
    wrapper = CacheWrapper(env, ttl_seconds=60.0)
    first = wrapper.evaluate(_request(0))
    second = wrapper.evaluate(_request(0))
    assert env.calls == 1
    assert first.metadata.get("cache_hit") is None
    assert second.metadata.get("cache_hit") is True


def test_cache_wrapper_bounds_store():
    env = CountingEnv()
    wrapper = CacheWrapper(env, ttl_seconds=60.0, max_entries=3)
    for i in range(10):
        wrapper.evaluate(_request(i))
    assert len(wrapper._store) == 3


def test_cache_wrapper_evicts_least_recently_used():
    env = CountingEnv()
    wrapper = CacheWrapper(env, ttl_seconds=60.0, max_entries=2)
    wrapper.evaluate(_request(0))
    wrapper.evaluate(_request(1))
    hit = wrapper.evaluate(_request(0))
    assert hit.metadata.get("cache_hit") is True
    wrapper.evaluate(_request(2))
    assert wrapper.evaluate(_request(0)).metadata.get("cache_hit") is True
    calls_before = env.calls
    wrapper.evaluate(_request(1))
    assert env.calls == calls_before + 1


def test_module_register_accepts_constructor_kwargs():
    guardrail_id = "framework-test-ctor-v1"
    try:
        register(guardrail_id, "my_package:MyGuardrail", threshold=0.9)
        spec = _default_registry._specs[guardrail_id]
        assert spec.kwargs == {"threshold": 0.9}
    finally:
        _default_registry._specs.pop(guardrail_id, None)


def test_module_register_splits_registry_params_from_ctor_kwargs():
    guardrail_id = "framework-test-split-v1"
    try:
        register(
            guardrail_id,
            "my_package:MyGuardrail",
            kwargs={"alpha": 1},
            tags=["test"],
            description="desc",
            threshold=0.9,
        )
        spec = _default_registry._specs[guardrail_id]
        assert spec.kwargs == {"alpha": 1, "threshold": 0.9}
        assert spec.tags == ["test"]
        assert spec.description == "desc"
    finally:
        _default_registry._specs.pop(guardrail_id, None)
