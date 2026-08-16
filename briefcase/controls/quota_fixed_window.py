"""
In-process fixed-window quota store.

Per-(tenant, bucket) counters in one process, guarded by a lock; with N
processes the effective ceiling is N times the limit. Suited to stopping
stuck retry loops and double submits, not to rationing spend. Acquire fails
open on internal errors: a limiter bug must never take down a real request.
A shared store (Redis or a database) can implement the same QuotaStore
Protocol later without changing call sites.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from briefcase._logging import get_logger
from briefcase.controls.ports import QuotaDecision

logger = get_logger(__name__)

_PRUNE_THRESHOLD = 2048
# Above the threshold, the O(n) prune scan runs every this-many acquires so
# a busy store pays amortized constant time; past the hard bound it runs on
# every acquire so memory stays bounded.
_PRUNE_EVERY = 256
_PRUNE_HARD_BOUND = 2 * _PRUNE_THRESHOLD


def _wall_now() -> float:
    return datetime.now(timezone.utc).timestamp()


class FixedWindowQuotaStore:
    """QuotaStore over in-process fixed windows.

    Policy keys: ``limit`` (calls per window) and ``window_s`` (seconds).
    ``mark_cooldown`` records a deny-until deadline per (tenant, bucket).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (tenant, bucket) -> (window_start, count, window_s)
        self._windows: Dict[Tuple[str, str], Tuple[float, int, float]] = {}
        # (tenant, bucket) -> cooldown expiry (epoch seconds)
        self._cooldowns: Dict[Tuple[str, str], float] = {}
        self._acquires_since_prune = 0

    def acquire(
        self,
        *,
        tenant_id: str,
        bucket: str,
        policy: Mapping[str, Any],
        ctx: Any = None,
        now: Optional[float] = None,
    ) -> QuotaDecision:
        try:
            ts = _wall_now() if now is None else float(now)
            limit = int(policy["limit"])
            window_s = float(policy["window_s"])
            key = (tenant_id, bucket)
            with self._lock:
                cooldown = self._cooldowns.get(key)
                if cooldown is not None:
                    if ts < cooldown:
                        return QuotaDecision(
                            allowed=False,
                            tokens_remaining=0,
                            cooldown_until=datetime.fromtimestamp(
                                cooldown, tz=timezone.utc
                            ),
                        )
                    del self._cooldowns[key]

                start, count, _ = self._windows.get(key, (ts, 0, window_s))
                if ts - start >= window_s:
                    start, count = ts, 0
                if count >= limit:
                    self._windows[key] = (start, count, window_s)
                    return QuotaDecision(allowed=False, tokens_remaining=0)
                count += 1
                self._windows[key] = (start, count, window_s)
                self._prune(ts)
                return QuotaDecision(
                    allowed=True, tokens_remaining=max(0, limit - count)
                )
        except Exception:
            logger.warning(
                "FixedWindowQuotaStore.acquire failed; allowing the call",
                exc_info=True,
            )
            return QuotaDecision(allowed=True, tokens_remaining=None)

    def mark_cooldown(
        self,
        *,
        tenant_id: str,
        bucket: str,
        seconds: float,
        ctx: Any = None,
        now: Optional[float] = None,
    ) -> None:
        try:
            ts = _wall_now() if now is None else float(now)
            with self._lock:
                self._cooldowns[(tenant_id, bucket)] = ts + float(seconds)
        except Exception:
            logger.warning(
                "FixedWindowQuotaStore.mark_cooldown failed; skipping",
                exc_info=True,
            )

    def reset(self) -> None:
        """Test hook: drop all windows and cooldowns."""
        with self._lock:
            self._windows.clear()
            self._cooldowns.clear()

    def _prune(self, ts: float) -> None:
        """Drop windows older than twice their own span, and expired
        cooldowns, once the dict grows past the threshold. Each entry is
        judged against the window span it was stored with, so a short-window
        acquire never evicts a longer bucket's live window. The scan is
        amortized: above the threshold it runs every _PRUNE_EVERY acquires,
        and past the hard bound on every acquire. Caller holds the lock."""
        if len(self._windows) <= _PRUNE_THRESHOLD:
            return
        self._acquires_since_prune += 1
        if (
            len(self._windows) <= _PRUNE_HARD_BOUND
            and self._acquires_since_prune < _PRUNE_EVERY
        ):
            return
        self._acquires_since_prune = 0
        stale = [
            k
            for k, (start, _, entry_window_s) in self._windows.items()
            if start < ts - 2 * entry_window_s
        ]
        for k in stale:
            del self._windows[k]
        expired = [k for k, expiry in self._cooldowns.items() if expiry < ts]
        for k in expired:
            del self._cooldowns[k]
