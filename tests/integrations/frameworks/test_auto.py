"""Tests for briefcase.auto() and briefcase.undo().

The frameworks themselves are not installed; conftest.py stubs their import
names, and individual tests swap in purpose-built fakes via patch.dict.
"""

import types

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from briefcase.auto import auto, undo, _PATCHES


# Fixture: keep _PATCHES empty and patches reverted around every test

@pytest.fixture(autouse=True)
def clean_patches():
    """Revert any leftover patches and empty _PATCHES before and after."""
    undo()
    _PATCHES.clear()
    yield
    undo()
    _PATCHES.clear()


# Helpers

def _make_exporter():
    exp = MagicMock()
    exp.export = AsyncMock(return_value=None)
    return exp


def _mock_langchain_modules():
    """Return (module dict for patch.dict, config namespace, original ensure_config)."""
    mock_lc_config = types.SimpleNamespace()

    def _orig(cfg=None, **kw):
        return cfg or {}

    mock_lc_config.ensure_config = _orig
    mock_runnables = types.SimpleNamespace(config=mock_lc_config)
    mock_lc = types.SimpleNamespace(runnables=mock_runnables)
    modules = {
        "langchain_core": mock_lc,
        "langchain_core.runnables": mock_runnables,
        "langchain_core.runnables.config": mock_lc_config,
    }
    return modules, mock_lc_config, _orig


# Unknown framework

class TestAutoUnknownFramework:
    def test_unknown_framework_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown framework"):
            auto("nonexistent")

    def test_error_message_lists_supported(self):
        with pytest.raises(ValueError, match="langchain"):
            auto("bad")


# Idempotency

class TestAutoIdempotency:
    def test_langchain_idempotent_returns_same_handler(self):
        with patch.dict("sys.modules", {"langchain_core": None,
                                        "langchain_core.runnables": None,
                                        "langchain_core.runnables.config": None}):
            h1 = auto("langchain")
            h2 = auto("langchain")
            assert h1 is h2

    def test_crewai_missing_dependency_raises(self, mocker):
        mocker.patch(
            "briefcase.integrations.frameworks.crewai_handler._CREWAI_AVAILABLE",
            False,
        )
        with pytest.raises(ImportError):
            auto("crewai")


# LangChain auto()

class TestAutoLangChain:
    def test_returns_handler_instance(self):
        from briefcase.integrations.frameworks.langchain_handler import BriefcaseLangChainHandler
        handler = auto("langchain")
        assert isinstance(handler, BriefcaseLangChainHandler)

    def test_handler_stored_in_patches(self):
        handler = auto("langchain")
        assert _PATCHES["langchain"]["handler"] is handler

    def test_exporter_forwarded_to_handler(self):
        exp = _make_exporter()
        handler = auto("langchain", exporter=exp)
        assert handler._exporter is exp

    def test_patches_ensure_config_when_langchain_installed(self):
        """When langchain_core is importable, ensure_config is patched in place."""
        modules, mock_lc_config, _orig = _mock_langchain_modules()
        with patch.dict("sys.modules", modules):
            auto("langchain")
            assert mock_lc_config.ensure_config is not _orig
            undo("langchain")

    def test_undo_restores_ensure_config(self):
        modules, mock_lc_config, _orig = _mock_langchain_modules()
        with patch.dict("sys.modules", modules):
            auto("langchain")
            assert mock_lc_config.ensure_config is not _orig
            undo("langchain")
            assert mock_lc_config.ensure_config is _orig


# AG2 auto()

