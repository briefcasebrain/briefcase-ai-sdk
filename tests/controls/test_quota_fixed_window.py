"""Tests for briefcase.controls.quota_fixed_window (FixedWindowQuotaStore)."""

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

    def test_fail_open_on_internal_error(self):
        store = make_store()
        # A policy missing its keys triggers the internal-error path.
        d = store.acquire(tenant_id="t1", bucket="draft", policy={}, now=100.0)
        assert d.allowed

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
