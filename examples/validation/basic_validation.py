"""
Basic prompt validation example.

Demonstrates the Briefcase prompt validation engine. The engine is pluggable:
you supply an *extractor* (finds references in a prompt), a *resolver* (checks
each reference against a knowledge base), and a versioned client (records the
commit the validation ran against).

This example ships tiny in-memory implementations so it runs offline. In
production you would pass a real ``briefcase.integrations.lakefs.VersionedClient``
and resolve references against a live lakeFS knowledge base.
"""

import re

from briefcase.validation import PromptValidationEngine
from briefcase.validation.errors import ValidationError, ValidationErrorCode


# --- Pluggable pieces (replace with real implementations in production) ------

class RegexExtractor:
    """Extracts ``path/like.pdf`` and ``Section X.Y`` references from a prompt."""

    _REF = re.compile(r"[\w/]+\.pdf|Section\s+[\d.]+")

    def extract(self, prompt: str) -> list:
        return self._REF.findall(prompt)


class KnowledgeBaseResolver:
    """Resolves references against an in-memory allowlist."""

    def __init__(self, known_references: set):
        self._known = known_references

    def resolve_all(self, references: list) -> list:
        errors = []
        for ref in references:
            if ref not in self._known:
                errors.append(
                    ValidationError(
                        code=ValidationErrorCode.REFERENCE_NOT_FOUND,
                        message=f"Reference not found in knowledge base: {ref}",
                        reference=ref,
                        severity="error",
                        layer="resolution",
                        remediation="Add the document to the knowledge base or fix the reference.",
                    )
                )
        return errors


class DemoLakeFS:
    """Stand-in for VersionedClient.get_commit() so the example runs offline."""

    def get_commit(self, repository: str, branch: str) -> str:
        return "demo0000000000000000000000000000000000000"


def main():
    extractor = RegexExtractor()
    resolver = KnowledgeBaseResolver(
        known_references={"policies/medicare_2024.pdf", "Section 4.2.3"}
    )

    validator = PromptValidationEngine(
        extractor=extractor,
        resolver=resolver,
        lakefs_client=DemoLakeFS(),
        repository="knowledge-base",
        branch="main",
        mode="strict",  # fail on errors
    )

    prompt = """
    Follow the Medicare Coverage Policy 2024 Q4 and reference Section 4.2.3
    for claim evaluation guidelines. Use policies/medicare_2024.pdf for
    the specific coverage details, and policies/missing_file.pdf as well.
    """

    report = validator.validate(prompt)

    print(f"Validation Status: {report.status}")
    print(f"References Checked: {report.references_checked}")
    print(f"Validation Time: {report.validation_time_ms:.2f}ms")
    print(f"LakeFS Commit: {report.lakefs_commit[:8]}")

    if report.has_errors:
        print(f"\n{len(report.errors)} Errors Found:")
        for error in report.errors:
            print(f"  - {error.message}")
            print(f"    Reference: {error.reference}")
            print(f"    Fix: {error.remediation}")

    if report.has_warnings:
        print(f"\n{len(report.warnings)} Warnings:")
        for warning in report.warnings:
            print(f"  - {warning.message}")

    if report.status == "passed":
        print("\nValidation passed! Safe to execute prompt.")
    else:
        print("\nValidation failed! Fix errors before proceeding.")


if __name__ == "__main__":
    main()