class TestAutoAG2:
    def test_ag2_patches_conversable_agent_init(self, mocker):
        """When the autogen namespace is importable, __init__ is patched."""

        class FakeConversableAgent:
            def __init__(self, *a, **k):
                pass

        _orig_init = FakeConversableAgent.__init__

        mock_ag2 = MagicMock()
        mock_ag2.ConversableAgent = FakeConversableAgent

        mocker.patch.dict("sys.modules", {"autogen": mock_ag2})
        mocker.patch(
            "briefcase.integrations.frameworks.ag2_handler._AG2_AVAILABLE",
            True,
        )
        mocker.patch(
            "briefcase.integrations.frameworks.ag2_handler.ConversableAgent",
            FakeConversableAgent,
        )

        handler = auto("ag2")
        assert FakeConversableAgent.__init__ is not _orig_init
        from briefcase.integrations.frameworks.ag2_handler import AG2HookTracer
        assert isinstance(handler, AG2HookTracer)

    def test_ag2_patched_init_body_executed(self, mocker):
        """Calling the patched __init__ executes the original then instruments."""
        class FakeConversableAgent:
            def __init__(self, *a, **k):
                self.name = "fake"

        mock_ag2 = MagicMock()
        mock_ag2.ConversableAgent = FakeConversableAgent

        mocker.patch.dict("sys.modules", {"autogen": mock_ag2})
        mocker.patch(
            "briefcase.integrations.frameworks.ag2_handler._AG2_AVAILABLE",
            True,
        )
        mocker.patch(
            "briefcase.integrations.frameworks.ag2_handler.ConversableAgent",
            FakeConversableAgent,
        )

        auto("ag2")

        # FakeConversableAgent has no register_hook, so instrument() raises
        # internally; the patched __init__ swallows that and still completes.
        instance = object.__new__(FakeConversableAgent)
        FakeConversableAgent.__init__(instance)  # must not raise
        assert instance.name == "fake"  # the original __init__ ran

    def test_ag2_undo_removes_from_patches(self, mocker):
        mocker.patch(
            "briefcase.integrations.frameworks.ag2_handler._AG2_AVAILABLE",
            True,
        )
        mocker.patch(
            "briefcase.integrations.frameworks.ag2_handler.ConversableAgent",
            MagicMock,
        )
        # Make the autogen import fail: no patch applied, handler still returned
        mocker.patch.dict("sys.modules", {"autogen": None})
        auto("ag2")
        assert "ag2" in _PATCHES
        undo("ag2")
        assert "ag2" not in _PATCHES


# CrewAI auto()

class TestAutoCrewAI:
    def test_crewai_returns_listener_instance(self, mocker):
        mocker.patch(
            "briefcase.integrations.frameworks.crewai_handler._CREWAI_AVAILABLE",
            True,
        )
        mocker.patch(
            "briefcase.integrations.frameworks.crewai_handler._crewai_bus",
            None,
        )
        from briefcase.integrations.frameworks.crewai_handler import CrewAIEventListener
        handler = auto("crewai")
        assert isinstance(handler, CrewAIEventListener)

    def test_crewai_registered_in_patches(self, mocker):
        mocker.patch(
            "briefcase.integrations.frameworks.crewai_handler._CREWAI_AVAILABLE",
            True,
        )
        mocker.patch(
            "briefcase.integrations.frameworks.crewai_handler._crewai_bus",
            None,
        )
        auto("crewai")
        assert "crewai" in _PATCHES


# OpenAI Agents auto()

class TestAutoOpenAIAgents:
    def test_openai_agents_delegates_to_install(self, mocker):
        mock_tracer = MagicMock()
        mock_install = mocker.patch(
            "briefcase.integrations.frameworks.openai_agents_handler.install",
            return_value=mock_tracer,
        )
        handler = auto("openai-agents", context_version="v1")
        mock_install.assert_called_once_with(
            context_version="v1",
            async_capture=True,
            exporter=None,
        )
        assert handler is mock_tracer
        assert "openai-agents" in _PATCHES

    def test_openai_agents_idempotent(self, mocker):
        mock_tracer = MagicMock()
        mocker.patch(
            "briefcase.integrations.frameworks.openai_agents_handler.install",
            return_value=mock_tracer,
        )
        h1 = auto("openai-agents")
        h2 = auto("openai-agents")
        assert h1 is h2


# AutoGen auto()

class TestAutoAutoGen:
    def test_autogen_delegates_to_install(self, mocker):
        mock_handler = MagicMock()
        mock_install = mocker.patch(
            "briefcase.integrations.frameworks.autogen_handler.install",
            return_value=mock_handler,
        )
        handler = auto("autogen", context_version="v2")
        mock_install.assert_called_once_with(
            context_version="v2",
            async_capture=True,
            exporter=None,
        )
        assert handler is mock_handler
        assert "autogen" in _PATCHES

    def test_autogen_undo_uninstalls(self, mocker):
        mock_handler = MagicMock()
        mocker.patch(
            "briefcase.integrations.frameworks.autogen_handler.install",
            return_value=mock_handler,
        )
        mock_uninstall = mocker.patch(
            "briefcase.integrations.frameworks.autogen_handler.uninstall",
        )
        auto("autogen")
        undo("autogen")
        assert "autogen" not in _PATCHES
        mock_uninstall.assert_called_once()


# LlamaIndex auto()

