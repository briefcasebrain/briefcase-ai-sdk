"""
Ports for briefcase.controls.

Runtime-checkable Protocols the application implements over its own storage
and policy systems. Port methods may be sync or async; the gateway awaits
awaitable results. Every method accepts an opaque ``ctx`` keyword so an
adapter can thread a transaction or request context through without the SDK
knowing its type. Store policies travel as plain mappings interpreted by the
concrete store (a token bucket reads capacity/refill keys, a fixed window
reads limit/window keys), so one gateway serves either kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class QuotaDecision:
    """Result of a quota acquire: whether the call may proceed, tokens left
    after the decision, and any active cooldown deadline."""

    allowed: bool
    tokens_remaining: Optional[int] = None
    cooldown_until: Optional[datetime] = None


@runtime_checkable
class QuotaStore(Protocol):
    def acquire(
        self,
        *,
        tenant_id: str,
        bucket: str,
        policy: Mapping[str, Any],
        ctx: Any = None,
        now: Any = None,
    ) -> Any:
        """Debit one call from the tenant's bucket; returns QuotaDecision
        (directly or awaitably)."""
        ...

    def mark_cooldown(
        self,
        *,
        tenant_id: str,
        bucket: str,
        seconds: float,
        ctx: Any = None,
        now: Any = None,
    ) -> Any:
        """Open a cooldown on the bucket so subsequent acquires fail fast."""
        ...


@runtime_checkable
class EntitlementsHook(Protocol):
    def is_hard_capped(self, *, tenant_id: str, ctx: Any = None) -> Any:
        """True when the tenant's plan blocks any further AI spend."""
        ...


@dataclass(frozen=True)
class CacheEntry:
    """A cached suggestion row: the entry id in the backing store, the items,
    the raw source label, and the optional content fingerprint."""

    entry_id: Optional[str]
    items: List[Any] = field(default_factory=list)
    source: str = ""
    fingerprint: Optional[str] = None


@runtime_checkable
class CacheStore(Protocol):
    def read(
        self,
        *,
        tenant_id: str,
        scope_id: str,
        kind: str,
        ttl_hours: float,
        fingerprint: Optional[str] = None,
        ctx: Any = None,
    ) -> Any:
        """Latest matching entry or None; returns CacheEntry (directly or
        awaitably). The store returns the raw row; hit rules live in the
        caller."""
        ...

    def write(
        self,
        *,
        tenant_id: str,
        scope_id: str,
        kind: str,
        items: List[Any],
        source: str,
        fingerprint: Optional[str] = None,
        actor_id: Optional[str] = None,
        row_id: Optional[str] = None,
        ctx: Any = None,
    ) -> Any:
        """Append an entry. Row-id generation stays application-side via
        ``row_id``."""
        ...


@runtime_checkable
class UsageSink(Protocol):
    def capture(
        self,
        *,
        tenant_id: str,
        bucket: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        scope_id: Optional[str] = None,
        ctx: Any = None,
    ) -> Any:
        """Record token usage for one call. Implementations must never raise;
        the gateway does not guard this path."""
        ...
