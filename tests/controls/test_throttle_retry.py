"""Tests for briefcase.controls.throttle and briefcase.controls.retry."""

import asyncio

import pytest

from briefcase.controls.retry import compute_backoff, retry_call, retry_call_async
from briefcase.controls.throttle import classify_provider_error


#  Fakes mimicking real provider exception shapes (no SDK imports needed)

class RateLimitError(Exception):
    """Same class name openai and anthropic use for 429s."""


class ThrottlingException(Exception):
    pass


class FakeClientError(Exception):
    """botocore ClientError shape: .response with Error.Code and HTTP status."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeHTTPError(Exception):
    """httpx-style error carrying a response with a status_code."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = type("R", (), {"status_code": status_code})()


class TestClassifyProviderError:
    def test_rate_limit_error_class_is_throttled(self):
        c = classify_provider_error(RateLimitError("429"))
        assert c.throttled and c.transient

    def test_throttling_exception_class_is_throttled(self):
        c = classify_provider_error(ThrottlingException("slow down"))
        assert c.throttled and c.transient

    def test_botocore_throttling_code_is_throttled(self):
        c = classify_provider_error(FakeClientError("ThrottlingException"))
        assert c.throttled and c.transient

    def test_botocore_too_many_requests_is_throttled(self):
        c = classify_provider_error(FakeClientError("TooManyRequestsException", 429))
        assert c.throttled and c.transient

    def test_service_quota_exceeded_is_transient_not_throttled(self):
        c = classify_provider_error(FakeClientError("ServiceQuotaExceededException"))
        assert not c.throttled
        assert c.transient

    def test_http_429_is_throttled(self):
        c = classify_provider_error(FakeHTTPError(429))
        assert c.throttled and c.transient

    def test_http_503_is_transient_not_throttled(self):
        c = classify_provider_error(FakeHTTPError(503))
        assert not c.throttled
        assert c.transient

    def test_plain_error_is_neither(self):
        c = classify_provider_error(ValueError("boom"))
        assert not c.throttled and not c.transient

    def test_message_regex_off_by_default(self):
        c = classify_provider_error(RuntimeError("provider rate limit reached"))
        assert not c.throttled

    def test_message_regex_opt_in(self):
        c = classify_provider_error(
            RuntimeError("provider rate limit reached"), message_regex=True
        )
        assert c.throttled and c.transient

    def test_cause_chain_is_traversed(self):
        outer = RuntimeError("wrapped")
        outer.__cause__ = FakeClientError("ThrottlingException")
        c = classify_provider_error(outer)
        assert c.throttled

    def test_context_branch_is_traversed_even_when_cause_is_set(self):
        outer = RuntimeError("wrapped")
        outer.__cause__ = ValueError("benign")
        outer.__context__ = FakeClientError("ThrottlingException")
        c = classify_provider_error(outer)
        assert c.throttled

    def test_cyclic_chain_terminates(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        c = classify_provider_error(a)
        assert not c.throttled and not c.transient


class TestComputeBackoff:
    def test_exponential_growth_with_cap(self):
        no_jitter = {"jitter_s": 0.0}
        assert compute_backoff(0, base_s=0.5, cap_s=30.0, **no_jitter) == 0.5
        assert compute_backoff(1, base_s=0.5, cap_s=30.0, **no_jitter) == 1.0
        assert compute_backoff(2, base_s=0.5, cap_s=30.0, **no_jitter) == 2.0
        assert compute_backoff(10, base_s=0.5, cap_s=30.0, **no_jitter) == 30.0

    def test_jitter_bounded(self):
        for _ in range(50):
            d = compute_backoff(0, base_s=0.5, cap_s=30.0, jitter_s=0.25)
            assert 0.5 <= d <= 0.75


class TestRetryCall:
    def test_returns_on_first_success(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert retry_call(fn, max_attempts=3, sleep=lambda s: None) == "ok"
        assert len(calls) == 1

    def test_retries_transient_then_succeeds(self):
        attempts = []

        def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise FakeHTTPError(503)
            return "ok"

        slept = []
        assert retry_call(fn, max_attempts=3, sleep=slept.append) == "ok"
        assert len(attempts) == 3
        assert len(slept) == 2

    def test_non_transient_raises_immediately(self):
        attempts = []

        def fn():
            attempts.append(1)
            raise ValueError("boom")

        with pytest.raises(ValueError):
            retry_call(fn, max_attempts=5, sleep=lambda s: None)
        assert len(attempts) == 1

    def test_exhausted_attempts_reraise_last_error(self):
        def fn():
            raise FakeHTTPError(503)

        with pytest.raises(FakeHTTPError):
            retry_call(fn, max_attempts=2, sleep=lambda s: None)

    def test_deadline_stops_retrying(self):
        now = [0.0]

        def clock():
            return now[0]

        def sleep(s):
            now[0] += s

        def fn():
            raise FakeHTTPError(503)

        attempts = []

        def counted():
            attempts.append(1)
            fn()

        with pytest.raises(FakeHTTPError):
            retry_call(
                counted,
                max_attempts=100,
                base_s=1.0,
                jitter_s=0.0,
                deadline_s=2.5,
                sleep=sleep,
                clock=clock,
            )
        # Backoffs 1s + 2s pass the 2.5s deadline on the second sleep.
        assert len(attempts) <= 3

    def test_async_variant_retries(self):
        attempts = []

        async def fn():
            attempts.append(1)
            if len(attempts) < 2:
                raise FakeHTTPError(503)
            return "ok"

        async def no_sleep(_s):
            return None

        result = asyncio.run(
            retry_call_async(fn, max_attempts=3, sleep=no_sleep)
        )
        assert result == "ok"
        assert len(attempts) == 2


class TestRetryValidation:
    def test_zero_attempts_raises_value_error(self):
        with pytest.raises(ValueError):
            retry_call(lambda: 1, max_attempts=0)

    def test_zero_attempts_raises_value_error_async(self):
        async def fn():
            return 1

        with pytest.raises(ValueError):
            asyncio.run(retry_call_async(fn, max_attempts=0))
