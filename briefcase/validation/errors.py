"""
Validation error taxonomy and error classes.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, List


class ValidationErrorCode(Enum):
    """HTTP-style error codes for validation failures."""

    # 4xx: Client errors (prompt issues)
    REFERENCE_NOT_FOUND = 404
    REFERENCE_GONE = 410  # Deprecated/removed
    VERSION_MISMATCH = 409
    INVALID_SYNTAX = 400
    REFERENCE_AMBIGUOUS = 300  # Multiple matches

    # 5xx: Server errors (knowledge base issues)
    LAKEFS_UNAVAILABLE = 503
    SCHEMA_INVALID = 500


@dataclass
class ValidationError:
    """Represents a validation failure."""

    code: ValidationErrorCode
    message: str
    reference: str  # The problematic reference
    severity: str  # "error" | "warning"
    layer: str  # "syntax" | "resolution" | "semantic"
    remediation: Optional[str] = None  # Suggested fix
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            'code': self.code.value,
            'message': self.message,
            'reference': self.reference,
            'severity': self.severity,
            'layer': self.layer,
            'remediation': self.remediation,
            'metadata': self.metadata or {}
        }


@dataclass
class ValidationReport:
    """Complete validation report."""

    status: str  # "passed" | "failed" | "warning"
    errors: List[ValidationError]
    warnings: List[ValidationError]
    references_checked: int
    validation_time_ms: float
    lakefs_commit: str

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def to_dict(self) -> dict:
        return {
            'status': self.status,
            'errors': [e.to_dict() for e in self.errors],
            'warnings': [w.to_dict() for w in self.warnings],
            'references_checked': self.references_checked,
            'validation_time_ms': self.validation_time_ms,
            'lakefs_commit': self.lakefs_commit
        }
