"""Structural conformance checks for the controls port Protocols.

CacheStore and UsageSink have no in-package consumer yet (the suggestion
pipeline lives in the TypeScript package; a Python port arrives in a later
release). These checks pin the Protocol shapes so an
adapter written today keeps conforming.
"""

from briefcase.controls.ports import (
    CacheEntry,
    CacheStore,
    EntitlementsHook,
    QuotaStore,
    UsageSink,
)
from briefcase.controls.quota_fixed_window import FixedWindowQuotaStore


class SampleCacheStore:
    def read(self, *, tenant_id, scope_id, kind, ttl_hours, fingerprint=None, ctx=None):
        return None

    def write(self, *, tenant_id, scope_id, kind, items, source,
              fingerprint=None, actor_id=None, row_id=None, ctx=None):
        return None


class SampleUsageSink:
    def capture(self, *, tenant_id, bucket, model, input_tokens, output_tokens,
                scope_id=None, ctx=None):
        return None


class SampleEntitlements:
    def is_hard_capped(self, *, tenant_id, ctx=None):
        return False


def test_sample_adapters_satisfy_the_protocols():
    assert isinstance(SampleCacheStore(), CacheStore)
    assert isinstance(SampleUsageSink(), UsageSink)
    assert isinstance(SampleEntitlements(), EntitlementsHook)
    assert isinstance(FixedWindowQuotaStore(), QuotaStore)


def test_cache_entry_defaults():
    entry = CacheEntry(entry_id="e1")
    assert entry.items == []
    assert entry.source == ""
    assert entry.fingerprint is None
