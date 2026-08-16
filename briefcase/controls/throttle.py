"""
Provider-throttle classification.

One classifier for every provider SDK's way of saying "slow down":
exception class names (openai/anthropic RateLimitError, ThrottlingException),
botocore error codes, HTTP status codes on attached responses, and the
exception cause chain. 503s and ServiceQuotaExceededException classify as
transient only; they signal retry with backoff, never a quota cooldown.
The message-text regex is opt-in because it can misfire on unrelated errors
that merely mention rate limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_THROTTLED_CLASS_NAMES = frozenset(
    {"RateLimitError", "ThrottlingException", "TooManyRequestsException"}
)
_THROTTLED_CODES = frozenset(
    {"ThrottlingException", "Throttling", "TooManyRequestsException"}
)
_TRANSIENT_ONLY_CODES = frozenset({"ServiceQuotaExceededException"})
_MESSAGE_REGEX = re.compile(r"throttl|rate.?limit", re.IGNORECASE)
_MAX_CAUSE_DEPTH = 8


@dataclass(frozen=True)
class ThrottleClassification:
    """throttled: the provider rejected for rate; cooldown-worthy.
    transient: worth retrying with backoff (every throttle is transient)."""

    throttled: bool
    transient: bool


def _status_code(err: BaseException) -> int | None:
    response = getattr(err, "response", None)
    if response is None:
        return None
    if isinstance(response, dict):
        meta = response.get("ResponseMetadata") or {}
        code = meta.get("HTTPStatusCode")
        return code if isinstance(code, int) else None
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _error_code(err: BaseException) -> str | None:
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = error.get("Code")
        return code if isinstance(code, str) else None
    return None


def _classify_single(err: BaseException, message_regex: bool) -> ThrottleClassification:
    name = type(err).__name__
    code = _error_code(err)
    status = _status_code(err)

    throttled = (
        name in _THROTTLED_CLASS_NAMES
        or (code in _THROTTLED_CODES)
        or status == 429
    )
    transient_only = (code in _TRANSIENT_ONLY_CODES) or status == 503

    if not throttled and message_regex:
        message = str(err)
        if _MESSAGE_REGEX.search(message):
            throttled = True

    return ThrottleClassification(
        throttled=throttled, transient=throttled or transient_only
    )


def classify_provider_error(
    err: BaseException, *, message_regex: bool = False
) -> ThrottleClassification:
    """Classify a provider error, traversing both the __cause__ and
    __context__ branch of every node (bounded, cycle-safe): a throttle behind
    an implicit exception context is found even when an explicit cause is
    also set.

    Args:
        message_regex: When True, also match "throttl"/"rate limit" in the
            message text. Off by default; a match opens quota cooldowns in the
            gateway, so enable it only where wrapped errors hide their type.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [err]
    visited = 0
    combined = ThrottleClassification(throttled=False, transient=False)
    while stack and visited < _MAX_CAUSE_DEPTH:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        visited += 1
        single = _classify_single(current, message_regex)
        combined = ThrottleClassification(
            throttled=combined.throttled or single.throttled,
            transient=combined.transient or single.transient,
        )
        if combined.throttled:
            break
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)
    return combined
