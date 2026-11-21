"""Packaging-level behaviour: importing without the compiled core, the
replay import hint, and docs that must agree with pyproject.toml."""

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run_without_native(body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a fresh interpreter where briefcase._native cannot be
    imported (whether or not the extension happens to be built)."""
    script = "import sys\nsys.modules['briefcase._native'] = None\n" + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO),
        capture_output=True, text=True, timeout=120,
    )


def test_pure_python_surface_works_without_the_native_core():
    result = _run_without_native("""
        import briefcase
        from briefcase.controls import Gateway  # noqa: F401
        from briefcase.events import BriefcaseEvent  # noqa: F401
        from briefcase.routing.internal import InternalRouter  # noqa: F401

        mem = briefcase.observe("memory")

        @briefcase.capture(async_capture=False)
        def classify(text):
            return text.upper()

        assert classify("hi") == "HI"
        assert mem.records[0]["function_name"] == "classify"
        print("ok")
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_native_names_raise_a_clear_import_error_only_when_used():
    result = _run_without_native("""
        import briefcase
        for access in (lambda: briefcase.init, lambda: briefcase.DecisionSnapshot):
            try:
                access()
            except ImportError as exc:
                assert "compiled briefcase core" in str(exc), exc
                assert "maturin develop" in str(exc), exc
            else:
                raise SystemExit("expected ImportError")
        try:
            from briefcase import Snapshot  # noqa: F401
        except ImportError as exc:
            assert "compiled briefcase core" in str(exc), exc
        else:
            raise SystemExit("expected ImportError")
        try:
            briefcase.definitely_not_a_thing
        except AttributeError:
            pass
        print("ok")
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_replay_import_error_does_not_point_at_an_empty_extra():
    result = _run_without_native("""
        try:
            import briefcase.replay  # noqa: F401
        except ImportError as exc:
            print(exc)
        else:
            raise SystemExit("expected ImportError")
    """)
    assert result.returncode == 0, result.stderr
    assert "briefcase-ai[replay]" not in result.stdout
    assert "'replay' extra" not in result.stdout
    assert "maturin develop" in result.stdout


def test_native_names_resolve_normally_when_the_core_is_present():
    import briefcase

    assert callable(briefcase.init)
    assert briefcase._native_import_error is None


# --- docs must agree with pyproject.toml about extras ----------------------

def _extras():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]


def _listed(text: str, start: str, end: str) -> set:
    """Backticked names in the sentence that starts with ``start``."""
    flat = " ".join(text.split())
    begin = flat.index(start)
    return set(re.findall(r"`([a-z0-9-]+)`", flat[begin:flat.index(end, begin)]))


@pytest.mark.parametrize("doc, empty_start, deps_start, deps_end", [
    ("AGENTS.md", "empty groupings that install nothing:",
     "These install third-party packages:", "(`all`"),
    ("README.md", "pull in **no** additional dependencies:",
     "The extras that install third-party packages are", ". `all`"),
    ("llms.txt", "empty groupings that install nothing:",
     "Extras that install third-party packages:", ". `all`"),
])
def test_docs_list_exactly_the_extras_that_pull_dependencies(
    doc, empty_start, deps_start, deps_end
):
    extras = _extras()
    with_deps = {name for name, deps in extras.items() if deps} - {"all", "dev"}
    empty = {name for name, deps in extras.items() if not deps}
    text = (REPO / doc).read_text(encoding="utf-8")
    assert _listed(text, deps_start, deps_end) == with_deps
    assert _listed(text, empty_start, ". ") == empty


def test_llms_full_extras_map_covers_every_extra_with_dependencies():
    extras = _extras()
    with_deps = {name for name, deps in extras.items() if deps} - {"dev"}
    section = (REPO / "llms-full.txt").read_text(encoding="utf-8").split("## 8. Extras map")[1]
    section = section.split("## 8b.")[0]
    mentioned = set(re.findall(r"\[([a-z0-9-]+)\]", section)) | set(
        re.findall(r"`([a-z0-9-]+)`", section)
    )
    assert with_deps <= mentioned, sorted(with_deps - mentioned)
    for name in with_deps - {"all"}:
        assert f"`{name}`" not in section.split("Needs an extra")[0]


def test_readme_extras_table_has_a_row_for_every_extra():
    extras = _extras()
    table = (REPO / "README.md").read_text(encoding="utf-8").split("## Extras")[1]
    table = table.split("\n\n", 2)[1]
    rows = set(re.findall(r"`([a-z0-9-]+)`", " ".join(
        line.split("|")[1] for line in table.splitlines() if line.startswith("| `")
    )))
    assert rows == set(extras)
