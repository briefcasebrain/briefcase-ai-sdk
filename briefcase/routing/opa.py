"""Open Policy Agent (OPA) router.

OPARouter posts an input document to an OPA policy endpoint and maps the
response to a :class:`RoutingDecision`. Responses are cached with a
per-entry TTL, keyed on the SHA-256 of the input JSON. On timeout
(default 50 ms) or any other error the router falls back to
:class:`InternalRouter`, so routing never blocks on OPA availability.
A ``human_review`` decision emits a low-confidence event.

Requires ``httpx``: ``pip install briefcase-ai[opa]`` or
``pip install httpx``. The import is lazy, so this module loads without
httpx installed and fails with a clear error when the router is
constructed.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple

from briefcase.routing.base import BaseRouter, RoutingDecision
from briefcase.routing.internal import InternalRouter

_INSTALL_HINT = (
    "httpx is required for OPARouter. Install with "
    "'pip install briefcase-ai[opa]' "
    "or 'pip install httpx'."
)


def _require_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return httpx


class _TTLCache:
    """Decision cache with a per-entry time-to-live.

    Expired entries are removed on read.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[str, Tuple[RoutingDecision, float]] = {}

    def get(self, key: str) -> Optional[RoutingDecision]:
        entry = self._store.get(key)
        if entry is None:
            return None
        decision, inserted_at = entry
        if time.monotonic() - inserted_at > self._ttl:
            del self._store[key]
            return None
        return decision

    def set(self, key: str, value: RoutingDecision) -> None:
        self._store[key] = (value, time.monotonic())


class OPARouter(BaseRouter):
    """Route decisions by consulting an OPA policy endpoint.

    Args:
        endpoint: OPA policy query URL, e.g.
            "http://opa:8181/v1/data/briefcase/routing".
        timeout_ms: HTTP timeout in milliseconds (default 50).
        cache_ttl_seconds: TTL for the response cache (default 60).
        fallback_threshold: Confidence threshold for the InternalRouter
            used when OPA is unreachable.
    """

    def __init__(
        self,
        endpoint: str,
        timeout_ms: float = 50.0,
        cache_ttl_seconds: float = 60.0,
        fallback_threshold: float = 0.85,
    ) -> None:
        self._httpx = _require_httpx()
        self._endpoint = endpoint
        self._timeout_s = timeout_ms / 1000.0
        self._cache = _TTLCache(cache_ttl_seconds)
        self._fallback = InternalRouter(confidence_threshold=fallback_threshold)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_input(decision_context: Any) -> dict:
        """Build the OPA input document from a decision context."""
        confidence = getattr(decision_context, "confidence", None)
        ctx = getattr(decision_context, "context", None)
        context_version = (
            str(getattr(ctx, "version", "") or "")
            if ctx is not None
            else str(getattr(decision_context, "context_version", "") or "")
        )
        model_params = getattr(decision_context, "model_parameters", None)
        if isinstance(model_params, dict):
            model_name = model_params.get("model_name", "")
        else:
            model_name = str(getattr(model_params, "model_name", "") or "") if model_params else ""

        tags = getattr(decision_context, "tags", {}) or {}

        return {
            "confidence": float(confidence) if confidence is not None else None,
            "context_version": context_version,
            "model_name": model_name,
            "tags": tags,
        }

    @staticmethod
    def _cache_key(input_doc: dict) -> str:
        serialised = json.dumps(input_doc, sort_keys=True, default=str)
        return hashlib.sha256(serialised.encode()).hexdigest()

    # ------------------------------------------------------------------
    # BaseRouter interface
    # ------------------------------------------------------------------

    async def route(self, decision_context: Any) -> RoutingDecision:
        """Consult OPA; fall back to InternalRouter on timeout or error."""
        start = time.monotonic()

        input_doc = self._build_input(decision_context)
        cache_key = self._cache_key(input_doc)

        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        action = "human_review"
        reason = None
        source = "opa"

        try:
            async with self._httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(
                    self._endpoint,
                    json={"input": input_doc},
                )
                response.raise_for_status()
                data = response.json()
                result = data.get("result", {})
                action = result.get("action", "human_review")
                reason = result.get("reason")

        except Exception:
            # Any error (timeout, network, HTTP status) falls back to the
            # InternalRouter so routing keeps working without OPA.
            fallback_result = await self._fallback.route(decision_context)
            eval_time_ms = (time.monotonic() - start) * 1000.0
            decision = RoutingDecision(
                action=fallback_result.action,
                source="internal",
                eval_time_ms=eval_time_ms,
                reason="OPA unavailable; fallback to internal router",
            )
            await self._maybe_emit(decision, decision_context)
            return decision

        eval_time_ms = (time.monotonic() - start) * 1000.0
        decision = RoutingDecision(
            action=action,
            source=source,
            eval_time_ms=eval_time_ms,
            reason=reason,
        )
        self._cache.set(cache_key, decision)
        await self._maybe_emit(decision, decision_context)
        return decision

    @staticmethod
    async def _maybe_emit(decision: RoutingDecision, decision_context: Any) -> None:
        """Emit a low-confidence event when the action is human_review."""
        if decision.action == "human_review":
            try:
                from briefcase.events.emitter import emit_low_confidence
                confidence = getattr(decision_context, "confidence", 0.0) or 0.0
                threshold = 0.85  # local default; OPA defines its own threshold
                await emit_low_confidence(decision_context, float(confidence), threshold)
            except Exception:
                pass  # event emission never breaks routing
