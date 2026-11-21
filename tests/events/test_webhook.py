"""Tests for briefcase/events/webhook.py (WebhookEmitter).

The HTTP transport is a module-level ``_post`` function; tests replace it
with a fake, so no network access happens. The https posture tests
exercise the constructor directly.
"""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

import briefcase.events.webhook as webhook_mod
from briefcase.events.types import BriefcaseEvent
from briefcase.events.webhook import WebhookEmitter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_type: str = "decision.low_confidence") -> BriefcaseEvent:
    return BriefcaseEvent(
        event_type=event_type,
        decision_id="dec-001",
        timestamp=datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc),
        payload={"confidence": 0.4, "threshold": 0.85},
        idempotency_key="idem-abc-123",
    )


def _run(coro):
    return asyncio.run(coro)


class _FakePost:
    """Replacement for webhook._post that records calls and replays outcomes.

    Each outcome is an int status code or an exception instance to raise.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append(
            {"url": url, "body": body, "headers": dict(headers), "timeout": timeout}
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _install_fake_post(monkeypatch, outcomes):
    fake = _FakePost(outcomes)
    monkeypatch.setattr(webhook_mod, "_post", fake)
    return fake


def _expected_body(event) -> bytes:
    return json.dumps(
        {
            "event_type": event.event_type,
            "decision_id": event.decision_id,
            "timestamp": event.timestamp.isoformat(),
            "idempotency_key": event.idempotency_key,
            "payload": event.payload,
        },
        default=str,
    ).encode()


# ---------------------------------------------------------------------------
# Delivery tests
# ---------------------------------------------------------------------------

def test_webhook_posts_event(monkeypatch):
    """emit() POSTs to the configured URL and returns True on 200."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [200])

    emitter = WebhookEmitter(url="https://example.com/hook", secret="s3cr3t")
    result = _run(emitter.emit(event))

    assert result is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "https://example.com/hook"


def test_webhook_hmac_signature(monkeypatch):
    """X-Briefcase-Signature is the HMAC-SHA256 of the request body."""
    event = _make_event()
    secret = "my-secret"
    fake = _install_fake_post(monkeypatch, [200])

    emitter = WebhookEmitter(url="https://example.com/hook", secret=secret)
    _run(emitter.emit(event))

    body = _expected_body(event)
    expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert fake.calls[0]["headers"]["X-Briefcase-Signature"] == expected_sig


def test_webhook_cloudevents_headers(monkeypatch):
    """Every request includes all required CloudEvents 1.0 headers."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [200])

    emitter = WebhookEmitter(url="https://example.com/hook")
    _run(emitter.emit(event))

    headers = fake.calls[0]["headers"]
    assert headers["ce-id"] == event.idempotency_key
    assert headers["ce-type"] == event.event_type
    assert headers["ce-source"] == "briefcase-ai"
    assert headers["ce-specversion"] == "1.0"
    assert headers["ce-time"] == event.timestamp.isoformat()


def test_webhook_filters_events(monkeypatch):
    """An unsubscribed event type does not trigger a POST."""
    event = _make_event(event_type="drift.detected")
    fake = _install_fake_post(monkeypatch, [])

    emitter = WebhookEmitter(
        url="https://example.com/hook",
        subscribed_events=["decision.low_confidence"],
    )
    result = _run(emitter.emit(event))

    assert result is False
    assert fake.calls == []


def test_webhook_accepts_matching_event(monkeypatch):
    """A subscribed event type triggers the POST."""
    event = _make_event(event_type="decision.low_confidence")
    fake = _install_fake_post(monkeypatch, [200])

    emitter = WebhookEmitter(
        url="https://example.com/hook",
        subscribed_events=["decision.low_confidence"],
    )
    result = _run(emitter.emit(event))

    assert result is True
    assert len(fake.calls) == 1


def test_webhook_retry_on_5xx(monkeypatch):
    """A 503 followed by 200 results in a successful emit after retry."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [503, 200])

    emitter = WebhookEmitter(url="https://example.com/hook", backoff_base=0)
    result = _run(emitter.emit(event))

    assert result is True
    assert len(fake.calls) == 2


def test_webhook_timeout(monkeypatch):
    """A timeout is handled gracefully and reported as False."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [TimeoutError("timed out")])

    emitter = WebhookEmitter(url="https://example.com/hook", timeout=0.001)
    result = _run(emitter.emit(event))

    assert result is False
    assert len(fake.calls) == 1


def test_webhook_max_retries(monkeypatch):
    """After 3 consecutive 5xx responses the emitter gives up."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [503, 503, 503])

    emitter = WebhookEmitter(url="https://example.com/hook", backoff_base=0)
    result = _run(emitter.emit(event))

    assert result is False
    assert len(fake.calls) == 3


