"""Tests for briefcase.controls.quota_fixed_window (FixedWindowQuotaStore)."""

import pytest

from briefcase.controls.quota_fixed_window import (
    _PRUNE_EVERY,
    _PRUNE_THRESHOLD,
    FixedWindowQuotaStore,
)

POLICY = {"limit": 3, "window_s": 60.0}


def make_store():
    return FixedWindowQuotaStore()


class TestFixedWindowQuotaStore:
    def test_allows_up_to_limit_in_one_window(self):
        store = make_store()
        for i in range(3):
            d = store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=100.0)
            assert d.allowed
            assert d.tokens_remaining == 3 - (i + 1)
        d = store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=100.0)
        assert not d.allowed
        assert d.tokens_remaining == 0

    def test_window_resets_after_expiry(self):
        store = make_store()
        for _ in range(3):
            store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=100.0)
        d = store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=161.0)
        assert d.allowed

    def test_buckets_and_tenants_are_isolated(self):
        store = make_store()
        for _ in range(3):
            store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=100.0)
        assert store.acquire(
            tenant_id="t2", bucket="draft", policy=POLICY, now=100.0
        ).allowed
        assert store.acquire(
            tenant_id="t1", bucket="polish", policy=POLICY, now=100.0
        ).allowed

    @pytest.mark.parametrize(
        "policy",
        [
            {},
            {"limit": 3},
            {"window_s": 60.0},
            {"limit": "many", "window_s": 60.0},
            {"limit": None, "window_s": 60.0},
            {"limit": 0, "window_s": 60.0},
            {"limit": -1, "window_s": 60.0},
            {"limit": float("nan"), "window_s": 60.0},
            {"limit": float("inf"), "window_s": 60.0},
            {"limit": 3, "window_s": 0},
            {"limit": 3, "window_s": -5.0},
            {"limit": 3, "window_s": float("nan")},
            {"limit": 3, "window_s": float("inf")},
            {"limit": 3, "window_s": "soon"},
        ],
    )
    def test_invalid_policy_fails_closed(self, policy, caplog):
        # Every one of these used to be allowed without limit: a missing key
        # raised into the fail-open handler, and window_s <= 0 reset the
        # counter on each call.
        store = make_store()
        with caplog.at_level("WARNING", logger="briefcase"):
            for _ in range(5):
                d = store.acquire(tenant_id="t1", bucket="draft", policy=policy, now=100.0)
                assert not d.allowed
                assert d.tokens_remaining == 0
        assert store.size == 0
        assert any("invalid policy" in r.getMessage() for r in caplog.records)

    def test_invalid_policy_is_denied_through_a_gateway_that_allows_on_port_errors(self):
        # Denying, not raising, is what keeps the gateway's "allow" error
        # policy from turning a bad policy into an unlimited bucket.
        import asyncio

        from briefcase.controls.gateway import Gateway, GatewayConfig

        gw = Gateway(GatewayConfig(
            quota_store=make_store(),
            buckets={"chat": {"limit": 3}},
            port_error_policy="allow",
        ))
        calls = []
        outcome = asyncio.run(
            gw.invoke(tenant_id="t1", bucket="chat", fn=lambda: calls.append(1))
        )
        assert not outcome.ok and outcome.reason == "quota_exhausted"
        assert calls == []

    def test_numeric_strings_and_fractional_limits_are_accepted(self):
        store = make_store()
        policy = {"limit": "2", "window_s": "60"}
        assert store.acquire(tenant_id="t1", bucket="d", policy=policy, now=100.0).allowed
        assert store.acquire(tenant_id="t1", bucket="d", policy=policy, now=100.0).allowed
        assert not store.acquire(tenant_id="t1", bucket="d", policy=policy, now=100.0).allowed

    def test_unexpected_internal_error_still_fails_open(self):
        store = make_store()
        store._windows = None  # simulate a limiter bug
        d = store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=100.0)
        assert d.allowed and d.tokens_remaining is None

    def test_cooldown_denies_until_expiry(self):
        store = make_store()
        store.mark_cooldown(tenant_id="t1", bucket="draft", seconds=30.0, now=100.0)
        d = store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=110.0)
        assert not d.allowed
        assert d.cooldown_until is not None
        d = store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=131.0)
        assert d.allowed

    def test_reset_clears_state(self):
        store = make_store()
        for _ in range(3):
            store.acquire(tenant_id="t1", bucket="draft", policy=POLICY, now=100.0)
        store.reset()
        assert store.acquire(
            tenant_id="t1", bucket="draft", policy=POLICY, now=100.0
        ).allowed

    def test_prune_bounds_state_size(self):
        store = make_store()
        seeded = _PRUNE_THRESHOLD + 200
        for i in range(seeded):
            store.acquire(
                tenant_id=f"t{i}", bucket="draft", policy=POLICY, now=100.0
            )
        # Entries far older than the window are pruned once the dict grows;
        # the scan is amortized, so acquire past one full cadence.
        for i in range(_PRUNE_EVERY + 1):
            store.acquire(
                tenant_id=f"fresh{i}", bucket="draft", policy=POLICY, now=100000.0
            )
        assert len(store._windows) < seeded

    def test_prune_never_evicts_a_longer_buckets_live_window(self):
        store = make_store()
        day_policy = {"limit": 2, "window_s": 86400.0}
        # Exhaust the day bucket mid-window.
        store.acquire(tenant_id="tday", bucket="daily", policy=day_policy, now=100.0)
        store.acquire(tenant_id="tday", bucket="daily", policy=day_policy, now=100.0)
        assert not store.acquire(
            tenant_id="tday", bucket="daily", policy=day_policy, now=700.0
        ).allowed
        # Grow past the prune threshold and a full amortization cadence with
        # short-window acquires 10 minutes later; the day bucket's window is
        # live and must survive the scans that fire.
        for i in range(_PRUNE_THRESHOLD + _PRUNE_EVERY + 10):
            store.acquire(tenant_id=f"t{i}", bucket="draft", policy=POLICY, now=700.0)
        assert not store.acquire(
            tenant_id="tday", bucket="daily", policy=day_policy, now=701.0
        ).allowed

    def test_prune_drops_expired_cooldowns(self):
        store = make_store()
        store.mark_cooldown(tenant_id="cold", bucket="draft", seconds=1.0, now=100.0)
        for i in range(_PRUNE_THRESHOLD + _PRUNE_EVERY + 10):
            store.acquire(tenant_id=f"t{i}", bucket="draft", policy=POLICY, now=500.0)
        assert ("cold", "draft") not in store._cooldowns