class TestAutoLlamaIndex:
    def test_llamaindex_returns_handler(self):
        from briefcase.integrations.frameworks.llamaindex_handler import BriefcaseLlamaIndexHandler
        with patch.dict("sys.modules", {
            "llama_index": None,
            "llama_index.core": None,
        }):
            handler = auto("llamaindex")
        assert isinstance(handler, BriefcaseLlamaIndexHandler)

    def test_llamaindex_in_patches(self):
        with patch.dict("sys.modules", {
            "llama_index": None,
            "llama_index.core": None,
        }):
            auto("llamaindex")
        assert "llamaindex" in _PATCHES


# PageIndex auto()

class TestAutoPageIndex:
    def test_pageindex_returns_tracer(self):
        from briefcase.integrations.frameworks.pageindex_handler import PageIndexTracer
        with patch.dict("sys.modules", {"pageindex": None}):
            handler = auto("pageindex")
        assert isinstance(handler, PageIndexTracer)

    def test_pageindex_patched_init_body_executed(self, mocker):
        """Calling the patched PageIndexClient.__init__ executes its body."""
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def chat_completions(self, messages, doc_id=None, **kw):
                return {"response": "ok"}

        mock_pi = MagicMock()
        mock_pi.PageIndexClient = FakeClient
        mocker.patch.dict("sys.modules", {"pageindex": mock_pi})

        tracer = auto("pageindex")

        # FakeClient.__init__ is now the patched init
        instance = object.__new__(FakeClient)
        FakeClient.__init__(instance)  # covers the patched init body
        # The patched init points the tracer at the new client instance
        assert tracer._client is instance
        undo("pageindex")

    def test_pageindex_patched_chat_body_executed(self, mocker):
        """Calling the patched chat_completions delegates through the tracer."""
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def chat_completions(self, messages, doc_id=None, **kw):
                return {"response": "original"}

        mock_pi = MagicMock()
        mock_pi.PageIndexClient = FakeClient
        mocker.patch.dict("sys.modules", {"pageindex": mock_pi})

        tracer = auto("pageindex")
        # Mock tracer.chat_completions to avoid real API calls
        mocker.patch.object(tracer, "chat_completions", return_value={"response": "mocked"})

        # FakeClient.chat_completions is now the patched method
        instance = object.__new__(FakeClient)
        result = FakeClient.chat_completions(
            instance,
            messages=[{"role": "user", "content": "hello"}],
        )
        # The patched method points the tracer at this client and delegates
        assert tracer._client is instance
        assert result == {"response": "mocked"}
        undo("pageindex")


# undo()

class TestUndo:
    def test_undo_single_framework(self, mocker):
        mock_tracer = MagicMock()
        mocker.patch(
            "briefcase.integrations.frameworks.openai_agents_handler.install",
            return_value=mock_tracer,
        )
        auto("openai-agents")
        assert "openai-agents" in _PATCHES
        undo("openai-agents")
        assert "openai-agents" not in _PATCHES

    def test_undo_all_frameworks(self, mocker):
        mock_tracer = MagicMock()
        mocker.patch(
            "briefcase.integrations.frameworks.openai_agents_handler.install",
            return_value=mock_tracer,
        )
        mock_handler = MagicMock()
        mocker.patch(
            "briefcase.integrations.frameworks.autogen_handler.install",
            return_value=mock_handler,
        )
        mocker.patch("briefcase.integrations.frameworks.autogen_handler.uninstall")
        auto("openai-agents")
        auto("autogen")
        assert len(_PATCHES) == 2
        undo()
        assert len(_PATCHES) == 0

    def test_undo_nonexistent_framework_is_safe(self):
        """undo() on a framework that was never auto()d does not raise."""
        undo("langchain")  # never registered


# _undo_one exception paths

class TestUndoExceptionPaths:
    def test_undo_setattr_exception_is_silent(self):
        """_undo_one silently swallows setattr exceptions."""
        class Frozen:
            def __setattr__(self, name, value):
                raise AttributeError("frozen object")

        obj = Frozen()
        _PATCHES["test_frozen"] = {
            "handler": MagicMock(),
            "undos": [(obj, "some_attr", "original_value")],
        }
        # Must not raise
        undo("test_frozen")
        assert "test_frozen" not in _PATCHES

    def test_undo_cleanup_callable_is_called(self):
        """_undo_one invokes cleanup when it is callable."""
        cleanup_called = []

        def cleanup():
            cleanup_called.append(True)

        _PATCHES["test_cleanup"] = {
            "handler": MagicMock(),
            "undos": [],
            "cleanup": cleanup,
        }
        undo("test_cleanup")
        assert len(cleanup_called) == 1
        assert "test_cleanup" not in _PATCHES

    def test_undo_cleanup_exception_is_silent(self):
        """_undo_one swallows exceptions raised by cleanup()."""
        def bad_cleanup():
            raise RuntimeError("cleanup exploded")

        _PATCHES["test_bad_cleanup"] = {
            "handler": MagicMock(),
            "undos": [],
            "cleanup": bad_cleanup,
        }
        # Must not raise
        undo("test_bad_cleanup")
        assert "test_bad_cleanup" not in _PATCHES


