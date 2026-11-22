"""Briefcase AI — Open-source decision tracking for AI."""

__version__ = "4.4.0"

# Configure the library logger first (NullHandler by default; opt-in output via
# enable_logging() or BRIEFCASE_LOG_LEVEL) before any submodule logs.
from briefcase._logging import (
    disable_logging,
    enable_logging,
    get_logger,
    set_log_level,
)

# Ergonomic top-level re-exports so the primary entry points are discoverable
# (and autocompletable by AI coding tools) as `briefcase.capture` /
# `briefcase.observe` / `briefcase.setup`, matching the README quickstart.
import sys as _sys
import types as _types

from briefcase.decorators import capture
from briefcase.config import BriefcaseConfig, setup
from briefcase._observe import observe
from briefcase._export_mixin import (
    configure_export,
    export_stats,
    flush,
    shutdown,
    wait_for_pending_exports,
)
from briefcase.exporters import (
    BaseExporter,
    ConsoleExporter,
    GCPCloudLoggingExporter,
    HTTPExporter,
    JSONLFileExporter,
    MemoryExporter,
    OTelExporter,
)
import briefcase.auto as _auto_module


# The compiled core is optional at import time: the pure-Python surface
# (capture, observe, exporters, controls, events, routing, ...) works without
# it, e.g. from a source checkout or on a platform with no prebuilt wheel.
# Native-backed names raise a clear ImportError only when first used.
_NATIVE_NAMES = (
    "DecisionSnapshot",
    "ExecutionContext",
    "HardwareMetadata",
    "Input",
    "ModelParameters",
    "Output",
    "Snapshot",
    "SnapshotQuery",
    "init",
    "init_with_config",
    "is_initialized",
)

try:
    from briefcase._native import (
        DecisionSnapshot,
        ExecutionContext,
        HardwareMetadata,
        Input,
        ModelParameters,
        Output,
        Snapshot,
        SnapshotQuery,
        init,
        init_with_config,
        is_initialized,
    )
except ImportError as _exc:
    _native_import_error: "ImportError | None" = _exc

    def __getattr__(name: str):
        if name in _NATIVE_NAMES:
            raise ImportError(
                f"briefcase.{name} needs the compiled briefcase core "
                "(briefcase._native), which is not available in this "
                "environment. Install a prebuilt wheel with 'pip install "
                "briefcase-ai', or build from source with 'maturin develop'. "
                "The pure-Python features (capture, observe, exporters, "
                "controls, events, routing) work without it."
            ) from _native_import_error
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
else:
    _native_import_error = None


_LAZY_ATTRS = {"budget": ("briefcase.budgets", "budget"),
               "budget_scope": ("briefcase.budgets", "budget_scope")}
_native_getattr = globals().get("__getattr__")


def __getattr__(name: str):  # noqa: F811
    """``briefcase.budget`` / ``briefcase.budget_scope`` load the budgets package on first
    use, so importing briefcase does not pay for it."""
    target = _LAZY_ATTRS.get(name)
    if target is not None:
        import importlib

        value = getattr(importlib.import_module(target[0]), target[1])
        globals()[name] = value
        return value
    if _native_getattr is not None:
        return _native_getattr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _CallableAutoModule(_types.ModuleType):
    """Lets ``briefcase.auto("langchain")`` work while ``briefcase.auto`` stays
    the submodule, so ``from briefcase.auto import auto, undo`` and
    ``briefcase.auto.undo()`` keep working unchanged."""

    def __call__(self, *args, **kwargs):
        return self.auto(*args, **kwargs)


_auto_module.__class__ = _CallableAutoModule
auto = _sys.modules["briefcase.auto"]

from briefcase.prompts import prompt  # noqa: E402

__all__ = [
    "prompt",
    "budget",
    "budget_scope",
    "DecisionSnapshot",
    "ExecutionContext",
    "HardwareMetadata",
    "Input",
    "ModelParameters",
    "Output",
    "Snapshot",
    "SnapshotQuery",
    "init",
    "init_with_config",
    "is_initialized",
    "capture",
    "observe",
    "setup",
    "auto",
    "flush",
    "shutdown",
    "wait_for_pending_exports",
    "configure_export",
    "export_stats",
    "BriefcaseConfig",
    "BaseExporter",
    "ConsoleExporter",
    "GCPCloudLoggingExporter",
    "HTTPExporter",
    "JSONLFileExporter",
    "MemoryExporter",
    "OTelExporter",
    "enable_logging",
    "set_log_level",
    "disable_logging",
    "get_logger",
    "__version__",
]
