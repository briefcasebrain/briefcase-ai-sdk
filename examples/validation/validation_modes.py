"""
Demonstration of different validation modes.

Validation modes control how the engine handles errors and warnings:
- strict:    Fails on any errors, warns on warnings
- tolerant:  Only fails on errors (warnings never fail)
- warn_only: Never fails, only records issues

Uses tiny in-memory extractor/resolver/client implementations so it runs
offline; swap in a real VersionedClient and resolver for production.
"""

import re

from briefcase.validation import PromptValidationEngine
from briefcase.validation.errors import ValidationError, ValidationErrorCode


class RegexExtractor:
    _REF = re.compile(r"[\w/]+\.pdf|Section\s+[\d.]+")

    def extract(self, prompt: str) -> list:
        return self._REF.findall(prompt)


class KnowledgeBaseResolver:
    """Flags every unknown reference as an error of the configured severity."""

    def __init__(self, known_references: set, severity: str = "error"):
        self._known = known_references
        self._severity = severity

    def resolve_all(self, references: list) -> list:
        return [
            ValidationError(
                code=ValidationErrorCode.REFERENCE_NOT_FOUND,
                message=f"Reference not found: {ref}",
                reference=ref,
                severity=self._severity,
                layer="resolution",
                remediation="Add the document or fix the reference.",
            )
            for ref in references
            if ref not in self._known
        ]


class DemoLakeFS:
    def get_commit(self, repository: str, branch: str) -> str:
        return "demo0000"


def demonstrate_mode(mode: str, prompt: str):
    print(f"\n{'=' * 60}")
    print(f"MODE: {mode.upper()}")
    print(f"{'=' * 60}")

    validator = PromptValidationEngine(
        extractor=RegexExtractor(),
        resolver=KnowledgeBaseResolver(known_references=set()),
        lakefs_client=DemoLakeFS(),
        repository="kb",
        mode=mode,
    )

    report = validator.validate(prompt)

    print(f"Status: {report.status}")
    print(f"Errors: {len(report.errors)}")
    print(f"Warnings: {len(report.warnings)}")

    if mode == "strict":
        print("\nStrict mode: Fails on errors, warns on warnings")
    elif mode == "tolerant":
        print("\nTolerant mode: Only errors cause failure")
    elif mode == "warn_only":
        print("\nWarn-only mode: Never fails, just records issues")


def main():
    prompt_with_error = "Follow policies/missing_file.pdf for guidelines"

    for mode in ("strict", "tolerant", "warn_only"):
        demonstrate_mode(mode, prompt_with_error)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print("- Use 'strict' for production (compliance-focused workloads)")
    print("- Use 'tolerant' for development (iterate faster)")
    print("- Use 'warn_only' for testing (monitor issues without blocking)")


if __name__ == "__main__":
    main()
