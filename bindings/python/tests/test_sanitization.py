"""Tests for Python bindings - data sanitization functionality."""

from __future__ import annotations

import pytest
from briefcase.sanitize import Sanitizer


class TestSanitizer:
    def test_sanitizer_creation(self):
        sanitizer = Sanitizer()
        assert sanitizer is not None

    def test_disabled_sanitizer(self):
        sanitizer = Sanitizer.disabled()

        result = sanitizer.sanitize("test@email.com")
        assert result.sanitized == "test@email.com"
        assert len(result.redactions) == 0

    def test_email_sanitization(self):
        sanitizer = Sanitizer()

        text = "Contact me at john.doe@example.com for details."
        result = sanitizer.sanitize(text)

        assert "[REDACTED_EMAIL]" in result.sanitized
        assert len(result.redactions) == 1
        assert result.redactions[0].pii_type == "email"

    def test_multiple_pii_sanitization(self):
        sanitizer = Sanitizer()

        text = "Contact john@example.com at 555-123-4567 or visit 192.168.1.100"
        result = sanitizer.sanitize(text)

        assert "[REDACTED_EMAIL]" in result.sanitized
        assert "[REDACTED_PHONE]" in result.sanitized
        assert "[REDACTED_IP]" in result.sanitized
        assert len(result.redactions) == 3

    def test_custom_pattern_add_and_remove(self):
        sanitizer = Sanitizer()

        sanitizer.add_pattern("employee_id", r"\bEMP-\d{6}\b")
        redacted = sanitizer.sanitize("Employee ID: EMP-123456")
        assert "[REDACTED_EMPLOYEE_ID]" in redacted.sanitized

        removed = sanitizer.remove_pattern("employee_id")
        assert removed is True

        plain = sanitizer.sanitize("Employee ID: EMP-123456")
        assert "[REDACTED_EMPLOYEE_ID]" not in plain.sanitized

    def test_remove_builtin_pattern_returns_false(self):
        sanitizer = Sanitizer()
        removed = sanitizer.remove_pattern("email")
        assert removed is False

    def test_enable_disable(self):
        sanitizer = Sanitizer()
        text = "Email: test@example.com"

        sanitizer.set_enabled(False)
        disabled = sanitizer.sanitize(text)
        assert disabled.sanitized == text

        sanitizer.set_enabled(True)
        enabled = sanitizer.sanitize(text)
        assert "[REDACTED_EMAIL]" in enabled.sanitized

    def test_sanitize_json(self):
        sanitizer = Sanitizer()

        data = {
            "user": {
                "email": "john@example.com",
                "phone": "555-123-4567",
            },
            "config": {
                "api_key": "sk-1234567890abcdef1234567890abcdef",
                "timeout": 30,
            },
        }

        result = sanitizer.sanitize_json(data)

        assert result.sanitized["user"]["email"] == "[REDACTED_EMAIL]"
        assert result.sanitized["user"]["phone"] == "[REDACTED_PHONE]"
        assert result.sanitized["config"]["api_key"] == "[REDACTED_API_KEY]"
        assert result.sanitized["config"]["timeout"] == 30
        assert result.redaction_count == 3

    def test_contains_pii(self):
        sanitizer = Sanitizer()

        assert sanitizer.contains_pii("Email: john@example.com") is True
        assert sanitizer.contains_pii("No pii here") is False

    def test_analyze_pii(self):
        sanitizer = Sanitizer()

        text = "Contact john@example.com or jane@test.org at 555-123-4567"
        analysis = sanitizer.analyze_pii(text)

        assert analysis["has_pii"] is True
        assert analysis["total_matches"] == 3
        assert analysis["unique_types"] == 2
        assert set(analysis["detected_types"]) == {"email", "phone"}

    def test_invalid_pattern(self):
        sanitizer = Sanitizer()

        with pytest.raises(Exception):
            sanitizer.add_pattern("invalid", "[")

    def test_result_to_dict(self):
        sanitizer = Sanitizer()

        result = sanitizer.sanitize("Email: test@example.com")
        obj = result.to_dict()

        assert "sanitized" in obj
        assert "redaction_count" in obj
        assert "has_redactions" in obj
        assert "redactions" in obj
        assert obj["sanitized"] == result.sanitized

    def test_json_result_to_dict(self):
        sanitizer = Sanitizer()

        result = sanitizer.sanitize_json({"email": "test@example.com", "count": 5})
        obj = result.to_dict()

        assert "sanitized" in obj
        assert "redaction_count" in obj

    def test_redaction_to_dict(self):
        sanitizer = Sanitizer()
        result = sanitizer.sanitize("Email: test@example.com")

        redaction = result.redactions[0]
        obj = redaction.to_dict()

        assert obj["pii_type"] == "email"
        assert obj["start_position"] < obj["end_position"]


class TestSanitizationIntegration:
    def test_training_data_redaction(self):
        sanitizer = Sanitizer()

        examples = [
            "Customer john.doe@company.com reported issue with order #12345",
            "Support ticket: Call Sarah at (555) 123-4567 regarding account 123-45-6789",
            "API logs show requests from 192.168.1.100 using key sk-abc123def4567890123456789012",
        ]

        redaction_count = 0
        for example in examples:
            result = sanitizer.sanitize(example)
            redaction_count += len(result.redactions)

        assert redaction_count > 0

    def test_configuration_data_sanitization(self):
        sanitizer = Sanitizer()

        config = {
            "database": {"host": "192.168.1.50", "port": 5432},
            "api": {
                "openai_key": "sk-1234567890abcdefghijklmnop",
                "rate_limit": 1000,
            },
            "notifications": {"admin_email": "admin@company.com"},
        }

        result = sanitizer.sanitize_json(config)

        assert result.sanitized["database"]["host"] == "[REDACTED_IP]"
        assert result.sanitized["api"]["openai_key"] == "[REDACTED_API_KEY]"
        assert result.sanitized["notifications"]["admin_email"] == "[REDACTED_EMAIL]"
        assert result.sanitized["database"]["port"] == 5432
        assert result.sanitized["api"]["rate_limit"] == 1000


if __name__ == "__main__":
    pytest.main([__file__])
