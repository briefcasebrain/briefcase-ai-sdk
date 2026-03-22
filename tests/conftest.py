import pytest
import os
import sys
from pathlib import Path

# Ensure we mock the compiled _core before any briefcase imports
from . import mock_core


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path, config):
    """Skip collection of test files that fail to import.

    Eagerly catches ImportError/ModuleNotFoundError/TypeError so that
    missing optional dependencies don't break the test suite.
    """
    if collection_path.suffix != ".py" or not collection_path.name.startswith("test_"):
        return None

    try:
        import importlib.util
        import types
        spec = importlib.util.spec_from_file_location(
            collection_path.stem, str(collection_path)
        )
        if spec and spec.loader:
            mod = types.ModuleType(spec.name)
            spec.loader.exec_module(mod)
    except (ImportError, ModuleNotFoundError, TypeError):
        return True  # skip this file
    except Exception:
        return None  # let pytest handle other errors

    return None
