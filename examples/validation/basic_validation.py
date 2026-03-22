"""
Basic prompt validation example.

This example demonstrates how to use the Briefcase prompt validation engine
to validate prompts against a lakeFS knowledge base before execution.
"""

from briefcase.validation import PromptValidationEngine
from briefcase.integrations.lakefs import VersionedClient


def main():
    # Initialize lakeFS client
    lakefs = VersionedClient(
        repository="knowledge-base",
        branch="main",
        lakefs_endpoint="https://briefcaseai.us-east-1.lakefscloud.io/api/v1",
        lakefs_access_key="your-access-key",
        lakefs_secret_key="your-secret-key"
    )

    # Initialize validation engine
    validator = PromptValidationEngine(
        lakefs_client=lakefs,
        repository="knowledge-base",
        branch="main",
        mode="strict"  # Fail on errors
    )

    # Example prompt with knowledge base references
    prompt = """
    Follow the Medicare Coverage Policy 2024 Q4 and reference Section 4.2.3
    for claim evaluation guidelines. Use policies/medicare_2024.pdf for
    the specific coverage details.
    """

    # Validate the prompt
    report = validator.validate(prompt)

    # Check results
    print(f"Validation Status: {report.status}")
    print(f"References Checked: {report.references_checked}")
    print(f"Validation Time: {report.validation_time_ms:.2f}ms")
    print(f"LakeFS Commit: {report.lakefs_commit[:8]}")

    if report.has_errors:
        print(f"\n {len(report.errors)} Errors Found:")
        for error in report.errors:
            print(f"  - {error.message}")
            print(f"    Reference: {error.reference}")
            print(f"    Fix: {error.remediation}")

    if report.has_warnings:
        print(f"\n  {len(report.warnings)} Warnings:")
        for warning in report.warnings:
            print(f"  - {warning.message}")

    if report.status == "passed":
        print("\n Validation passed! Safe to execute prompt.")
        # Proceed with LLM call
    else:
        print("\n Validation failed! Fix errors before proceeding.")


if __name__ == "__main__":
    main()
