"""briefcase.auto() with no name: detect what is installed, instrument it,
never raise for what is absent. Also the deferred (lazy) install hook."""

import importlib
import sys
import textwrap

import pytest

import briefcase
import briefcase.auto as auto_mod
from briefcase.auto import _PATCHES, _PENDING, auto, pending, undo

# auto() also raises its "not recorded" / "full content" notices through
# ``warnings`` so they are seen without logging configured; the tests assert
# on the log records.
pytestmark = pytest.mark.filterwarnings("ignore:briefcase.auto")

ALL_NAMES = [
    "langchain", "llamaindex", "ag2", "crewai", "openai-agents", "autogen", "pageindex",
    "openai", "anthropic", "google-genai", "bedrock", "aiobotocore",
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


# -- red team: the import hook --------------------------------------------------

@pytest.fixture
def fake_install(mocker):
    """Make ``fake`` instrumentable: records each install and registers a
    handler like a real integration does."""
    installs = []

    def install(name, exporter, context_version, async_capture, kwargs):
        handler = object()
        installs.append((name, dict(kwargs, exporter=exporter, context_version=context_version,
                                    async_capture=async_capture)))
        _PATCHES[name] = {"handler": handler, "undos": []}
        return handler

    mocker.patch.object(auto_mod, "_install", side_effect=install)
    mocker.patch.object(auto_mod, "_SUPPORTED", auto_mod._SUPPORTED | {"fake"})
    return installs


def test_the_hook_leaves_every_other_import_and_finder_alone(fake_sdk, mocker, tmp_path):
    _only_fake(mocker)
    (tmp_path / "bc_unrelated.py").write_text("VALUE = 1\n")
    asked = []

    class Spy:
        def find_spec(self, fullname, path=None, target=None):
            asked.append((fullname, path, target))
            return None

    spy = Spy()
    sys.meta_path.insert(0, spy)
    try:
        auto(lazy=True)
        hook = auto_mod._import_hook
        assert sys.meta_path[0] is hook
        assert hook.find_spec("bc_unrelated") is None and hook.find_spec("os.path") is None
        import bc_unrelated

        assert type(bc_unrelated.__loader__).__name__ == "SourceFileLoader"
        module = importlib.import_module(fake_sdk)
        # the other finder was consulted for the watched module, with the
        # arguments the import system passed
        assert (fake_sdk, None, None) in asked
        # and the module does not keep our loader wrapper
        assert type(module.__loader__).__name__ == "SourceFileLoader"
        assert module.__spec__.loader is module.__loader__
    finally:
        sys.meta_path.remove(spy)
        sys.modules.pop("bc_unrelated", None)


def test_reload_and_resource_access_work_after_a_deferred_install(fake_sdk, mocker, fake_install):
    _only_fake(mocker)
    auto(lazy=True)
    module = importlib.import_module(fake_sdk)
    assert [name for name, _ in fake_install] == ["fake"]
    reloaded = importlib.reload(module)
    assert reloaded is module and module.READY is True
    assert module.__loader__.get_data(module.__file__).startswith(b"\nfrom bc_fake_sdk")
    assert [name for name, _ in fake_install] == ["fake"]  # not installed twice


def test_a_module_that_fails_to_import_fails_as_usual_and_can_be_retried(
        tmp_path, monkeypatch, mocker, fake_install):
    package = tmp_path / "bc_broken_sdk"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('sdk import failed')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(auto_mod._FRAMEWORK_MODULES, "fake", "bc_broken_sdk")
    importlib.invalidate_caches()
    _only_fake(mocker)
    auto(lazy=True)
    with pytest.raises(RuntimeError, match="sdk import failed"):
        importlib.import_module("bc_broken_sdk")
    assert "bc_broken_sdk" not in sys.modules and fake_install == []
    assert pending() == {"fake": True}  # still waiting for a successful import
    (package / "__init__.py").write_text("READY = True\n")
    importlib.invalidate_caches()
    assert importlib.import_module("bc_broken_sdk").READY is True
    assert [name for name, _ in fake_install] == ["fake"]
    sys.modules.pop("bc_broken_sdk", None)


def test_circular_imports_inside_the_package_still_resolve(tmp_path, monkeypatch, mocker,
                                                           fake_install):
    package = tmp_path / "bc_cyclic_sdk"
    package.mkdir()
    (package / "__init__.py").write_text("from bc_cyclic_sdk import a\nREADY = a.VALUE\n")
    (package / "a.py").write_text("import bc_cyclic_sdk\nfrom bc_cyclic_sdk import b\nVALUE = b.VALUE\n")
    (package / "b.py").write_text("import bc_cyclic_sdk.a\nVALUE = 7\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(auto_mod._FRAMEWORK_MODULES, "fake", "bc_cyclic_sdk")
    importlib.invalidate_caches()
    _only_fake(mocker)
    seen_ready = []
    fake_install_effect = auto_mod._install.side_effect

    def install(name, *args):
        # runs when the package finished executing: it is fully initialised
        seen_ready.append(sys.modules["bc_cyclic_sdk"].READY)
        return fake_install_effect(name, *args)

    auto_mod._install.side_effect = install
    auto(lazy=True)
    assert importlib.import_module("bc_cyclic_sdk").READY == 7
    assert seen_ready == [7]
    for name in [m for m in sys.modules if m.startswith("bc_cyclic_sdk")]:
        del sys.modules[name]


def test_an_import_while_another_thread_holds_the_auto_lock_does_not_deadlock(
        fake_sdk, mocker, fake_install):
    """Thread A imports the SDK (holding its import lock) and the hook fires;
    thread B is inside auto() (holding the auto lock) and imports the same
    SDK. Waiting for each other's lock would hang both forever."""
    import threading

    _only_fake(mocker)
    auto(lazy=True)
    imported = threading.Event()
    errors = []

    def importer():
        try:
            importlib.import_module(fake_sdk)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        imported.set()

    with auto_mod._LOCK:  # "thread B is inside auto()"
        thread = threading.Thread(target=importer)
        thread.start()
        assert imported.wait(10), "the import blocked on the auto lock"
        assert fake_install == []  # deferred to the lock holder ...
        auto_mod._drain_ready()    # ... which runs it before releasing
    thread.join(10)
    assert not errors and [name for name, _ in fake_install] == ["fake"]
    assert pending() == {}


def test_lazy_auto_imports_no_sdk(tmp_path):
    """auto(lazy=True) is what `briefcase exec` runs at interpreter start."""
    import subprocess

    code = textwrap.dedent("""
        import sys
        import briefcase
        briefcase.observe("memory")
        result = briefcase.auto(lazy=True)
        heavy = [m for m in ("openai", "anthropic", "google.genai", "botocore", "boto3",
                             "httpx", "pydantic") if m in sys.modules]
        print(sorted(result), heavy)
        briefcase.auto.undo()
        assert not any(type(f).__name__ == "_DeferredInstallFinder" for f in sys.meta_path)
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    names, heavy = done.stdout.strip().rsplit(" [", 1)
    assert heavy == "]", f"SDKs imported by auto(lazy=True): [{heavy}"


# -- red team: options, status, undo ---------------------------------------------

def test_an_explicit_call_with_new_options_re_instruments(installed, mocker, fake_install, caplog):
    import logging

    installed.add("fake")
    first = auto("fake", capture_content="none")
    assert auto("fake") is first                          # no options: same handler
    assert auto("fake", capture_content="none") is first  # same options: same handler
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        second = auto("fake", capture_content="full", context_version="v2")
    assert second is not first
    assert "different options (capture_content, context_version)" in caplog.text
    assert fake_install[-1][1]["capture_content"] == "full"
    assert fake_install[-1][1]["context_version"] == "v2"
    assert len(fake_install) == 2


def test_auto_all_applies_changed_common_options_too(installed, fake_install):
    installed.add("fake")
    mocker_names = dict(auto_mod._FRAMEWORK_MODULES, fake="bc_fake_sdk")
    auto_mod._FRAMEWORK_MODULES.update(mocker_names)
    try:
        first = auto(capture_content="none")["fake"]
        assert auto(capture_content="none")["fake"] is first
        assert auto()["fake"] is first
        second = auto(capture_content="hash")["fake"]
        assert second is not first and fake_install[-1][1]["capture_content"] == "hash"
    finally:
        auto_mod._FRAMEWORK_MODULES.pop("fake", None)


def test_an_explicit_call_builds_on_a_deferred_installs_options(fake_sdk, mocker, fake_install):
    """`briefcase exec` defers with capture_content="none"; the program then
    calls auto("x", option=...). The environment's choice must survive."""
    _only_fake(mocker)
    auto(lazy=True, capture_content="none", context_version="from-exec")
    auto("fake", max_records=5)
    assert pending() == {} and auto_mod._import_hook not in sys.meta_path
    options = fake_install[-1][1]
    assert options["capture_content"] == "none" and options["context_version"] == "from-exec"
    assert options["max_records"] == 5
    importlib.import_module(fake_sdk)
    assert len(fake_install) == 1  # the import does not install it a second time


def test_status_reports_every_state(fake_sdk, mocker, fake_install):
    _only_fake(mocker)
    assert auto_mod.status()["integrations"]["openai"]["state"] == "not_installed"
    auto(lazy=True)
    report = auto_mod.status()
    assert report["lazy_import_hook"] is True
    mocker.patch.object(auto_mod, "_install", side_effect=RuntimeError("layout changed"))
    importlib.import_module(fake_sdk)
    report = auto_mod.status()
    assert report["lazy_import_hook"] is False and set(report) == {
        "dedupe", "lazy_import_hook", "integrations"}
    assert all(entry["kind"] in ("framework", "provider")
               for entry in report["integrations"].values())
    assert "fake: failed - RuntimeError: layout changed" in "\n".join(auto_mod.format_status())


def test_a_failed_install_is_reported_by_status(installed, mocker):
    installed.add("openai")
    mocker.patch.object(auto_mod, "_install", side_effect=RuntimeError("layout changed"))
    assert auto(skip=[n for n in ALL_NAMES if n != "openai"]) == {}
    entry = auto_mod.status()["integrations"]["openai"]
    assert entry == {"kind": "provider", "state": "failed",
                     "reason": "RuntimeError: layout changed"}
    assert "openai: failed - RuntimeError: layout changed" in "\n".join(auto_mod.format_status())


def test_unknown_skip_names_are_reported(installed, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="briefcase"):
        auto(skip=["langchian", "openai"])
    assert "langchian" in caplog.text and "not integrations" in caplog.text


def test_framework_full_content_default_warns_once_and_env_turns_it_off(
        installed, fake_install, mocker, monkeypatch, caplog):
    import logging

    mocker.patch.object(auto_mod, "_warned_framework_content", False)
    monkeypatch.delenv("BRIEFCASE_CAPTURE_CONTENT", raising=False)
    monkeypatch.setitem(auto_mod._FRAMEWORK_MODULES, "fake", "bc_fake_sdk")
    monkeypatch.setitem(auto_mod._FRAMEWORK_MODULES, "fake2", "bc_fake_sdk")
    mocker.patch.object(auto_mod, "_SUPPORTED", auto_mod._SUPPORTED | {"fake", "fake2"})
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        auto("fake")
        auto("fake2")
    warnings = [r.getMessage() for r in caplog.records if "FULL prompt" in r.getMessage()]
    assert len(warnings) == 1
    assert "capture_content='none'" in warnings[0] and "BRIEFCASE_CAPTURE_CONTENT=none" in warnings[0]
    assert "capture_content" not in fake_install[0][1]  # the default itself is unchanged

    undo()
    _PATCHES.clear()
    caplog.clear()
    mocker.patch.object(auto_mod, "_warned_framework_content", False)
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        auto("fake", capture_content="full")  # stated explicitly: no warning
        monkeypatch.setenv("BRIEFCASE_CAPTURE_CONTENT", "none")
        auto("fake2")                          # the environment decides
    assert not [r for r in caplog.records if "FULL prompt" in r.getMessage()]
    assert fake_install[-1][1]["capture_content"] == "none"


def test_providers_are_not_given_the_framework_warning(installed, fake_install, mocker, caplog):
    import logging

    mocker.patch.object(auto_mod, "_warned_framework_content", False)
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        auto("openai")
    assert "FULL prompt" not in caplog.text


def test_undo_leaves_a_later_third_party_patch_in_place(mocker, caplog):
    import logging
    import types

    target = types.SimpleNamespace(__name__="framework_module")

    def original():
        return "original"

    def ours():
        return original()

    switch = {"active": True}
    target.fn = ours
    _PATCHES["langchain"] = {"handler": object(), "switch": switch,
                             "undos": [(target, "fn", original, ours)]}

    def theirs():
        return ours()

    target.fn = theirs
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        undo("langchain")
    assert target.fn is theirs and switch["active"] is False
    assert "patched again by another library" in caplog.text
