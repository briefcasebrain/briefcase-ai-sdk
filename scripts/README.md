# Version Management Scripts

Automated tools for managing version numbers across all Briefcase AI packages.

## Scripts

### `bump-version.py` (Recommended)
Cross-platform Python script for version management.

**Usage:**
```bash
# Quick release (update, commit, tag, and push)
./scripts/bump-version.py 3.1.0 --push

# Step by step
./scripts/bump-version.py 3.1.0           # Update versions only
./scripts/bump-version.py 3.1.0 --commit  # Update and commit
./scripts/bump-version.py 3.1.0 --tag     # Update, commit, and tag
./scripts/bump-version.py 3.1.0 --push    # Update, commit, tag, and push
```

### `bump-version.sh`
Bash wrapper that delegates to the Python script.

## What Gets Updated

Both scripts automatically update version numbers in:

1. **`Cargo.toml`** — Rust workspace version
2. **`pyproject.toml`** — Python package version
3. **`briefcase/__init__.py`** — Python runtime version
4. **`bindings/python/src/lib.rs`** — Native module version

Version targets are defined in `scripts/version_targets.toml`.

## CI/CD Integration

When you push a version tag (e.g., `v3.1.0`), the GitHub Actions publish workflow automatically:

1. **Publishes** `briefcase-core` to crates.io
2. **Builds** Python wheels for Linux, macOS, and Windows
3. **Publishes** wheels to PyPI
4. **Creates** a GitHub Release with artifacts

**Monitor the workflow:**
https://github.com/briefcasebrain/briefcase-ai-sdk/actions

## Support

For issues with version management:
1. Check this README
2. Run script with `--help` flag
3. Open an issue: https://github.com/briefcasebrain/briefcase-ai-sdk/issues