class TestEviction:
    def test_expired_windows_are_swept_below_the_size_threshold(self):
        # The old prune ran only past 2048 entries, so a small store kept
        # every expired window for the life of the process.
        store = FixedWindowQuotaStore(sweep_interval_s=1.0)
        for i in range(50):
            store.acquire(tenant_id=f"t{i}", bucket="draft", policy=POLICY, now=100.0)
        assert store.size == 50
        store.acquire(tenant_id="later", bucket="draft", policy=POLICY, now=100.0 + 61.0)
        assert store.size == 1

    def test_default_interval_is_sixty_seconds(self):
        store = FixedWindowQuotaStore()
        short = {"limit": 1, "window_s": 1.0}
        for i in range(10):
            store.acquire(tenant_id=f"t{i}", bucket="b", policy=short, now=100.0)
        store.acquire(tenant_id="x", bucket="b", policy=short, now=159.0)
        assert store.size == 11
        store.acquire(tenant_id="y", bucket="b", policy=short, now=161.0)
        assert store.size == 1  # x's window (started 159, span 1) lapsed too

    def test_cooldowns_alone_do_not_grow_without_bound(self):
        # Cooldowns were pruned only when the *windows* dict was large, so
        # marking many keys that never acquired leaked every entry.
        store = FixedWindowQuotaStore(sweep_interval_s=1.0)
        for i in range(500):
            store.mark_cooldown(tenant_id=f"t{i}", bucket="draft", seconds=5.0, now=100.0)
        assert len(store._cooldowns) == 500
        store.mark_cooldown(tenant_id="new", bucket="draft", seconds=5.0, now=200.0)
        assert set(store._cooldowns) == {("new", "draft")}

    def test_denied_acquires_also_sweep(self):
        store = FixedWindowQuotaStore(sweep_interval_s=1.0)
        day = {"limit": 1, "window_s": 86400.0}
        store.acquire(tenant_id="busy", bucket="daily", policy=day, now=100.0)
        for i in range(20):
            store.acquire(tenant_id=f"t{i}", bucket="draft", policy=POLICY, now=100.0)
        # Only denied calls from here on.
        assert not store.acquire(tenant_id="busy", bucket="daily", policy=day, now=500.0).allowed
        assert store.size == 1

    def test_evict_expired_reports_what_it_dropped_and_keeps_live_state(self):
        store = FixedWindowQuotaStore(sweep_interval_s=1e9)
        day = {"limit": 1, "window_s": 86400.0}
        store.acquire(tenant_id="a", bucket="draft", policy=POLICY, now=100.0)
        store.acquire(tenant_id="b", bucket="daily", policy=day, now=100.0)
        store.mark_cooldown(tenant_id="c", bucket="draft", seconds=10.0, now=100.0)
        store.mark_cooldown(tenant_id="d", bucket="draft", seconds=1000.0, now=100.0)
        assert store.evict_expired(now=100.0 + 59.0) == 1  # c's cooldown
        assert store.evict_expired(now=100.0 + 60.0) == 1  # a's window
        assert store.size == 1
        assert not store.acquire(tenant_id="b", bucket="daily", policy=day, now=200.0).allowed
        assert not store.acquire(tenant_id="d", bucket="draft", policy=POLICY, now=200.0).allowed

    def test_eviction_never_changes_a_decision(self):
        def run(sweep_interval_s):
            store = FixedWindowQuotaStore(sweep_interval_s=sweep_interval_s)
            decisions = []
            for step in range(400):
                now = 100.0 + step * 7.0
                if step % 37 == 0:
                    store.mark_cooldown(tenant_id="t1", bucket="draft", seconds=20.0, now=now)
                for tenant in ("t1", "t2", f"once{step}"):
                    d = store.acquire(tenant_id=tenant, bucket="draft", policy=POLICY, now=now)
                    decisions.append((d.allowed, d.tokens_remaining, d.cooldown_until))
            return decisions, store.size

        swept, swept_size = run(1.0)
        unswept, unswept_size = run(1e12)
        assert swept == unswept
        assert swept_size < 20 < unswept_size

    def test_backwards_clock_restarts_the_interval(self):
        store = FixedWindowQuotaStore(sweep_interval_s=10.0)
        store.acquire(tenant_id="a", bucket="draft", policy=POLICY, now=1000.0)
        store.acquire(tenant_id="b", bucket="draft", policy=POLICY, now=500.0)
        store.acquire(tenant_id="c", bucket="draft", policy=POLICY, now=511.0)
        assert store._last_sweep == 511.0
