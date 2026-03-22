"""Briefcase AI — Open-source decision tracking for AI."""

__version__ = "3.0.0"

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
    "__version__",
]
