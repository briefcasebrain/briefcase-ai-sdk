import pytest

import briefcase
from briefcase.auto import _PATCHES, undo
from briefcase.integrations import providers

try:
    import jsonschema  # noqa: F401
    import referencing  # noqa: F401
    _MISSING = None
except ImportError as exc:  # pragma: no cover - depends on the environment
    _MISSING = f"the contract suite needs jsonschema>=4.18 (pip install jsonschema): {exc}"


@pytest.fixture
def mem():
    return briefcase.MemoryExporter()


@pytest.fixture
def instrument(mem):
    """Instrument a provider with content capture on (replay needs the
    request), exporting inline into ``mem``; undone afterwards."""
    installed = []

    def _instrument(name, **options):
        options.setdefault("exporter", mem)
        options.setdefault("async_capture", False)
        options.setdefault("capture_content", True)
        inst = providers.instrument(name, **options)
        installed.append(inst)
        return inst

    yield _instrument
    for inst in installed:
        inst.uninstall()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / "home"))
    undo()
    _PATCHES.clear()
    yield
    undo()
    _PATCHES.clear()


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/contracts/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.filterwarnings("ignore:briefcase.auto"))
            if _MISSING:
                item.add_marker(pytest.mark.skip(reason=_MISSING))
