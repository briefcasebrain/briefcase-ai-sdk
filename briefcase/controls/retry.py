"""
Retry with exponential backoff for provider calls.

Backoff is base * 2^attempt capped, plus bounded jitter. A deadline stops
retrying before the next sleep would pass it. Only errors the classifier
marks transient are retried; everything else raises immediately.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Optional

from briefcase.controls.throttle import ThrottleClassification, classify_provider_error

Classifier = Callable[[BaseException], ThrottleClassification]


def compute_backoff(
    attempt: int,
    *,
    base_s: float = 0.5,
    cap_s: float = 30.0,
    jitter_s: float = 0.5,
    rng: Callable[[], float] = random.random,
) -> float:
    """Delay before retry number ``attempt + 1`` (attempt is 0-based)."""
    delay = min(cap_s, base_s * (2**attempt))
    if jitter_s > 0:
        delay += rng() * jitter_s
    return delay


def _should_retry(
    err: BaseException, classify: Classifier, retry_throttled: bool
) -> bool:
    c = classify(err)
    if c.throttled:
        return retry_throttled
    return c.transient


def retry_call(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
    base_s: float = 0.5,
    cap_s: float = 30.0,
    jitter_s: float = 0.5,
    deadline_s: Optional[float] = None,
    classify: Classifier = classify_provider_error,
    retry_throttled: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Call ``fn`` until it succeeds, a non-retryable error raises, attempts
    run out, or the deadline (seconds on ``clock``, measured from entry) would
    be passed by the next backoff sleep."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    start = clock()
    last: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except BaseException as err:
            last = err
            if not _should_retry(err, classify, retry_throttled):
                raise
            if attempt + 1 >= max_attempts:
                raise
            delay = compute_backoff(
                attempt, base_s=base_s, cap_s=cap_s, jitter_s=jitter_s
            )
            if deadline_s is not None and (clock() - start) + delay > deadline_s:
                raise
            sleep(delay)
    raise last  # type: ignore[misc]  # pragma: no cover - loop returns or raises


async def retry_call_async(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    base_s: float = 0.5,
    cap_s: float = 30.0,
    jitter_s: float = 0.5,
    deadline_s: Optional[float] = None,
    classify: Classifier = classify_provider_error,
    retry_throttled: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Async variant of retry_call for coroutine-returning callables."""
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    start = clock()
    last: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except BaseException as err:
            last = err
            if not _should_retry(err, classify, retry_throttled):
                raise
            if attempt + 1 >= max_attempts:
                raise
            delay = compute_backoff(
                attempt, base_s=base_s, cap_s=cap_s, jitter_s=jitter_s
            )
            if deadline_s is not None and (clock() - start) + delay > deadline_s:
                raise
            await sleep(delay)
    raise last  # type: ignore[misc]  # pragma: no cover - loop returns or raises
