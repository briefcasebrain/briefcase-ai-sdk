"""Webhook transport for Briefcase events.

POSTs :class:`briefcase.events.types.BriefcaseEvent` records as JSON to a
configured URL with:

- HMAC-SHA256 signature in the ``X-Briefcase-Signature`` header (omitted,
  with one WARNING, when no secret is configured)
- CloudEvents 1.0 headers on every request
- per-event-type subscription filtering
- up to 3 attempts on 5xx responses, with jittered exponential backoff
- configurable timeout (default 10 s)
- every failed delivery logged at WARNING

Uses only the standard library; no optional dependency is needed.

Transport security: plain http destinations are accepted only for
loopback hosts, since the signed payload would otherwise travel in
cleartext. Remote webhooks require https unless
``allow_insecure_http=True`` is passed. Redirects are never followed, so
a redirect can never downgrade a request to plain http.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import random
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from briefcase._logging import get_logger
from briefcase.events.types import BriefcaseEvent

logger = get_logger(__name__)


def _is_loopback_host(host: str) -> bool:
    """True for hosts that resolve to the local machine: "localhost" or a
    loopback IP (127.0.0.0/8, ::1)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses to follow any redirect."""

    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        return None


def _post(url: str, body: bytes, headers: Dict[str, str], timeout: float) -> int:
    """POST *body* to *url* and return the HTTP status code.

    Never follows redirects; a redirect response is returned as its own
    status code. Runs synchronously and is dispatched to a thread by
    :meth:`WebhookEmitter.emit`.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_RefuseRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as err:
        code = err.code
        err.close()
        return code


class WebhookEmitter:
    """POST BriefcaseEvents to a webhook endpoint.

    Args:
        url: Destination URL for POST requests. Plain http is rejected for
            non-loopback hosts unless ``allow_insecure_http`` is True.
        secret: HMAC-SHA256 signing secret. When empty (the default) the
            request carries no ``X-Briefcase-Signature`` header, and a
            WARNING is logged once: a signature keyed with "" would look
            valid while authenticating nothing.
        subscribed_events: List of event_type strings to forward.
            If None or empty, all events are forwarded.
        timeout: HTTP request timeout in seconds (default 10).
        allow_insecure_http: Permit plain http to a non-loopback host,
            sending the signed payload in cleartext (default False).
        max_attempts: Total attempts for a 5xx response (default 3).
        backoff_base: Delay in seconds before the first retry; doubles per
            attempt with full jitter (default 0.5). 0 retries immediately.
        backoff_max: Upper bound on a single retry delay (default 10).
    """

    def __init__(
        self,
        url: str,
        secret: str = "",
        subscribed_events: Optional[List[str]] = None,
        timeout: float = 10.0,
        allow_insecure_http: bool = False,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 10.0,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "http"
            and not allow_insecure_http
            and not _is_loopback_host(parsed.hostname or "")
        ):
            raise ValueError(
                "Webhook URL uses http with a non-loopback host; the signed "
                "payload would be sent in cleartext. Use https, or pass "
                "allow_insecure_http=True."
            )

        self._url = url
        self._secret = secret
        self._subscribed_events = subscribed_events or []
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = max(0.0, backoff_base)
        self._backoff_max = max(0.0, backoff_max)
        self._warned_unsigned = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_subscribed(self, event: BriefcaseEvent) -> bool:
        if not self._subscribed_events:
            return True
        return event.event_type in self._subscribed_events

    @staticmethod
    def _sign(body: bytes, secret: str) -> str:
        """Return the HMAC-SHA256 hex digest of *body*."""
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def _build_headers(self, event: BriefcaseEvent, body: bytes) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # CloudEvents 1.0
            "ce-id": event.idempotency_key,
            "ce-type": event.event_type,
            "ce-source": "briefcase-ai",
            "ce-specversion": "1.0",
            "ce-time": event.timestamp.isoformat(),
        }
        if self._secret:
            headers["X-Briefcase-Signature"] = self._sign(body, self._secret)
        elif not self._warned_unsigned:
            self._warned_unsigned = True
            logger.warning(
                "WebhookEmitter for %s has no secret; events are sent without "
                "an X-Briefcase-Signature header. Pass secret=... so receivers "
                "can verify them.", self._safe_url(),
            )
        return headers

    def _safe_url(self) -> str:
        """The destination without userinfo or query string, for log lines."""
        parsed = urlsplit(self._url)
        return f"{parsed.scheme}://{parsed.hostname or ''}" + (
            f":{parsed.port}" if parsed.port else ""
        ) + parsed.path

    def _retry_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff: uniform in [0, base * 2**attempt],
        capped at backoff_max."""
        ceiling = min(self._backoff_max, self._backoff_base * (2 ** attempt))
        return random.uniform(0.0, ceiling) if ceiling > 0 else 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def emit(self, event: BriefcaseEvent) -> bool:
        """Send *event* to the webhook endpoint.

        Returns True if the request was acknowledged (2xx), False otherwise.
        Timeouts, network errors, and exhausted retries are logged at
        WARNING and reported as False; this method never raises for them.
        """
        if not self._is_subscribed(event):
            return False

        body = json.dumps(
            {
                "event_type": event.event_type,
                "decision_id": event.decision_id,
                "timestamp": event.timestamp.isoformat(),
                "idempotency_key": event.idempotency_key,
                "payload": event.payload,
            },
            default=str,
        ).encode()

        headers = self._build_headers(event, body)

        for attempt in range(self._max_attempts):
            try:
                status = await asyncio.to_thread(
                    _post, self._url, body, headers, self._timeout
                )
            except Exception as exc:
                logger.warning(
                    "Webhook delivery of %s to %s failed: %s: %s",
                    event.event_type, self._safe_url(), type(exc).__name__, exc,
                )
                return False
            if status < 400:
                return True
            if status < 500:
                logger.warning(
                    "Webhook delivery of %s to %s rejected with HTTP %d",
                    event.event_type, self._safe_url(), status,
                )
                return False
            # 5xx: back off, then retry until attempts are exhausted
            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(self._retry_delay(attempt))
        logger.warning(
            "Webhook delivery of %s to %s failed with HTTP %d after %d attempts",
            event.event_type, self._safe_url(), status, self._max_attempts,
        )
        return False
