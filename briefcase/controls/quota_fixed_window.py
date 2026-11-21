"""
In-process fixed-window quota store.

Per-(tenant, bucket) counters in one process, guarded by a lock; with N
processes the effective ceiling is N times the limit. Suited to stopping
stuck retry loops and double submits, not to rationing spend. A shared store
(Redis or a database) can implement the same QuotaStore Protocol later
without changing call sites.

An invalid policy fails CLOSED: ``acquire`` denies rather than letting an
unlimited number of calls through a misconfigured bucket (a missing key, a
non-numeric or non-finite value, ``limit`` below 1, ``window_s`` at or below
zero). It denies instead of raising because a gateway whose error policy is
"allow" treats a raising store as an outage and lets the call through. This
matches ``MemoryQuotaStore`` in the TypeScript package. Only an unexpected
internal error still fails open: a limiter bug must never take down a real
request.

Idle state is evicted: a window that has run its span is indistinguishable
from one never seen, so it and any lapsed cooldown are dropped by a periodic
sweep (and by ``evict_expired``).
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from briefcase._logging import get_logger
from briefcase.controls.ports import QuotaDecision

logger = get_logger(__name__)

# Between timed sweeps, a store holding more than this many entries also
# sweeps every _PRUNE_EVERY acquires (amortized constant time), and past the
# hard bound on every acquire, so a burst of distinct keys cannot outrun the
# timer.
_PRUNE_THRESHOLD = 2048
_PRUNE_EVERY = 256
_PRUNE_HARD_BOUND = 2 * _PRUNE_THRESHOLD

_DEFAULT_SWEEP_INTERVAL_S = 60.0


def _parse_policy(policy: Mapping[str, Any]) -> Optional[Tuple[int, float]]:
    """Return ``(limit, window_s)``, or None when the policy cannot ration."""
    try:
        limit = float(policy["limit"])
        window_s = float(policy["window_s"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(limit) or limit < 1:
        return None
    if not math.isfinite(window_s) or window_s <= 0:
        return None
    return int(limit), window_s


def _wall_now() -> float:
    return datetime.now(timezone.utc).timestamp()


class FixedWindowQuotaStore:
    """QuotaStore over in-process fixed windows.

    Policy keys: ``limit`` (calls per window, >= 1) and ``window_s`` (seconds,
    > 0); an invalid policy is denied. ``mark_cooldown`` records a deny-until
    deadline per (tenant, bucket). ``sweep_interval_s`` is the minimum time
    between automatic eviction sweeps.
    """

    def __init__(self, *, sweep_interval_s: float = _DEFAULT_SWEEP_INTERVAL_S) -> None:
        self._lock = threading.Lock()
        # (tenant, bucket) -> (window_start, count, window_s)
        self._windows: Dict[Tuple[str, str], Tuple[float, int, float]] = {}
        # (tenant, bucket) -> cooldown expiry (epoch seconds)
        self._cooldowns: Dict[Tuple[str, str], float] = {}
        self._sweep_interval_s = float(sweep_interval_s)
        self._last_sweep: Optional[float] = None
        self._acquires_since_prune = 0

    @property
    def size(self) -> int:
        """Number of windows currently held (cooldowns excluded)."""
        with self._lock:
            return len(self._windows)

    def acquire(
        self,
        *,
        tenant_id: str,
        bucket: str,
        policy: Mapping[str, Any],
        ctx: Any = None,
        now: Optional[float] = None,
    ) -> QuotaDecision:
        parsed = _parse_policy(policy)
        if parsed is None:
            logger.warning(
                "FixedWindowQuotaStore.acquire: invalid policy for bucket %r "
                "(need limit >= 1 and window_s > 0); denying the call",
                bucket,
            )
            return QuotaDecision(allowed=False, tokens_remaining=0)
        limit, window_s = parsed
        try:
            ts = _wall_now() if now is None else float(now)
            key = (tenant_id, bucket)
            with self._lock:
                self._maybe_sweep(ts)
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
                self._maybe_sweep(ts)
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
            self._last_sweep = None
            self._acquires_since_prune = 0

    def evict_expired(self, now: Optional[float] = None) -> int:
        """Drop windows that have run their span and cooldowns that have
        lapsed. Runs automatically from ``acquire`` and ``mark_cooldown``;
        returns the number of entries removed."""
        ts = _wall_now() if now is None else float(now)
        with self._lock:
            return self._evict(ts)

    def _evict(self, ts: float) -> int:
        """Caller holds the lock. Each window is judged against the span it
        was stored with, so a short-window acquire never evicts a longer
        bucket's live window. Eviction never changes a decision: an evicted
        window is one ``acquire`` would reset to zero anyway, and an evicted
        cooldown one it would delete."""
        stale = [
            k
            for k, (start, _, entry_window_s) in self._windows.items()
            if ts - start >= entry_window_s
        ]
        for k in stale:
            del self._windows[k]
        expired = [k for k, expiry in self._cooldowns.items() if ts >= expiry]
        for k in expired:
            del self._cooldowns[k]
        self._last_sweep = ts
        self._acquires_since_prune = 0
        return len(stale) + len(expired)

    def _maybe_sweep(self, ts: float) -> None:
        """Sweep when the interval has elapsed, or sooner when the store is
        large. Caller holds the lock."""
        if self._last_sweep is None:
            self._last_sweep = ts
        elif ts < self._last_sweep:
            # A clock that moved backwards restarts the interval instead of
            # stalling it.
            self._last_sweep = ts
        elif ts - self._last_sweep >= self._sweep_interval_s:
            self._evict(ts)
            return
        held = len(self._windows) + len(self._cooldowns)
        if held <= _PRUNE_THRESHOLD:
            return
        self._acquires_since_prune += 1
        if held > _PRUNE_HARD_BOUND or self._acquires_since_prune >= _PRUNE_EVERY:
            self._evict(ts)
