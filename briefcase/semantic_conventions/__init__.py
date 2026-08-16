"""
Semantic conventions for Briefcase SDK.

This module provides standardized attribute names for telemetry data
to ensure consistency across all integrations and features.
"""

from briefcase.semantic_conventions import lakefs
from briefcase.semantic_conventions import workflow
from briefcase.semantic_conventions import rag
from briefcase.semantic_conventions import external_data
from briefcase.semantic_conventions import cowork
from briefcase.semantic_conventions import agent_state
from briefcase.semantic_conventions import controls

__all__ = [
    "lakefs",
    "workflow",
    "rag",
    "external_data",
    "cowork",
    "agent_state",
    "controls",
]
