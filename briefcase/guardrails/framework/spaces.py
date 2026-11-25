"""Policy spaces: the shape of what a guardrail evaluates, and space algebra.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import fnmatch
import random
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from briefcase.guardrails._types import (
    EvalRequest,
)


# ============================================================================
# 2. SPACE ABSTRACTIONS — Describes what a guardrail evaluates
# ============================================================================
#
# Like gymnasium.spaces.Box/Discrete/Dict, PolicySpace describes the SHAPE
# of valid inputs. This enables:
#   - Automatic input validation before evaluation
#   - Random sampling for testing (fuzz, adversarial, simulation)
#   - Self-documenting guardrails (introspect what they care about)
#   - Type-safe composition (verify two guardrails are compatible)
#

@dataclass
class SpaceBound:
    """Numeric bound for a context attribute."""
    low: float = float("-inf")
    high: float = float("inf")
    dtype: str = "float"  # "float" | "int"


@dataclass
class PolicySpace:
    """Describes the evaluation domain of a GuardrailEnv.

    Analogous to gymnasium.spaces.Dict — a named collection of sub-spaces.
    """
    agents: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=lambda: ["access"])
    resources: List[str] = field(default_factory=list)  # glob patterns
    context_schema: Dict[str, SpaceBound] = field(default_factory=dict)

    def contains(self, request: EvalRequest) -> bool:
        """Check if a request is within this space (like Space.contains)."""
        if self.agents and request.agent not in self.agents:
            return False
        if self.actions and request.action not in self.actions:
            return False
        for attr, bound in self.context_schema.items():
            val = request.context.get(attr)
            if val is not None and isinstance(val, (int, float)):
                if val < bound.low or val > bound.high:
                    return False
        return True

    def sample(self, n: int = 1, seed: Optional[int] = None) -> List[EvalRequest]:
        """Generate random valid requests (like Space.sample).

        Useful for fuzz testing, simulation warm-up, and adversarial generation.
        """
        rng = random.Random(seed)
        results = []
        for _ in range(n):
            ctx: Dict[str, Any] = {}
            for attr, bound in self.context_schema.items():
                lo = max(bound.low, -1e6)
                hi = min(bound.high, 1e6)
                if bound.dtype == "int":
                    ctx[attr] = rng.randint(int(lo), int(hi))
                else:
                    ctx[attr] = round(rng.uniform(lo, hi), 4)
            results.append(EvalRequest(
                agent=rng.choice(self.agents) if self.agents else "default",
                action=rng.choice(self.actions) if self.actions else "access",
                resource=rng.choice(self.resources) if self.resources else "/",
                context=ctx,
            ))
        return results

    def boundary_samples(self, n_per_bound: int = 5) -> List[EvalRequest]:
        """Generate samples at boundary conditions (like SMT counter-examples).

        For each numeric context attribute, produces values at low, high, and
        epsilon above/below the bounds. This is the complement to the SMT
        solver — heuristic boundary probing when formal verification is
        unavailable.
        """
        samples = []
        for attr, bound in self.context_schema.items():
            if bound.low == float("-inf") and bound.high == float("inf"):
                continue
            boundary_values = []
            if bound.low != float("-inf"):
                eps = 1 if bound.dtype == "int" else 0.01
                boundary_values.extend([bound.low - eps, bound.low, bound.low + eps])
            if bound.high != float("inf"):
                eps = 1 if bound.dtype == "int" else 0.01
                boundary_values.extend([bound.high - eps, bound.high, bound.high + eps])
            for val in boundary_values:
                samples.append(EvalRequest(
                    agent=self.agents[0] if self.agents else "default",
                    action=self.actions[0] if self.actions else "access",
                    resource=self.resources[0] if self.resources else "/",
                    context={attr: int(val) if bound.dtype == "int" else val},
                ))
        return samples


# ============================================================================
# 10. SPACE ALGEBRA — Composition checking
# ============================================================================
#
# Like gymnasium.spaces.utils (flatdim, flatten, unflatten) but for
# PolicySpace. Enables answering: "Can these two guardrails be composed?"
# "What's the combined evaluation domain of this pipeline?"
#

class SpaceAlgebra:
    """Operations on PolicySpaces for composition checking.

    When stacking guardrails, you need to know if they're compatible.
    SpaceAlgebra provides union (what either accepts), intersection
    (what both accept), and compatibility checking.

    Gymnasium parallel: gymnasium.spaces.utils provides flatten/unflatten;
    we provide set-algebraic operations because guardrail composition is
    inherently a set theory problem (who can do what with which data).
    """

    @staticmethod
    def union(a: PolicySpace, b: PolicySpace) -> PolicySpace:
        """Union of two spaces — accepts inputs valid in EITHER space.

        Useful for AnyOf composition: if either guardrail accepts the
        request shape, the composition does too.
        """
        agents = list(set(a.agents) | set(b.agents)) if a.agents or b.agents else []
        actions = list(set(a.actions) | set(b.actions)) if a.actions or b.actions else []
        resources = list(set(a.resources) | set(b.resources)) if a.resources or b.resources else []

        # For context schema: take the wider bounds
        all_attrs = set(list(a.context_schema.keys()) + list(b.context_schema.keys()))
        context_schema = {}
        for attr in all_attrs:
            bound_a = a.context_schema.get(attr, SpaceBound())
            bound_b = b.context_schema.get(attr, SpaceBound())
            context_schema[attr] = SpaceBound(
                low=min(bound_a.low, bound_b.low),
                high=max(bound_a.high, bound_b.high),
                dtype=bound_a.dtype,
            )

        return PolicySpace(
            agents=sorted(agents),
            actions=sorted(actions),
            resources=sorted(resources),
            context_schema=context_schema,
        )

    @staticmethod
    def intersection(a: PolicySpace, b: PolicySpace) -> PolicySpace:
        """Intersection of two spaces — accepts inputs valid in BOTH spaces.

        Useful for AllOf composition: both guardrails must accept the
        request shape for the composition to evaluate it.
        """
        # If either has agents, intersect; if one is empty (no constraint), use the other
        if a.agents and b.agents:
            agents = sorted(set(a.agents) & set(b.agents))
        elif a.agents:
            agents = list(a.agents)
        elif b.agents:
            agents = list(b.agents)
        else:
            agents = []

        if a.actions and b.actions:
            actions = sorted(set(a.actions) & set(b.actions))
        elif a.actions:
            actions = list(a.actions)
        elif b.actions:
            actions = list(b.actions)
        else:
            actions = []

        if a.resources and b.resources:
            resources = SpaceAlgebra._glob_intersect_resources(
                a.resources, b.resources,
            )
        elif a.resources:
            resources = list(a.resources)
        elif b.resources:
            resources = list(b.resources)
        else:
            resources = []

        # For context schema: take the tighter bounds
        all_attrs = set(list(a.context_schema.keys()) + list(b.context_schema.keys()))
        context_schema = {}
        for attr in all_attrs:
            bound_a = a.context_schema.get(attr, SpaceBound())
            bound_b = b.context_schema.get(attr, SpaceBound())
            context_schema[attr] = SpaceBound(
                low=max(bound_a.low, bound_b.low),
                high=min(bound_a.high, bound_b.high),
                dtype=bound_a.dtype,
            )

        return PolicySpace(
            agents=agents,
            actions=actions,
            resources=resources,
            context_schema=context_schema,
        )

    @staticmethod
    def is_compatible(a: PolicySpace, b: PolicySpace) -> bool:
        """Check if two spaces have a non-empty intersection.

        Returns False if composing them would create an impossible
        evaluation domain (e.g., no shared agents, disjoint resources,
        or inverted bounds).
        """
        inter = SpaceAlgebra.intersection(a, b)

        # Check for empty discrete intersections
        if a.agents and b.agents and not inter.agents:
            return False
        if a.actions and b.actions and not inter.actions:
            return False
        if a.resources and b.resources and not inter.resources:
            return False

        # Check for inverted numeric bounds
        for attr, bound in inter.context_schema.items():
            if bound.low > bound.high:
                return False

        return True

    @staticmethod
    def _glob_intersect_resources(
        a_resources: List[str], b_resources: List[str],
    ) -> List[str]:
        """Compute glob-aware resource intersection.

        Two resources overlap if either fnmatch-matches the other.
        When a pattern matches a literal, the more specific (literal)
        form is kept.  When two patterns match each other (e.g. both
        identical), one copy is kept.
        """
        result: List[str] = []
        for ra in a_resources:
            for rb in b_resources:
                if ra == rb:
                    if ra not in result:
                        result.append(ra)
                elif fnmatch.fnmatch(rb, ra):
                    # ra is a broader pattern that matches rb
                    if rb not in result:
                        result.append(rb)
                elif fnmatch.fnmatch(ra, rb):
                    # rb is a broader pattern that matches ra
                    if ra not in result:
                        result.append(ra)
        return sorted(result)

    @staticmethod
    def dimensionality(space: PolicySpace) -> Dict[str, int]:
        """Report the cardinality of each dimension.

        Like gymnasium.spaces.utils.flatdim — useful for understanding
        the size of the evaluation domain.
        """
        return {
            "agents": len(space.agents) if space.agents else -1,  # -1 = unbounded
            "actions": len(space.actions) if space.actions else -1,
            "resources": len(space.resources) if space.resources else -1,
            "context_attrs": len(space.context_schema),
        }
