"""
Demonstration of different validation modes.

Validation modes control how the engine handles errors and warnings:
- strict: Fails on any errors, warns on warnings
- tolerant: Only fails on critical errors
- warn_only: Never fails, only records issues
"""

from briefcase.validation import PromptValidationEngine
from briefcase.integrations.lakefs import VersionedClient


def demonstrate_mode(lakefs, mode: str, prompt: str):
    """Demonstrate a validation mode."""
    print(f"\n{'='*60}")
    print(f"MODE: {mode.upper()}")
    print(f"{'='*60}")

    validator = PromptValidationEngine(
        lakefs_client=lakefs,
        repository="kb",
        mode=mode
    )

    report = validator.validate(prompt)

    print(f"Status: {report.status}")
    print(f"Errors: {len(report.errors)}")
    print(f"Warnings: {len(report.warnings)}")

    if mode == "strict":
        print("\nStrict mode: Fails on errors, warns on warnings")
    elif mode == "tolerant":
        print("\nTolerant mode: Only critical errors cause failure")
    elif mode == "warn_only":
        print("\nWarn-only mode: Never fails, just records issues")


def main():
    # Initialize lakeFS client
    lakefs = VersionedClient(
        repository="kb",
        branch="main",
        lakefs_endpoint="https://briefcaseai.us-east-1.lakefscloud.io/api/v1",
        lakefs_access_key="key",
        lakefs_secret_key="secret"
    )

    # Prompt with a missing reference
    prompt_with_error = "Follow policies/missing_file.pdf for guidelines"

    # Demonstrate each mode
    demonstrate_mode(lakefs, "strict", prompt_with_error)
    demonstrate_mode(lakefs, "tolerant", prompt_with_error)
    demonstrate_mode(lakefs, "warn_only", prompt_with_error)

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    print("- Use 'strict' for production (compliance-focused workloads)")
    print("- Use 'tolerant' for development (iterate faster)")
    print("- Use 'warn_only' for testing (monitor issues without blocking)")


if __name__ == "__main__":
    main()
