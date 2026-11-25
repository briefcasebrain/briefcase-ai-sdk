import pytest

import briefcase
from briefcase.auto import _PATCHES, undo
from briefcase.integrations import providers


@pytest.fixture
def mem():
    return briefcase.MemoryExporter()


@pytest.fixture
def instrument(mem):
    """Instrument a provider for the test and restore it afterwards."""
    installed = []

    def _instrument(name, **options):
        options.setdefault("exporter", mem)
        options.setdefault("async_capture", False)
        inst = providers.instrument(name, **options)
        installed.append(inst)
        return inst

    yield _instrument
    for inst in installed:
        inst.uninstall()


@pytest.fixture(autouse=True)
def _clean_auto_registry():
    undo()
    _PATCHES.clear()
    yield
    undo()
    _PATCHES.clear()


def pytest_collection_modifyitems(items):
    # The capture-gap warning is also raised through ``warnings`` so it is
    # visible in applications that have not configured logging; the tests
    # that provoke it on purpose assert on the log record instead.
    for item in items:
        if "tests/providers/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.filterwarnings("ignore:briefcase.auto"))