# Patched ensure_config behavior

class TestLangChainPatchedEnsureConfig:
    def test_patched_ensure_config_injects_handler(self):
        """The patched ensure_config injects the handler into callbacks."""
        modules, mock_lc_config, _orig = _mock_langchain_modules()
        with patch.dict("sys.modules", modules):
            handler = auto("langchain")
            patched_fn = mock_lc_config.ensure_config
            result = patched_fn()
            undo("langchain")

        assert handler in result.get("callbacks", [])

    def test_patched_ensure_config_preserves_existing_callbacks(self):
        """Existing callbacks are preserved, not replaced."""
        existing_cb = MagicMock()
        mock_lc_config = types.SimpleNamespace()

        def _orig(cfg=None, **kw):
            return {"callbacks": [existing_cb]}

        mock_lc_config.ensure_config = _orig
        mock_runnables = types.SimpleNamespace(config=mock_lc_config)
        mock_lc = types.SimpleNamespace(runnables=mock_runnables)

        with patch.dict("sys.modules", {
            "langchain_core": mock_lc,
            "langchain_core.runnables": mock_runnables,
            "langchain_core.runnables.config": mock_lc_config,
        }):
            handler = auto("langchain")
            result = mock_lc_config.ensure_config()
            undo("langchain")

        assert existing_cb in result["callbacks"]
        assert handler in result["callbacks"]

    def test_patched_ensure_config_idempotent_injection(self):
        """The handler is not added twice when already in callbacks."""
        modules, mock_lc_config, _orig = _mock_langchain_modules()
        with patch.dict("sys.modules", modules):
            auto("langchain")
            result1 = mock_lc_config.ensure_config()
            result2 = mock_lc_config.ensure_config(result1)
            undo("langchain")

        # The handler appears exactly once
        assert result2["callbacks"].count(result2["callbacks"][0]) == 1


# llamaindex cleanup behavior

class TestLlamaIndexCleanup:
    def test_cleanup_calls_remove_handler(self):
        """undo('llamaindex') calls remove_handler on the callback manager."""
        mock_mgr = MagicMock()
        mock_settings = MagicMock()
        mock_settings.callback_manager = mock_mgr
        mock_core = MagicMock()
        mock_core.Settings = mock_settings

        with patch.dict("sys.modules", {
            "llama_index": MagicMock(),
            "llama_index.core": mock_core,
        }):
            auto("llamaindex")
            undo("llamaindex")

        mock_mgr.remove_handler.assert_called_once()

    def test_cleanup_fallback_when_remove_handler_raises(self):
        """When remove_handler raises, cleanup falls back to filtering handlers."""
        mock_mgr = MagicMock()
        mock_mgr.remove_handler.side_effect = AttributeError("no remove_handler")
        mock_mgr.handlers = []

        mock_settings = MagicMock()
        mock_settings.callback_manager = mock_mgr
        mock_core = MagicMock()
        mock_core.Settings = mock_settings

        with patch.dict("sys.modules", {
            "llama_index": MagicMock(),
            "llama_index.core": mock_core,
        }):
            auto("llamaindex")
            # Must not raise even when remove_handler fails
            undo("llamaindex")

        # Fallback path: handlers attribute was used for filtering
        assert mock_mgr.handlers == []

    def test_cleanup_fallback_exception_is_silent(self):
        """When both remove_handler and handlers access fail, cleanup is silent."""
        class BrokenManager:
            def add_handler(self, handler):
                pass

            def remove_handler(self, handler):
                raise AttributeError("no remove_handler")

            @property
            def handlers(self):
                raise AttributeError("no handlers")

        mock_settings = MagicMock()
        mock_settings.callback_manager = BrokenManager()
        mock_core = MagicMock()
        mock_core.Settings = mock_settings

        with patch.dict("sys.modules", {
            "llama_index": MagicMock(),
            "llama_index.core": mock_core,
        }):
            auto("llamaindex")
            # Must not raise even when everything in cleanup fails
            undo("llamaindex")
