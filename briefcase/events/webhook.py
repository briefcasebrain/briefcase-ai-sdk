"""Webhook transport for Briefcase events.

POSTs :class:`briefcase.events.types.BriefcaseEvent` records as JSON to a
configured URL with:

- HMAC-SHA256 signature in the ``X-Briefcase-Signature`` header
- CloudEvents 1.0 headers on every request
- per-event-type subscription filtering
- up to 3 attempts on 5xx responses
- configurable timeout (default 10 s)

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
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from briefcase.events.types import BriefcaseEvent


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
        secret: HMAC-SHA256 signing secret.
        subscribed_events: List of event_type strings to forward.
            If None or empty, all events are forwarded.
        timeout: HTTP request timeout in seconds (default 10).
        allow_insecure_http: Permit plain http to a non-loopback host,
            sending the signed payload in cleartext (default False).
    """

    def __init__(
        self,
        url: str,
        secret: str = "",
        subscribed_events: Optional[List[str]] = None,
        timeout: float = 10.0,
        allow_insecure_http: bool = False,
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
        return {
            "Content-Type": "application/json",
            "X-Briefcase-Signature": self._sign(body, self._secret),
            # CloudEvents 1.0
            "ce-id": event.idempotency_key,
            "ce-type": event.event_type,
            "ce-source": "briefcase-ai",
            "ce-specversion": "1.0",
            "ce-time": event.timestamp.isoformat(),
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def emit(self, event: BriefcaseEvent) -> bool:
        """Send *event* to the webhook endpoint.

        Returns True if the request was acknowledged (2xx), False otherwise.
        Timeouts, network errors, and exhausted retries are swallowed and
        reported as False.
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
        max_attempts = 3

        for _attempt in range(max_attempts):
            try:
                status = await asyncio.to_thread(
                    _post, self._url, body, headers, self._timeout
                )
            except Exception:
                return False
            if status < 500:
                return status < 400
            # 5xx: retry until attempts are exhausted
        return False
