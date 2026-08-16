"""Tests for briefcase.controls.gateway."""

import asyncio
from datetime import datetime

import pytest

from briefcase.controls.gateway import Gateway, GatewayConfig
from briefcase.controls.ports import QuotaDecision
from briefcase.exporters import MemoryExporter


class FakeQuotaStore:
    def __init__(self, allowed=True, tokens_remaining=5):
        self.allowed = allowed
        self.tokens_remaining = tokens_remaining
        self.acquires = []
        self.cooldowns = []

    def acquire(self, *, tenant_id, bucket, policy, ctx=None, now=None):
        self.acquires.append((tenant_id, bucket, dict(policy)))
        return QuotaDecision(
            allowed=self.allowed, tokens_remaining=self.tokens_remaining
        )

    def mark_cooldown(self, *, tenant_id, bucket, seconds, ctx=None, now=None):
        self.cooldowns.append((tenant_id, bucket, seconds))


class ThrottlingException(Exception):
    pass


BUCKETS = {"chat": {"limit": 5, "window_s": 60.0}}


def make_gateway(**overrides):
    cfg = dict(
        quota_store=FakeQuotaStore(),
        buckets=BUCKETS,
    )
    cfg.update(overrides)
    config = GatewayConfig(**cfg)
    return Gateway(config), config


def run(coro):
    return asyncio.run(coro)


class TestGatewayComposition:
    def test_success_returns_value_and_tokens(self):
        gw, cfg = make_gateway()

        async def fn():
            return "answer"

        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))
        assert outcome.ok
        assert outcome.value == "answer"
        assert outcome.tokens_remaining == 5
        assert cfg.quota_store.acquires == [("org1", "chat", BUCKETS["chat"])]

    def test_sync_fn_supported(self):
        gw, _ = make_gateway()
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 42))
        assert outcome.ok and outcome.value == 42

    def test_hard_cap_short_circuits_before_quota(self):
        class Capped:
            def is_hard_capped(self, *, tenant_id, ctx=None):
                return True

        gw, cfg = make_gateway(entitlements=Capped())
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
        assert not outcome.ok
        assert outcome.reason == "hard_capped"
        assert cfg.quota_store.acquires == []

    def test_quota_denied_returns_exhausted_without_running_fn(self):
        store = FakeQuotaStore(allowed=False, tokens_remaining=0)
        ran = []
        gw, _ = make_gateway(quota_store=store)
        outcome = run(
            gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: ran.append(1))
        )
        assert not outcome.ok
        assert outcome.reason == "quota_exhausted"
        assert outcome.tokens_remaining == 0
        assert ran == []

    def test_throttle_marks_cooldown(self):
        store = FakeQuotaStore()

        def fn():
            raise ThrottlingException("slow down")

        gw, _ = make_gateway(quota_store=store, cooldown_seconds=1800)
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))
        assert not outcome.ok
        assert outcome.reason == "throttled"
        assert store.cooldowns == [("org1", "chat", 1800)]
        assert isinstance(outcome.cooldown_until, datetime)

    def test_internal_error_carries_cause(self):
        boom = ValueError("boom")

        def fn():
            raise boom

        gw, _ = make_gateway()
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))
        assert not outcome.ok
        assert outcome.reason == "internal"
        assert outcome.cause is boom

    def test_unknown_bucket_raises(self):
        gw, _ = make_gateway()
        with pytest.raises(ValueError):
            run(gw.invoke(tenant_id="org1", bucket="nope", fn=lambda: 1))

    def test_port_error_propagates_by_default(self):
        class Exploding:
            def is_hard_capped(self, *, tenant_id, ctx=None):
                raise RuntimeError("db down")

        gw, _ = make_gateway(entitlements=Exploding())
        with pytest.raises(RuntimeError):
            run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))

    def test_port_error_policy_allow_continues(self):
        class Exploding:
            def is_hard_capped(self, *, tenant_id, ctx=None):
                raise RuntimeError("db down")

        gw, _ = make_gateway(entitlements=Exploding(), port_error_policy="allow")
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 7))
        assert outcome.ok and outcome.value == 7

    def test_port_error_policy_deny_blocks(self):
        class Exploding:
            def is_hard_capped(self, *, tenant_id, ctx=None):
                raise RuntimeError("db down")

        gw, _ = make_gateway(entitlements=Exploding(), port_error_policy="deny")
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 7))
        assert not outcome.ok
        assert outcome.reason == "hard_capped"

    def test_async_quota_store_supported(self):
        class AsyncStore:
            def __init__(self):
                self.cooldowns = []

            async def acquire(self, *, tenant_id, bucket, policy, ctx=None, now=None):
                return QuotaDecision(allowed=True, tokens_remaining=9)

            async def mark_cooldown(self, *, tenant_id, bucket, seconds, ctx=None, now=None):
                self.cooldowns.append(seconds)

        gw, _ = make_gateway(quota_store=AsyncStore())
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
        assert outcome.ok and outcome.tokens_remaining == 9


