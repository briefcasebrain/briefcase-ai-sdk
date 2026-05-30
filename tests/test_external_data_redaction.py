"""Security regression tests: external-data snapshots must not persist raw PII
when a sanitizer is configured on the tracker.

These tests use a lightweight fake sanitizer so they validate the *tracker's*
redaction wiring deterministically, independent of the native Sanitizer (whose
redaction quality is covered by bindings/python/tests/test_sanitization.py).

See SECURITY.md and ExternalDataTracker(sanitizer=...).
"""

import json
import re

from briefcase.external_data.tracker import (
    ExternalDataTracker,
    SnapshotPolicy,
    SnapshotFrequency,
)


class _FakeLakeFS:
    """Minimal lakeFS double that records uploaded object bodies."""

    def __init__(self):
        self.objects = {}

    def upload_object(self, repository, branch, path, body):
        self.objects[path] = body


class _Result:
    def __init__(self, sanitized):
        self.sanitized = sanitized


class _FakeSanitizer:
    """Redacts email/SSN via regex — mirrors the native Sanitizer's contract
    (`.sanitize(text).sanitized`) without requiring the compiled extension."""

    _EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    _SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

    def sanitize(self, text):
        redacted = self._EMAIL.sub("[REDACTED_EMAIL]", text)
        redacted = self._SSN.sub("[REDACTED_SSN]", redacted)
        return _Result(redacted)


_PII_PAYLOAD = {
    "customer_email": "john.doe@example.com",
    "ssn": "123-45-6789",
    "amount": 100,
}


def _tracker(sanitizer=None):
    lakefs = _FakeLakeFS()
    tracker = ExternalDataTracker(
        lakefs_client=lakefs,
        repository="repo",
        default_policy=SnapshotPolicy(frequency=SnapshotFrequency.EVERY_CALL),
        sanitizer=sanitizer,
    )
    return tracker, lakefs


def _only_body(lakefs):
    assert len(lakefs.objects) == 1
    return next(iter(lakefs.objects.values()))


def test_without_sanitizer_raw_pii_is_persisted():
    """Documents the default (un-sanitized) behavior so the redaction path is
    a clear, deliberate opt-in."""
    tracker, lakefs = _tracker(sanitizer=None)
    tracker.track_api_call("ofac", "https://x", "GET", _PII_PAYLOAD)
    body = _only_body(lakefs)
    assert "john.doe@example.com" in body
    assert "123-45-6789" in body


def test_with_sanitizer_pii_is_redacted_before_persist():
    tracker, lakefs = _tracker(sanitizer=_FakeSanitizer())
    tracker.track_api_call("ofac", "https://x", "GET", _PII_PAYLOAD)
    body = _only_body(lakefs)
    assert "john.doe@example.com" not in body
    assert "123-45-6789" not in body
    assert "[REDACTED_EMAIL]" in body
    assert "100" in body  # non-PII content survives
    snap = tracker.get_latest_snapshot("ofac")
    assert snap.metadata.get("sanitized") is True


def test_redaction_preserves_data_hash_for_drift():
    """data_hash is computed over the original payload, so drift detection is
    identical whether or not a sanitizer is attached."""
    t_plain, _ = _tracker(sanitizer=None)
    t_redact, _ = _tracker(sanitizer=_FakeSanitizer())
    h_plain = t_plain.track_api_call("ofac", "https://x", "GET", _PII_PAYLOAD)["data_hash"]
    h_redact = t_redact.track_api_call("ofac", "https://x", "GET", _PII_PAYLOAD)["data_hash"]
    assert h_plain == h_redact


def test_db_query_snapshot_is_also_redacted():
    tracker, lakefs = _tracker(sanitizer=_FakeSanitizer())
    tracker.track_db_query(
        db_system="postgres",
        db_name="customers",
        query="SELECT * FROM customers",
        result_data=_PII_PAYLOAD,
        result_count=1,
        store_snapshot=True,
    )
    body = _only_body(lakefs)
    assert "john.doe@example.com" not in body
    assert "123-45-6789" not in body


def test_source_name_cannot_traverse_storage_key():
    """A crafted source_name must not escape the snapshots/ prefix in the
    lakeFS object key."""
    tracker, lakefs = _tracker(sanitizer=None)
    tracker.track_api_call("../../etc/passwd", "https://x", "GET", {"k": "v"})
    (path,) = lakefs.objects.keys()
    assert path.startswith("snapshots/")
    assert ".." not in path
    assert "/etc/" not in path


def test_sanitizer_failure_fails_closed():
    """If the sanitizer raises, the raw payload must NOT be persisted."""

    class _ExplodingSanitizer:
        def sanitize(self, text):
            raise RuntimeError("boom")

    tracker, lakefs = _tracker(sanitizer=_ExplodingSanitizer())
    tracker.track_api_call("ofac", "https://x", "GET", _PII_PAYLOAD)
    body = _only_body(lakefs)
    # Fails closed: stores metadata only, never the raw PII payload.
    assert "john.doe@example.com" not in body
    assert "123-45-6789" not in body
    parsed = json.loads(body)
    assert parsed.get("sanitized") is True
