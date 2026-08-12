"""
Example: Basic lakeFS versioning with Briefcase SDK.

This example demonstrates three patterns for capturing lakeFS commit SHAs:
1. Context manager (recommended)
2. Decorator
3. Direct client usage

It runs offline with mock=True. Against a real lakeFS server, drop mock=True
and configure the endpoint and credentials (explicit parameters or the
LAKEFS_ENDPOINT / LAKEFS_ACCESS_KEY / LAKEFS_PRIVATE_KEY environment
variables); construction raises if either is missing.
"""

from briefcase.integrations.lakefs import (
    versioned_context,
    versioned,
    VersionedClient
)


# Stand-in for an authenticated Briefcase client; unused in mock mode
class MockBriefcaseClient:
    pass


client = MockBriefcaseClient()


# Pattern 1: Context Manager (Recommended)
print("Pattern 1: Context Manager")
print("-" * 50)

with versioned_context(client, "acme-workspace", "main", mock=True) as lakefs:
    # All file access within this context is automatically tracked
    policy_doc = lakefs.read_object("policies/aetna_medical_necessity_2025_Q1.pdf")
    guidelines = lakefs.read_object("guidelines/cms_coverage_v2.json")

    print(f"Read policy document: {len(policy_doc)} bytes")
    print(f"Read guidelines: {len(guidelines)} bytes")
    print(f"Commit SHA: {lakefs.get_commit()}")

print()


# Pattern 2: Decorator
print("Pattern 2: Decorator")
print("-" * 50)


@versioned(repository="acme-workspace", branch="main", mock=True)
def evaluate_prior_auth(claim_data: dict, versioned_client=None) -> dict:
    """
    Evaluate prior authorization using versioned policy documents.

    All policy accesses are automatically tracked with commit SHAs.
    """
    # These reads are automatically tracked with commit SHAs
    policy = versioned_client.read_object("policies/medicare_part_d.pdf")
    guidelines = versioned_client.read_object("guidelines/cms_ncd_list.json")

    # Simulated decision logic
    decision = {
        "status": "APPROVED",
        "reason": f"Policy and guidelines validated ({len(policy)} + {len(guidelines)} bytes processed)",
        "commit_sha": versioned_client.get_commit()
    }

    return decision


# Call the decorated function
claim = {"id": "CLM_123", "procedure": "72148"}
result = evaluate_prior_auth(claim, briefcase_client=client)
print(f"Decision: {result['status']}")
print(f"Reason: {result['reason']}")
print(f"Commit SHA: {result['commit_sha']}")

print()


# Pattern 3: Direct Client
print("Pattern 3: Direct Client")
print("-" * 50)

versioned_client = VersionedClient(
    repository="acme-workspace",
    branch="main",
    briefcase_client=client,
    mock=True
)

# Read multiple files
files = ["policies/policy1.pdf", "policies/policy2.pdf"]
for file_path in files:
    if versioned_client.object_exists(file_path):
        content = versioned_client.read_object(file_path)
        print(f"Read {file_path}: {len(content)} bytes")

# List objects
objects = versioned_client.list_objects(prefix="policies/")
print(f"Found {len(objects)} objects in policies/")
print(f"Commit SHA: {versioned_client.get_commit()}")

print()
print("=" * 50)
print("All patterns complete!")
print("=" * 50)
print()
print("Note: This example uses mock=True. In production:")
print("  1. Install lakefs SDK: pip install lakefs")
print("  2. Set LAKEFS_ENDPOINT, LAKEFS_ACCESS_KEY, LAKEFS_PRIVATE_KEY")
print("  3. Drop mock=True; all reads then carry real commit SHAs")