class TestGatewayRecords:
    def test_exports_record_with_wire_schema_fields(self):
        mem = MemoryExporter()
        gw, _ = make_gateway(exporter=mem, await_export=True)

        run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: "v"))
        assert len(mem.records) == 1
        rec = mem.records[0]
        for field in (
            "decision_id",
            "decision_type",
            "function_name",
            "inputs",
            "outputs",
            "started_at",
            "ended_at",
            "execution_time_ms",
        ):
            assert field in rec, field
        assert rec["outputs"]["briefcase.controls.gateway.outcome"] == "ok"

    def test_no_exporter_means_no_export(self):
        gw, _ = make_gateway()
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
        assert outcome.ok

    def test_record_never_contains_fn_result_content(self):
        mem = MemoryExporter()
        gw, _ = make_gateway(exporter=mem, await_export=True)
        run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: "secret-payload"))
        assert "secret-payload" not in repr(mem.records)

    def test_on_outcome_callback_fires_with_event_name(self):
        events = []
        gw, _ = make_gateway(on_outcome=lambda name, payload: events.append((name, payload)))
        run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
        assert len(events) == 1
        name, payload = events[0]
        assert name == "gateway_ok"
        assert payload["bucket"] == "chat"
        assert payload["tenant_id"] == "org1"

    def test_event_names_are_injectable(self):
        events = []
        gw, _ = make_gateway(
            event_names={"ok": "ai_invoke_ok"},
            on_outcome=lambda name, payload: events.append(name),
        )
        run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
        assert events == ["ai_invoke_ok"]


class TestGatewayGlobalExporter:
    def test_observe_wires_the_gateway_like_capture(self):
        import briefcase
        from briefcase.config import BriefcaseConfig

        BriefcaseConfig.reset()
        try:
            mem = briefcase.observe("memory")
            gw, _ = make_gateway(await_export=True)
            run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: "v"))
            assert len(mem.records) == 1
            assert mem.records[0]["decision_type"] == "controls.gateway.chat"
        finally:
            BriefcaseConfig.reset()

    def test_no_exporter_anywhere_builds_no_record(self):
        from briefcase.config import BriefcaseConfig

        BriefcaseConfig.reset()
        try:
            gw, _ = make_gateway()
            outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
            assert outcome.ok
        finally:
            BriefcaseConfig.reset()


class TestGatewayOutcomeFixtureParity:
    def test_event_names_and_record_errors_match_the_shared_fixture(self):
        import json
        from pathlib import Path

        fixture = json.loads(
            (Path(__file__).parent.parent / "fixtures" / "gateway_outcomes.json").read_text()
        )
        cases = {o["key"]: o for o in fixture["outcomes"]}
        assert set(cases) == {
            "ok", "hard_capped", "quota_exhausted", "throttled", "internal"
        }

        def drive(key):
            events = []
            mem = MemoryExporter()
            kwargs = dict(
                exporter=mem,
                await_export=True,
                on_outcome=lambda name, payload: events.append(name),
            )
            if key == "hard_capped":
                class Capped:
                    def is_hard_capped(self, *, tenant_id, ctx=None):
                        return True

                gw, _ = make_gateway(entitlements=Capped(), **kwargs)
                fn = lambda: 1  # noqa: E731
            elif key == "quota_exhausted":
                gw, _ = make_gateway(
                    quota_store=FakeQuotaStore(allowed=False, tokens_remaining=0),
                    **kwargs,
                )
                fn = lambda: 1  # noqa: E731
            elif key == "throttled":
                gw, _ = make_gateway(**kwargs)

                def fn():
                    raise ThrottlingException("slow down")
            elif key == "internal":
                gw, _ = make_gateway(**kwargs)

                def fn():
                    raise ValueError("boom")
            else:
                gw, _ = make_gateway(**kwargs)
                fn = lambda: 1  # noqa: E731
            run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))
            return events, mem.records

        for key, case in cases.items():
            events, records = drive(key)
            assert events == [case["event"]], key
            assert len(records) == 1, key
            if case["record_error"] is None:
                assert "error" not in records[0], key
            else:
                expected = {
                    "throttled": "ThrottlingException",
                    "internal": "ValueError",
                }[key]
                assert records[0]["error"] == expected, key


class TestGatewayExceptionScope:
    def test_cancelled_error_propagates_not_converted(self):
        async def fn():
            raise asyncio.CancelledError()

        gw, _ = make_gateway()
        with pytest.raises(asyncio.CancelledError):
            run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))

    def test_keyboard_interrupt_propagates(self):
        def fn():
            raise KeyboardInterrupt()

        gw, _ = make_gateway()
        with pytest.raises(KeyboardInterrupt):
            run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))

    def test_deadline_applies_to_awaitable_returning_fn(self):
        def fn():
            # Not a coroutine function itself; returns an awaitable.
            return asyncio.sleep(5)

        gw, _ = make_gateway()
        outcome = run(
            gw.invoke(tenant_id="org1", bucket="chat", fn=fn, deadline_s=0.01)
        )
        assert not outcome.ok
        assert outcome.reason == "internal"
        assert isinstance(outcome.cause, (asyncio.TimeoutError, TimeoutError))

    def test_cooldown_until_is_none_when_marking_fails(self):
        class BrokenCooldownStore(FakeQuotaStore):
            def mark_cooldown(self, **kwargs):
                raise RuntimeError("store down")

        def fn():
            raise ThrottlingException("slow down")

        gw, _ = make_gateway(quota_store=BrokenCooldownStore())
        outcome = run(gw.invoke(tenant_id="org1", bucket="chat", fn=fn))
        assert outcome.reason == "throttled"
        assert outcome.cooldown_until is None

    def test_propagating_port_error_still_exports_a_record(self):
        class Exploding:
            def is_hard_capped(self, *, tenant_id, ctx=None):
                raise RuntimeError("db down")

        mem = MemoryExporter()
        gw, _ = make_gateway(
            entitlements=Exploding(), exporter=mem, await_export=True
        )
        with pytest.raises(RuntimeError):
            run(gw.invoke(tenant_id="org1", bucket="chat", fn=lambda: 1))
        assert len(mem.records) == 1
        rec = mem.records[0]
        assert rec["outputs"]["briefcase.controls.gateway.outcome"] == "internal"
        assert rec["error"] == "RuntimeError"
