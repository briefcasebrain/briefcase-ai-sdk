import pytest

import briefcase
from briefcase.auto import _PATCHES, undo
from briefcase.integrations import providers


@pytest.fixture
def mem():
    return briefcase.MemoryExporter()


@pytest.fixture
def instrument(mem):
    """Instrument a provider (content on) for the test, undo afterwards."""
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
