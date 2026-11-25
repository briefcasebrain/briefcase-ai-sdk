"""Rendering / observability protocol.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

from typing import (
    Any,
    Dict,
    Protocol,
    runtime_checkable,
)


# ============================================================================
# 13. RENDERING / OBSERVABILITY PROTOCOL
# ============================================================================
#
# Gymnasium has Env.render() for visualization. For guardrails, "rendering"
# means structured observability output — not pixels but audit data.
#
# This protocol lets any env or wrapper emit structured observations
# that tools can consume (dashboards, CLI reporters, OTel exporters).
#

@runtime_checkable
class Renderable(Protocol):
    """Optional protocol for guardrails that support structured rendering.

    Like gymnasium.Env.render() but produces structured data instead
    of pixel buffers. Consumed by dashboards, CLI reporters, and
    OTel span exporters.

    Usage:
        if isinstance(env, Renderable):
            for frame in env.render():
                dashboard.push(frame)
    """

    def render(self, mode: str = "human") -> Dict[str, Any]:
        """Produce a structured observation.

        Modes:
          "human"     → Dict with readable strings
          "json"      → Dict with JSON-serializable values
          "otel"      → Dict with OTel attribute conventions
        """
        ...