def test_webhook_payload_matches_schema(monkeypatch):
    """The JSON body contains all BriefcaseEvent fields."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [200])

    emitter = WebhookEmitter(url="https://example.com/hook")
    _run(emitter.emit(event))

    body = json.loads(fake.calls[0]["body"])
    assert body["event_type"] == event.event_type
    assert body["decision_id"] == event.decision_id
    assert body["idempotency_key"] == event.idempotency_key
    assert "timestamp" in body
    assert "payload" in body


def test_webhook_general_exception_silent(monkeypatch):
    """A non-timeout transport exception is swallowed and returns False."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [ConnectionError("network failure")])

    emitter = WebhookEmitter(url="https://example.com/hook")
    result = _run(emitter.emit(event))

    assert result is False
    assert len(fake.calls) == 1


def test_webhook_4xx_returns_false(monkeypatch):
    """A 4xx response is not acknowledged and is not retried."""
    event = _make_event()
    fake = _install_fake_post(monkeypatch, [404])

    emitter = WebhookEmitter(url="https://example.com/hook")
    result = _run(emitter.emit(event))

    assert result is False
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# HTTPS posture tests
# ---------------------------------------------------------------------------

def test_webhook_rejects_plain_http_remote():
    """A plain-http non-loopback URL is rejected at construction."""
    with pytest.raises(ValueError, match="https"):
        WebhookEmitter(url="http://example.com/hook")


def test_webhook_allows_loopback_http(monkeypatch):
    """Plain http to loopback hosts is accepted and delivered."""
    for url in (
        "http://localhost:8080/hook",
        "http://127.0.0.1:9/hook",
        "http://[::1]:8/hook",
    ):
        fake = _install_fake_post(monkeypatch, [200])
        emitter = WebhookEmitter(url=url)
        assert _run(emitter.emit(_make_event())) is True
        assert fake.calls[0]["url"] == url


def test_webhook_insecure_opt_in(monkeypatch):
    """allow_insecure_http=True permits plain http to a remote host."""
    fake = _install_fake_post(monkeypatch, [200])
    emitter = WebhookEmitter(url="http://example.com/hook", allow_insecure_http=True)
    assert _run(emitter.emit(_make_event())) is True
    assert len(fake.calls) == 1


def test_webhook_https_always_allowed():
    """An https URL constructs without any opt-in."""
    WebhookEmitter(url="https://example.com/hook")


# ---------------------------------------------------------------------------
# Backoff, failure logging, and unsigned delivery
# ---------------------------------------------------------------------------

def test_webhook_backs_off_between_5xx_attempts(monkeypatch):
    """Retries wait with jittered exponential backoff instead of firing at once."""
    fake = _install_fake_post(monkeypatch, [503, 503, 200])
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(webhook_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(webhook_mod.random, "uniform", lambda low, high: high)

    emitter = WebhookEmitter(url="https://example.com/hook", secret="s",
                             backoff_base=0.5, backoff_max=10)
    assert _run(emitter.emit(_make_event())) is True
    assert len(fake.calls) == 3
    assert delays == [0.5, 1.0]


def test_webhook_backoff_is_jittered_and_capped():
    emitter = WebhookEmitter(url="https://example.com/hook", backoff_base=1.0, backoff_max=3.0)
    samples = [emitter._retry_delay(4) for _ in range(50)]
    assert all(0.0 <= s <= 3.0 for s in samples)
    assert len(set(samples)) > 1


def test_webhook_max_attempts_is_configurable(monkeypatch):
    fake = _install_fake_post(monkeypatch, [503] * 5)
    emitter = WebhookEmitter(url="https://example.com/hook", max_attempts=5, backoff_base=0)
    assert _run(emitter.emit(_make_event())) is False
    assert len(fake.calls) == 5


@pytest.mark.parametrize("outcomes, fragment", [
    ([503, 503, 503], "HTTP 503 after 3 attempts"),
    ([404], "rejected with HTTP 404"),
    ([ConnectionError("network failure")], "ConnectionError: network failure"),
])
def test_webhook_failures_are_logged_at_warning(monkeypatch, caplog, outcomes, fragment):
    import logging

    _install_fake_post(monkeypatch, outcomes)
    emitter = WebhookEmitter(url="https://user:pw@example.com/hook?key=abc",
                             secret="s", backoff_base=0)
    with caplog.at_level(logging.WARNING, logger="briefcase.events.webhook"):
        assert _run(emitter.emit(_make_event())) is False
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(fragment in m for m in messages)
    # Credentials and query strings never reach the log line.
    assert not any("pw@" in m or "key=abc" in m for m in messages)


def test_webhook_without_secret_omits_signature_and_warns_once(monkeypatch, caplog):
    import logging

    fake = _install_fake_post(monkeypatch, [200, 200])
    emitter = WebhookEmitter(url="https://example.com/hook")
    with caplog.at_level(logging.WARNING, logger="briefcase.events.webhook"):
        assert _run(emitter.emit(_make_event())) is True
        assert _run(emitter.emit(_make_event())) is True
    for call in fake.calls:
        assert "X-Briefcase-Signature" not in call["headers"]
        assert call["headers"]["ce-specversion"] == "1.0"
    warnings = [r for r in caplog.records if "no secret" in r.getMessage()]
    assert len(warnings) == 1
