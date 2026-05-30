"""Briefcase AI — Open-source decision tracking for AI."""

__version__ = "3.1.0"

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
# `briefcase.setup`, matching the README quickstart.
from briefcase.decorators import capture
from briefcase.config import BriefcaseConfig, setup

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
    "setup",
    "BriefcaseConfig",
    "__version__",
]
