"""The usage guide served by the MCP how_to tool must ship inside the package.

briefcase.mcp itself needs the optional mcp dependency, so the loader module
is loaded by path here and these tests run without the extra.
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE_MODULE = REPO / "briefcase" / "mcp" / "_guide.py"


def _load_guide_module():
    spec = importlib.util.spec_from_file_location("_briefcase_mcp_guide", GUIDE_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_guide_matches_the_repo_root_copy():
    packaged = REPO / "briefcase" / "mcp" / "llms-full.txt"
    assert packaged.read_bytes() == (REPO / "llms-full.txt").read_bytes(), (
        "briefcase/mcp/llms-full.txt is the copy shipped in the wheel; "
        "re-copy it after editing llms-full.txt"
    )


def test_packaged_guide_is_listed_for_the_wheel():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    include = pyproject.split("[tool.maturin]")[1]
    assert '"briefcase/mcp/llms-full.txt"' in include


def test_guide_loads_from_the_package_directory():
    guide = _load_guide_module()
    text = guide.llms_full_text()
    assert "briefcase-ai" in text and "## 1." in text
    assert guide._packaged_guide().parent == GUIDE_MODULE.parent


def test_guide_falls_back_when_no_file_is_readable(monkeypatch, tmp_path):
    guide = _load_guide_module()
    # Simulate an install where neither copy exists.
    monkeypatch.setattr(guide, "__file__", str(tmp_path / "a" / "b" / "c" / "_guide.py"))
    text = guide.llms_full_text()
    assert text.startswith("briefcase usage guide: https://")


def test_installed_layout_without_repo_root_still_finds_the_guide(tmp_path):
    """Copy only the package files, as a wheel would lay them out."""
    pkg = tmp_path / "site-packages" / "briefcase" / "mcp"
    pkg.mkdir(parents=True)
    for name in ("_guide.py", "llms-full.txt"):
        (pkg / name).write_bytes((REPO / "briefcase" / "mcp" / name).read_bytes())
    spec = importlib.util.spec_from_file_location("_installed_guide", pkg / "_guide.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "## 1." in module.llms_full_text()
