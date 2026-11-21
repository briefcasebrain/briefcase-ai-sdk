"""briefcase.auto() with no name: detect what is installed, instrument it,
never raise for what is absent. Also the deferred (lazy) install hook."""

import importlib
import sys
import textwrap

import pytest

import briefcase
import briefcase.auto as auto_mod
from briefcase.auto import _PATCHES, _PENDING, auto, pending, undo

ALL_NAMES = [
    "langchain", "llamaindex", "ag2", "crewai", "openai-agents", "autogen", "pageindex",
    "openai", "anthropic", "google-genai", "bedrock",
]


@pytest.fixture(autouse=True)
def clean():
    undo()
    _PATCHES.clear()
    yield
    undo()
    _PATCHES.clear()
    assert _PENDING == {}
    assert auto_mod._import_hook not in sys.meta_path


@pytest.fixture
def installed(mocker):
    """Control which integrations look installed."""
    names = set()
    mocker.patch.object(auto_mod, "_is_installed", side_effect=lambda name: name in names)
    return names


@pytest.fixture
def fake_handlers(mocker):
    """Replace the real instrumentation with a recorder."""
    calls = {}

    def fake_locked(name, exporter, context_version, async_capture, kwargs):
        if name in _PATCHES:
            return _PATCHES[name]["handler"]
        handler = object()
        calls[name] = dict(kwargs, exporter=exporter, context_version=context_version,
                           async_capture=async_capture)
        _PATCHES[name] = {"handler": handler, "undos": []}
        return handler

    mocker.patch.object(auto_mod, "_auto_locked", side_effect=fake_locked)
    return calls


# -- detection --------------------------------------------------------------

def test_registry_covers_frameworks_and_providers():
    assert sorted(auto_mod._SUPPORTED) == sorted(ALL_NAMES)


def test_nothing_installed_returns_empty_and_does_not_raise(installed):
    assert auto() == {}
    assert _PATCHES == {}


def test_is_installed_uses_find_spec_and_never_raises(mocker):
    for module in ("langchain_core", "llama_index", "llama_index.core", "crewai", "pageindex"):
        mocker.patch.dict("sys.modules")
        sys.modules.pop(module, None)

    def fake_find_spec(name, package=None):
        if name == "langchain_core":
            return object()
        if name == "llama_index.core":
            raise ModuleNotFoundError("No module named 'llama_index'")
        if name == "crewai":
            raise ValueError("crewai.__spec__ is None")
        return None

    find_spec = mocker.patch.object(auto_mod.importlib.util, "find_spec", side_effect=fake_find_spec)
    assert auto_mod._is_installed("langchain") is True
    assert auto_mod._is_installed("llamaindex") is False
    assert auto_mod._is_installed("crewai") is False
    assert auto_mod._is_installed("pageindex") is False
    assert find_spec.call_count == 4


def test_blocked_module_counts_as_absent(mocker):
    mocker.patch.dict("sys.modules", {"pageindex": None})
    assert auto_mod._is_installed("pageindex") is False


def test_generic_import_name_also_needs_its_distribution(mocker):
    """A project's own ``agents`` package is not the OpenAI Agents SDK."""
    mocker.patch.dict("sys.modules", {"agents": object()})
    mocker.patch.object(auto_mod.importlib.metadata, "version",
                        side_effect=importlib.metadata.PackageNotFoundError("openai-agents"))
    assert auto_mod._is_installed("openai-agents") is False
    mocker.patch.object(auto_mod.importlib.metadata, "version", return_value="1.0")
    assert auto_mod._is_installed("openai-agents") is True


def test_detected_integrations_are_instrumented(installed, fake_handlers):
    installed.update({"langchain", "openai", "bedrock"})
    exporter = briefcase.MemoryExporter()

    result = auto(exporter=exporter, context_version="v9", async_capture=False)

    assert set(result) == {"langchain", "openai", "bedrock"}
    for name, handler in result.items():
        assert handler is _PATCHES[name]["handler"]
        assert fake_handlers[name] == {
            "exporter": exporter, "context_version": "v9", "async_capture": False}


def test_second_call_returns_the_same_handlers(installed, fake_handlers):
    installed.update({"langchain", "openai"})
    first = auto()
    second = auto()
    assert first == second
    assert len(fake_handlers) == 2


def test_skip_leaves_named_integrations_alone(installed, fake_handlers):
    installed.update({"langchain", "openai"})
    assert set(auto(skip=["langchain"])) == {"openai"}


def test_one_failing_integration_does_not_stop_the_others(installed, mocker):
    installed.update({"langchain", "openai", "anthropic"})
    warning = mocker.patch.object(auto_mod.logger, "warning")

    def fake_locked(name, exporter, context_version, async_capture, kwargs):
        if name == "openai":
            raise ImportError("openai is half-installed")
        _PATCHES[name] = {"handler": name, "undos": []}
        return name

    mocker.patch.object(auto_mod, "_auto_locked", side_effect=fake_locked)
    assert auto() == {"langchain": "langchain", "anthropic": "anthropic"}
    assert warning.call_count == 1
    assert warning.call_args[0][1] == "openai"


