"""The collection policy in tests/conftest.py.

A test module may be skipped at collection only for a missing optional
third-party package from the pyproject extras; every other import failure
must fail collection, and each skip must be reported with its reason.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import conftest as policy

REPO = Path(__file__).resolve().parent.parent


def _missing(name):
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


def _roots_with_absent_package():
    # A root that is certainly not installed, so the tests do not depend on
    # which extras this environment happens to have.
    return {"briefcase_absent_optional_pkg": "someextra", "pytest": "dev"}


class TestAllowList:
    def test_every_extra_distribution_resolves_to_an_import_root(self):
        roots = policy.optional_import_roots()
        for expected in ("grpc", "nacl", "llama_index", "mcp", "pykx", "moto",
                         "google.cloud.logging", "opentelemetry.exporter.otlp.proto.grpc"):
            assert expected in roots, expected

    def test_derived_from_pyproject(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent('''
            [project.optional-dependencies]
            empty = []
            # a comment naming "not-a-package"
            one = ["some-dist>=1,<2"]
            many = [
                "other_dist[extra]>=2",
                "pynacl>=1.5",
            ]
            dev = ["pytest", "moto[kms]>=5"]
            all = ["briefcase-ai[one,many]"]

            [project.scripts]
            x = "also.not:a_package"
        '''), encoding="utf-8")
        roots = policy.optional_import_roots(pyproject)
        assert roots["some_dist"] == "one"
        assert roots["other_dist"] == "many"
        assert roots["nacl"] == "many"
        assert roots["moto"] == "dev"
        # Test tooling and the package itself are never a reason to skip.
        assert "pytest" not in roots
        assert "briefcase_ai" not in roots and "not_a_package" not in roots

    def test_test_tooling_is_not_skippable(self):
        roots = policy.optional_import_roots()
        for tool in ("pytest", "pytest_mock", "pytest_asyncio", "mypy", "flake8"):
            assert tool not in roots


class TestClassification:
    def test_missing_optional_package_is_skippable(self):
        roots = _roots_with_absent_package()
        assert policy.missing_optional_dependency(
            _missing("briefcase_absent_optional_pkg"), roots
        ) == ("briefcase_absent_optional_pkg", "someextra")

    def test_missing_submodule_of_absent_package_is_skippable(self):
        roots = _roots_with_absent_package()
        found = policy.missing_optional_dependency(
            _missing("briefcase_absent_optional_pkg.sub"), roots
        )
        assert found == ("briefcase_absent_optional_pkg.sub", "someextra")

    def test_missing_namespace_parent_is_skippable(self):
        roots = {"briefcase_absent_ns.cloud.logging": "gcp"}
        found = policy.missing_optional_dependency(_missing("briefcase_absent_ns.cloud"), roots)
        assert found == ("briefcase_absent_ns.cloud", "gcp")

    def test_friendly_reraise_of_a_missing_optional_package_is_skippable(self):
        roots = _roots_with_absent_package()
        try:
            try:
                raise _missing("briefcase_absent_optional_pkg")
            except ImportError as inner:
                raise ImportError("install briefcase-ai[someextra]") from inner
        except ImportError as exc:
            assert policy.missing_optional_dependency(exc, roots) is not None

    def test_unlisted_missing_module_fails(self):
        assert policy.missing_optional_dependency(_missing("not_in_any_extra")) is None

    def test_missing_briefcase_submodule_fails(self):
        assert policy.missing_optional_dependency(_missing("briefcase.nope")) is None

    def test_plain_import_error_from_briefcase_fails(self):
        exc = ImportError("cannot import name 'gone' from 'briefcase.thing'", name="briefcase.thing")
        assert policy.missing_optional_dependency(exc) is None

    def test_installed_but_incompatible_package_fails(self):
        # `pytest` stands in for an allow-listed package that IS installed but
        # lost a submodule (mcp 2.x dropping mcp.server.fastmcp).
        roots = _roots_with_absent_package()
        assert policy.missing_optional_dependency(_missing("pytest.removed_api"), roots) is None

    def test_non_import_errors_fail(self):
        assert policy.missing_optional_dependency(TypeError("unsupported operand")) is None
        assert policy.missing_optional_dependency(None) is None


def _absent_optional_root():
    for root in policy.optional_import_roots():
        if "." not in root and not policy._is_installed(root):
            return root
    return None


def test_collection_end_to_end(tmp_path):
    """Run pytest on a scratch package that reuses the real hooks."""
    absent = _absent_optional_root()
    if absent is None:
        pytest.skip("every optional package is installed; nothing to skip on")

    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import (  # noqa: F401\n"
        "    pytest_pycollect_makemodule, pytest_terminal_summary)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_optional.py").write_text(
        f"import {absent}\n\ndef test_x():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    broken = {
        "test_broken_internal.py": "from briefcase import this_name_does_not_exist\n",
        "test_broken_submodule.py": "import briefcase.this_module_does_not_exist\n",
        "test_broken_unlisted.py": "import a_package_in_no_extra\n",
        "test_broken_typeerror.py": "x: int = None + 1\n",
    }
    for name, body in broken.items():
        (tmp_path / name).write_text(body + "\ndef test_x():\n    pass\n", encoding="utf-8")

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(REPO)] + [p for p in [os.environ.get("PYTHONPATH")] if p]
    ))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), "-c", os.devnull, str(tmp_path)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    # The missing optional package is skipped, and says so without -rs.
    assert "test modules skipped: optional dependency missing" in out, out
    assert f"SKIPPED test_optional.py: optional dependency '{absent}' is not installed" in out, out
    assert "pip install 'briefcase-ai[" in out
    # Everything else is a collection error, not a skip.
    for name in broken:
        assert f"ERROR {name}" in out, out
    assert "4 errors" in out, out
