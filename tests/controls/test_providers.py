"""Tests for briefcase.controls.providers."""

import asyncio
import threading

import pytest

from briefcase.controls.providers import (
    LLMProvider,
    NoProviderAvailable,
    ProviderRegistry,
    TextCompletion,
    resolve_credential,
    scoped_credential,
    scoped_provider_name,
)


def _completion(provider="stub", text="ok"):
    return TextCompletion(
        text=text, input_tokens=1, output_tokens=2, model_id="m-1", provider=provider
    )


class StubProvider:
    """Minimal conforming provider: configurable availability and outcome."""

    def __init__(self, name, available=True, error=None):
        self.name = name
        self._available = available
        self._error = error
        self.calls = 0

    def available(self):
        return self._available

    def complete(self, *, system, prompt, max_tokens):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _completion(provider=self.name)


class TestProtocolConformance:
    def test_stub_satisfies_runtime_checkable_protocol(self):
        assert isinstance(StubProvider("stub"), LLMProvider)

    def test_object_without_complete_does_not_satisfy_protocol(self):
        class Partial:
            name = "partial"

            def available(self):
                return True

        assert not isinstance(Partial(), LLMProvider)

    def test_completion_is_immutable(self):
        completion = _completion()
        with pytest.raises(Exception):
            completion.text = "changed"


def _registry(*providers, **kwargs):
    kwargs.setdefault("fallback_order", [p.name for p in providers])
    registry = ProviderRegistry(**kwargs)
    for provider in providers:
        registry.register(provider)
    return registry


class TestSelection:
    def test_empty_registry_yields_nothing(self):
        registry = ProviderRegistry(fallback_order=("a", "b"))
        assert list(registry.select_providers()) == []

    def test_fallback_order_is_respected(self):
        registry = _registry(StubProvider("b"), StubProvider("a"), fallback_order=("a", "b"))
        assert [p.name for p in registry.select_providers()] == ["a", "b"]

    def test_unavailable_providers_are_skipped(self):
        registry = _registry(
            StubProvider("a", available=False), StubProvider("b"), fallback_order=("a", "b")
        )
        assert [p.name for p in registry.select_providers()] == ["b"]

    def test_preferred_comes_first(self):
        registry = _registry(StubProvider("a"), StubProvider("b"), fallback_order=("a", "b"))
        assert [p.name for p in registry.select_providers(preferred="b")] == ["b", "a"]

    def test_preferred_is_not_yielded_twice(self):
        registry = _registry(StubProvider("a"), StubProvider("b"))
        names = [p.name for p in registry.select_providers(preferred="a")]
        assert names.count("a") == 1

    def test_preferred_name_is_normalized(self):
        registry = _registry(StubProvider("a"), StubProvider("b"))
        assert [p.name for p in registry.select_providers(preferred="  A ")][0] == "a"

    def test_unknown_preferred_falls_back_to_order(self):
        registry = _registry(StubProvider("a"), StubProvider("b"))
        assert [p.name for p in registry.select_providers(preferred="nope")] == ["a", "b"]

    def test_unavailable_preferred_skipped_but_chain_continues(self):
        registry = _registry(StubProvider("a", available=False), StubProvider("b"))
        names = [p.name for p in registry.select_providers(preferred="a")]
        assert names == ["b"]

    def test_registry_reads_an_injected_mapping_live(self):
        backing = {}
        registry = ProviderRegistry(fallback_order=("late",), providers=backing)
        assert list(registry.select_providers()) == []
        backing["late"] = StubProvider("late")
        assert [p.name for p in registry.select_providers()] == ["late"]
        assert registry.providers is backing


class TestCompleteText:
    def test_first_success_wins(self):
        first = StubProvider("first")
        second = StubProvider("second")
        registry = _registry(first, second)
        result = registry.complete_text(system="s", prompt="p")
        assert result.provider == "first"
        assert second.calls == 0

    def test_failure_falls_through_to_next_provider(self):
        failing = StubProvider("failing", error=RuntimeError("boom"))
        healthy = StubProvider("healthy")
        registry = _registry(failing, healthy)
        result = registry.complete_text(system="s", prompt="p")
        assert result.provider == "healthy"
        assert failing.calls == 1

    def test_all_failures_raise_with_last_error_truncated(self):
        first = StubProvider("first", error=RuntimeError("early failure"))
        second = StubProvider("second", error=RuntimeError("x" * 300))
        registry = _registry(first, second)
        with pytest.raises(NoProviderAvailable) as excinfo:
            registry.complete_text(system="s", prompt="p")
        assert str(excinfo.value) == "x" * 200
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_nothing_configured_raises_without_cause(self):
        registry = ProviderRegistry(fallback_order=("a",))
        with pytest.raises(NoProviderAvailable) as excinfo:
            registry.complete_text(system="s", prompt="p")
        assert str(excinfo.value) == "no provider configured"
        assert excinfo.value.__cause__ is None

    def test_preferred_env_var_supplies_the_preference(self, monkeypatch):
        registry = _registry(
            StubProvider("a"), StubProvider("b"), preferred_env_var="BRIEFCASE_TEST_PROVIDER"
        )
        monkeypatch.setenv("BRIEFCASE_TEST_PROVIDER", "b")
        assert registry.complete_text(system="s", prompt="p").provider == "b"

    def test_explicit_preferred_beats_env_var(self, monkeypatch):
        registry = _registry(
            StubProvider("a"), StubProvider("b"), preferred_env_var="BRIEFCASE_TEST_PROVIDER"
        )
        monkeypatch.setenv("BRIEFCASE_TEST_PROVIDER", "b")
        assert registry.complete_text(system="s", prompt="p", preferred="a").provider == "a"


