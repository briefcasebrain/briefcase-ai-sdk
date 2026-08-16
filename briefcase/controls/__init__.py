"""
briefcase.controls: enforcement layer for AI invocations.

Ports (Protocols) for quota, entitlements, cache, and usage sinks; a gateway
composing hard-cap, quota, and throttle-cooldown checks around a model call;
a unified provider-throttle classifier with retry/backoff; and an in-process
fixed-window quota store. Every side effect goes through an injected port, so
applications keep their own storage, policies, and tenancy rules.
"""

from briefcase.controls.gateway import Gateway, GatewayConfig, Outcome
from briefcase.controls.providers import (
    LLMProvider,
    NoProviderAvailable,
    ProviderRegistry,
    TextCompletion,
    scoped_credential,
    scoped_provider_name,
)
from briefcase.controls.ports import (
    CacheEntry,
    CacheStore,
    EntitlementsHook,
    QuotaDecision,
    QuotaStore,
    UsageSink,
)
from briefcase.controls.quota_fixed_window import FixedWindowQuotaStore
from briefcase.controls.retry import compute_backoff, retry_call, retry_call_async
from briefcase.controls.throttle import ThrottleClassification, classify_provider_error

__all__ = [
    "CacheEntry",
    "CacheStore",
    "EntitlementsHook",
    "FixedWindowQuotaStore",
    "Gateway",
    "GatewayConfig",
    "LLMProvider",
    "NoProviderAvailable",
    "ProviderRegistry",
    "TextCompletion",
    "Outcome",
    "QuotaDecision",
    "QuotaStore",
    "ThrottleClassification",
    "UsageSink",
    "classify_provider_error",
    "scoped_credential",
    "scoped_provider_name",
    "compute_backoff",
    "retry_call",
    "retry_call_async",
]
