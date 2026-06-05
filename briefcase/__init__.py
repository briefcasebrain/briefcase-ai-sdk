"""Briefcase AI — Open-source decision tracking for AI."""

__version__ = "3.3.1"

# Configure the library logger first (NullHandler by default; opt-in output via
# enable_logging() or BRIEFCASE_LOG_LEVEL) before any submodule logs.
from briefcase._logging import (
    disable_logging,
    enable_logging,
    get_logger,
    set_log_level,
)

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

# Ergonomic top-level re-exports so the primary entry points are discoverable
# (and autocompletable by AI coding tools) as `briefcase.capture` /
# `briefcase.observe` / `briefcase.setup`, matching the README quickstart.
from briefcase.decorators import capture
from briefcase.config import BriefcaseConfig, setup
from briefcase._observe import observe

__all__ = [
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
    "BriefcaseConfig",
    "enable_logging",
    "set_log_level",
    "disable_logging",
    "get_logger",
    "__version__",
]