class TestResolveMode:
    def test_first_available_name(self):
        registry = _registry(StubProvider("a", available=False), StubProvider("b"))
        assert registry.resolve_mode() == "b"
        assert registry.resolve_mode(preferred="b") == "b"

    def test_fallback_label_when_nothing_available(self):
        registry = ProviderRegistry(fallback_order=("a",), fallback_label="rule_based")
        assert registry.resolve_mode() == "rule_based"
        assert registry.resolve_mode(preferred="a") == "rule_based"


class TestScopedCredential:
    def test_env_var_supplies_platform_credential_without_scope(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_A", "platform-a")
        assert resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A") == "platform-a"
        assert scoped_provider_name() is None

    def test_missing_env_var_resolves_to_none(self, monkeypatch):
        monkeypatch.delenv("BRIEFCASE_TEST_KEY_A", raising=False)
        assert resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A") is None

    def test_scoped_credential_wins_for_its_provider(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_A", "platform-a")
        with scoped_credential("prov_a", "tenant-a"):
            assert resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A") == "tenant-a"
            assert scoped_provider_name() == "prov_a"
        assert resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A") == "platform-a"
        assert scoped_provider_name() is None

    def test_scope_for_one_provider_blocks_anothers_platform_credential(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_B", "platform-b")
        with scoped_credential("prov_a", "tenant-a"):
            assert resolve_credential("prov_b", "BRIEFCASE_TEST_KEY_B") is None
        assert resolve_credential("prov_b", "BRIEFCASE_TEST_KEY_B") == "platform-b"

    def test_none_or_empty_arguments_are_a_no_op(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_A", "platform-a")
        with scoped_credential(None, None):
            assert resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A") == "platform-a"
        with scoped_credential("prov_a", ""):
            assert scoped_provider_name() is None

    def test_provider_name_is_normalized(self):
        with scoped_credential("  Prov_A ", "tenant-a"):
            assert resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A") == "tenant-a"

    def test_scope_does_not_leak_into_a_new_thread(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_A", "platform-a")
        seen = {}

        def worker():
            seen["value"] = resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A")

        with scoped_credential("prov_a", "tenant-a"):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        assert seen["value"] == "platform-a"

    def test_concurrent_threads_keep_their_own_scopes(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_A", "platform-a")
        barrier = threading.Barrier(2)
        seen = {}

        def worker(label, credential):
            with scoped_credential("prov_a", credential):
                barrier.wait(timeout=5)
                seen[label] = resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A")

        threads = [
            threading.Thread(target=worker, args=("one", "tenant-1")),
            threading.Thread(target=worker, args=("two", "tenant-2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert seen == {"one": "tenant-1", "two": "tenant-2"}

    def test_concurrent_asyncio_tasks_keep_their_own_scopes(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_A", "platform-a")

        async def worker(credential):
            with scoped_credential("prov_a", credential):
                await asyncio.sleep(0)
                return resolve_credential("prov_a", "BRIEFCASE_TEST_KEY_A")

        async def main():
            return await asyncio.gather(worker("tenant-1"), worker("tenant-2"))

        assert asyncio.run(main()) == ["tenant-1", "tenant-2"]

    def test_ladder_skips_a_provider_locked_out_by_anothers_scope(self, monkeypatch):
        """End to end: a scoped credential for one provider makes a provider
        gated on another provider's platform key unavailable."""
        monkeypatch.setenv("BRIEFCASE_TEST_KEY_B", "platform-b")

        class EnvGatedProvider(StubProvider):
            def __init__(self, name, env_var):
                super().__init__(name)
                self._env_var = env_var

            def available(self):
                return resolve_credential(self.name, self._env_var) is not None

        prov_b = EnvGatedProvider("prov_b", "BRIEFCASE_TEST_KEY_B")
        registry = _registry(prov_b, fallback_order=("prov_b",))
        assert registry.resolve_mode() == "prov_b"
        with scoped_credential("prov_a", "tenant-a"):
            assert registry.resolve_mode() == "unavailable"
