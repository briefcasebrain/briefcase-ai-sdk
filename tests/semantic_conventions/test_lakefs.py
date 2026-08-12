"""
Tests for lakeFS semantic conventions.
"""


def test_lakefs_conventions():
    """Ensure all convention constants are defined and unique."""
    from briefcase.semantic_conventions import lakefs

    # Check all required attributes exist
    assert hasattr(lakefs, 'LAKEFS_COMMIT_SHA')
    assert hasattr(lakefs, 'LAKEFS_FILE_PATH')
    assert hasattr(lakefs, 'LAKEFS_REPOSITORY')
    assert hasattr(lakefs, 'LAKEFS_ARTIFACT_PREFIX')

    # Check no duplicates
    attrs = [v for k, v in vars(lakefs).items() if k.startswith('LAKEFS_')]
    assert len(attrs) == len(set(attrs)), "Duplicate attribute names found"


def test_lakefs_attribute_format():
    """Verify attribute names follow semantic conventions."""
    from briefcase.semantic_conventions import lakefs

    # All attributes should be lowercase with dots
    attrs = [v for k, v in vars(lakefs).items() if k.startswith('LAKEFS_')]
    for attr in attrs:
        assert attr.startswith('lakefs.'), f"Attribute {attr} should start with 'lakefs.'"
        assert attr.islower() or '_' in attr, f"Attribute {attr} should be lowercase"
