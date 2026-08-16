"""
The controls gateway: one enforced path for AI invocations.

Composition order: entitlements hard-cap, quota acquire, run the call, and on
a throttled failure open a bucket cooldown. The wrapped call only runs after
quota is acquired, so no path spends without a debit. Outcomes are typed;
port errors follow an explicit policy instead of an implicit one. When an
exporter is configured, each invocation emits a decision record carrying
semantic-convention attributes only, never the call's content.
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.controls.ports import QuotaDecision, QuotaStore
from briefcase.controls.throttle import classify_provider_error
from briefcase.semantic_conventions.controls import (
    CONTROLS_GATEWAY_BUCKET,
    CONTROLS_GATEWAY_OUTCOME,
    CONTROLS_GATEWAY_TENANT_ID,
    CONTROLS_QUOTA_COOLDOWN_UNTIL,
    CONTROLS_QUOTA_TOKENS_REMAINING,
)

logger = get_logger(__name__)

_DEFAULT_EVENT_NAMES: Dict[str, str] = {
    "ok": "gateway_ok",
    "hard_capped": "gateway_hard_capped",
    "quota_exhausted": "gateway_quota_exhausted",
    "throttled": "gateway_throttled",
    "internal": "gateway_internal_error",
}

PORT_ERROR_POLICIES = ("propagate", "allow", "deny")


@dataclass(frozen=True)
class Outcome:
    """Result of a gated invocation. ``reason`` is None on success, else one
    of "hard_capped", "quota_exhausted", "throttled", "internal"."""

    ok: bool
    value: Any = None
    reason: Optional[str] = None
    tokens_remaining: Optional[int] = None
    cooldown_until: Optional[datetime] = None
    cause: Optional[BaseException] = None


@dataclass
class GatewayConfig:
    quota_store: QuotaStore = None  # type: ignore[assignment]
    buckets: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    entitlements: Any = None
    cooldown_seconds: float = 3600.0
    exporter: Any = None
    await_export: bool = False
    on_outcome: Optional[Callable[[str, Dict[str, Any]], None]] = None
    event_names: Mapping[str, str] = field(default_factory=dict)
    port_error_policy: str = "propagate"
    message_regex_throttle: bool = False
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if self.port_error_policy not in PORT_ERROR_POLICIES:
            raise ValueError(
                f"port_error_policy must be one of {PORT_ERROR_POLICIES}, "
                f"got {self.port_error_policy!r}"
            )


class _GatewayExporter(ExportMixin):
    def __init__(self, exporter: Any, async_capture: bool) -> None:
        self._exporter = exporter
        self.async_capture = async_capture


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class Gateway:
    """Invoke AI calls behind hard-cap, quota, and cooldown enforcement."""

    def __init__(self, config: GatewayConfig) -> None:
        if config.quota_store is None:
            raise ValueError("GatewayConfig.quota_store is required")
        self._cfg = config
        self._exporter = _GatewayExporter(
            exporter=config.exporter, async_capture=not config.await_export
        )
        self._events = {**_DEFAULT_EVENT_NAMES, **dict(config.event_names)}

    async def invoke(
        self,
        *,
        tenant_id: str,
        bucket: str,
        fn: Callable[[], Any],
        ctx: Any = None,
        deadline_s: Optional[float] = None,
    ) -> Outcome:
        """Run ``fn`` behind hard-cap, quota, and cooldown enforcement.

        ``deadline_s`` bounds the await of an awaitable-returning ``fn``
        (a timeout surfaces as reason "internal" with a TimeoutError cause);
        a synchronous ``fn`` completes during the call itself, so no deadline
        can apply to it. Cancellation and interpreter-exit exceptions
        (CancelledError, KeyboardInterrupt, SystemExit) always propagate.
        A port error under the "propagate" policy is exported as an
        "internal" outcome record before it re-raises.
        """
        cfg = self._cfg
        if bucket not in cfg.buckets:
            raise ValueError(f"Unknown bucket {bucket!r}; configured: {sorted(cfg.buckets)}")
        policy = cfg.buckets[bucket]
        started_at = cfg.clock()

        try:
            capped = await self._hard_capped(tenant_id, ctx)
        except Exception as err:
            self._finish(
                Outcome(ok=False, reason="internal", cause=err),
                tenant_id, bucket, fn, started_at,
            )
            raise
        if capped:
            outcome = Outcome(
                ok=False, reason="hard_capped", tokens_remaining=0
            )
            return self._finish(outcome, tenant_id, bucket, fn, started_at)

        try:
            decision = await self._acquire(tenant_id, bucket, policy, ctx)
        except Exception as err:
            self._finish(
                Outcome(ok=False, reason="internal", cause=err),
                tenant_id, bucket, fn, started_at,
            )
            raise
        if not decision.allowed:
            outcome = Outcome(
                ok=False,
                reason="quota_exhausted",
                tokens_remaining=decision.tokens_remaining,
                cooldown_until=decision.cooldown_until,
            )
            return self._finish(outcome, tenant_id, bucket, fn, started_at)

        try:
            raw = fn()
            if inspect.isawaitable(raw):
                if deadline_s is not None:
                    import asyncio

                    value = await asyncio.wait_for(raw, timeout=deadline_s)
                else:
                    value = await raw
            else:
                value = raw
            outcome = Outcome(
                ok=True, value=value, tokens_remaining=decision.tokens_remaining
            )
        # Exception, not BaseException: CancelledError, KeyboardInterrupt,
        # and SystemExit must propagate, never convert into an Outcome.
        except Exception as err:
            classification = classify_provider_error(
                err, message_regex=cfg.message_regex_throttle
            )
            if classification.throttled:
                cooldown_marked = True
                try:
                    await _maybe_await(
                        cfg.quota_store.mark_cooldown(
                            tenant_id=tenant_id,
                            bucket=bucket,
                            seconds=cfg.cooldown_seconds,
                            ctx=ctx,
                        )
                    )
                except Exception:
                    cooldown_marked = False
                    logger.warning(
                        "mark_cooldown failed after a throttle", exc_info=True
                    )
                outcome = Outcome(
                    ok=False,
                    reason="throttled",
                    tokens_remaining=0,
                    # Advisory deadline on the gateway clock; the store's own
                    # clock governs the enforced cooldown. None when marking
                    # failed, so the outcome never asserts a cooldown that
                    # does not exist.
                    cooldown_until=(
                        cfg.clock() + timedelta(seconds=cfg.cooldown_seconds)
                        if cooldown_marked
                        else None
                    ),
                    cause=err,
                )
            else:
                outcome = Outcome(ok=False, reason="internal", cause=err)
        return self._finish(outcome, tenant_id, bucket, fn, started_at)

    #  Port calls under the configured error policy

    async def _hard_capped(self, tenant_id: str, ctx: Any) -> bool:
        cfg = self._cfg
        if cfg.entitlements is None:
            return False
        try:
            return bool(
                await _maybe_await(
                    cfg.entitlements.is_hard_capped(tenant_id=tenant_id, ctx=ctx)
                )
            )
        except Exception:
            if cfg.port_error_policy == "propagate":
                raise
            logger.warning(
                "EntitlementsHook failed; policy=%s", cfg.port_error_policy,
                exc_info=True,
            )
            return cfg.port_error_policy == "deny"

    async def _acquire(
        self, tenant_id: str, bucket: str, policy: Mapping[str, Any], ctx: Any
    ) -> QuotaDecision:
        cfg = self._cfg
        try:
            return await _maybe_await(
                cfg.quota_store.acquire(
                    tenant_id=tenant_id, bucket=bucket, policy=policy, ctx=ctx
                )
            )
        except Exception:
            if cfg.port_error_policy == "propagate":
                raise
            logger.warning(
                "QuotaStore.acquire failed; policy=%s", cfg.port_error_policy,
                exc_info=True,
            )
            if cfg.port_error_policy == "deny":
                return QuotaDecision(allowed=False, tokens_remaining=0)
            return QuotaDecision(allowed=True, tokens_remaining=None)

    #  Outcome reporting

    def _finish(
        self,
        outcome: Outcome,
        tenant_id: str,
        bucket: str,
        fn: Callable[[], Any],
        started_at: datetime,
    ) -> Outcome:
        self._emit_event(outcome, tenant_id, bucket)
        self._export_record(outcome, tenant_id, bucket, fn, started_at)
        return outcome

    def _emit_event(self, outcome: Outcome, tenant_id: str, bucket: str) -> None:
        cfg = self._cfg
        if cfg.on_outcome is None:
            return
        key = "ok" if outcome.ok else (outcome.reason or "internal")
        payload = {
            "tenant_id": tenant_id,
            "bucket": bucket,
            "outcome": key,
            "tokens_remaining": outcome.tokens_remaining,
            "cooldown_until": outcome.cooldown_until.isoformat()
            if outcome.cooldown_until
            else None,
        }
        try:
            cfg.on_outcome(self._events.get(key, key), payload)
        except Exception:
            logger.warning("on_outcome callback failed", exc_info=True)

    def _export_record(
        self,
        outcome: Outcome,
        tenant_id: str,
        bucket: str,
        fn: Callable[[], Any],
        started_at: datetime,
    ) -> None:
        cfg = self._cfg
        # Resolves like @capture: the config exporter first, then the global
        # one wired by briefcase.observe(); with neither, no record is built.
        exporter = self._exporter._resolve_exporter()
        if exporter is None:
            return
        ended_at = cfg.clock()
        key = "ok" if outcome.ok else (outcome.reason or "internal")
        record = {
            "decision_id": str(uuid.uuid4()),
            "decision_type": f"controls.gateway.{bucket}",
            "function_name": getattr(fn, "__name__", "<callable>"),
            "inputs": {
                CONTROLS_GATEWAY_TENANT_ID: tenant_id,
                CONTROLS_GATEWAY_BUCKET: bucket,
            },
            "outputs": {
                CONTROLS_GATEWAY_OUTCOME: key,
                CONTROLS_QUOTA_TOKENS_REMAINING: outcome.tokens_remaining,
                CONTROLS_QUOTA_COOLDOWN_UNTIL: outcome.cooldown_until.isoformat()
                if outcome.cooldown_until
                else None,
            },
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "execution_time_ms": (ended_at - started_at).total_seconds() * 1000,
        }
        if not outcome.ok and outcome.cause is not None:
            record["error"] = type(outcome.cause).__name__
        self._exporter._trigger_export(record, exporter=exporter)