def test_common_options_reach_every_integration(installed, fake_handlers):
    installed.update({"langchain", "openai"})

    def redact(text):
        return text

    auto(capture_content=False, redact=redact)
    # Framework handlers take the mode string; providers accept the bool.
    assert fake_handlers["langchain"]["capture_content"] == "none"
    assert fake_handlers["openai"]["capture_content"] is False
    assert fake_handlers["langchain"]["redact"] is redact
    assert fake_handlers["openai"]["redact"] is redact


def test_handler_specific_options_are_rejected_without_a_name(installed):
    with pytest.raises(TypeError, match="max_records"):
        auto(max_records=5)


# -- named calls keep their behaviour ---------------------------------------

def test_named_provider_that_is_missing_raises_import_error(mocker):
    mocker.patch.dict("sys.modules", {"openai": None})
    with pytest.raises(ImportError, match="openai is not installed"):
        auto("openai")
    assert "openai" not in _PATCHES


def test_unknown_name_still_raises_value_error():
    with pytest.raises(ValueError, match="Unknown framework"):
        auto("nonexistent")


def test_undo_runs_provider_cleanup(mocker):
    cleanup = mocker.Mock()
    instrumentor = mocker.Mock(uninstall=cleanup)
    mocker.patch("briefcase.integrations.providers.instrument", return_value=instrumentor)

    assert auto("openai") is instrumentor
    assert _PATCHES["openai"]["handler"] is instrumentor
    undo()
    cleanup.assert_called_once_with()
    assert _PATCHES == {}


def test_briefcase_auto_is_still_a_callable_module(installed):
    assert briefcase.auto is auto_mod
    assert briefcase.auto() == {}
    assert briefcase.auto.undo is undo
    from briefcase.auto import auto as imported

    assert imported is auto
    with pytest.raises(ValueError):
        briefcase.auto("nonexistent")


# -- deferred install -------------------------------------------------------

@pytest.fixture
def fake_sdk(tmp_path, monkeypatch):
    """An importable package standing in for an SDK, registered as a
    framework named ``fake`` whose import the lazy install waits on."""
    package = tmp_path / "bc_fake_sdk"
    package.mkdir()
    (package / "__init__.py").write_text(textwrap.dedent("""
        from bc_fake_sdk.client import Client
        READY = True
    """))
    (package / "client.py").write_text("class Client:\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(auto_mod._FRAMEWORK_MODULES, "fake", "bc_fake_sdk")
    importlib.invalidate_caches()
    yield "bc_fake_sdk"
    for name in [m for m in sys.modules if m.startswith("bc_fake_sdk")]:
        del sys.modules[name]


def _only_fake(mocker):
    real = auto_mod._is_installed
    mocker.patch.object(
        auto_mod, "_is_installed", side_effect=lambda name: name == "fake" and real(name))


def test_lazy_defers_until_the_package_is_imported(fake_sdk, mocker):
    _only_fake(mocker)
    fired = []
    mocker.patch.object(auto_mod, "_instrument_quietly",
                        side_effect=lambda name, options: fired.append((name, options)))

    result = auto(lazy=True, capture_content="hash", context_version="v1")

    assert result == {"fake": True}
    assert pending() == {"fake": True}
    assert fake_sdk not in sys.modules  # nothing was imported to get here
    assert auto_mod._import_hook in sys.meta_path
    assert fired == []

    module = importlib.import_module(fake_sdk)

    assert module.READY is True and module.Client.__name__ == "Client"
    assert [name for name, _ in fired] == ["fake"]
    assert fired[0][1]["capture_content"] == "hash"
    assert fired[0][1]["context_version"] == "v1"
    assert pending() == {}
    assert auto_mod._import_hook not in sys.meta_path


def test_lazy_instruments_immediately_when_already_imported(fake_sdk, mocker):
    _only_fake(mocker)
    importlib.import_module(fake_sdk)
    fired = []
    mocker.patch.object(auto_mod, "_instrument_quietly",
                        side_effect=lambda name, options: fired.append(name) or "handler")

    assert auto(lazy=True) == {"fake": "handler"}
    assert fired == ["fake"]
    assert pending() == {}


def test_undo_cancels_a_deferred_install(fake_sdk, mocker):
    _only_fake(mocker)
    fired = []
    mocker.patch.object(auto_mod, "_instrument_quietly",
                        side_effect=lambda name, options: fired.append(name))
    auto(lazy=True)
    undo()

    assert pending() == {}
    assert auto_mod._import_hook not in sys.meta_path
    importlib.import_module(fake_sdk)
    assert fired == []


def test_a_failing_deferred_install_does_not_break_the_import(fake_sdk, mocker):
    _only_fake(mocker)
    mocker.patch.object(auto_mod, "_instrument_quietly", side_effect=RuntimeError("boom"))
    auto(lazy=True)
    assert importlib.import_module(fake_sdk).READY is True


def test_real_provider_end_to_end_when_installed():
    """With a provider SDK really installed, auto() returns its instrumentor
    and undo() restores the SDK."""
    pytest.importorskip("openai")
    from openai.resources.chat.completions import Completions
    import inspect

    original = inspect.getattr_static(Completions, "create")
    result = auto(skip=[n for n in ALL_NAMES if n != "openai"])
    assert set(result) == {"openai"}
    assert result["openai"].patched
    assert inspect.getattr_static(Completions, "create") is not original
    undo()
    assert inspect.getattr_static(Completions, "create") is original
