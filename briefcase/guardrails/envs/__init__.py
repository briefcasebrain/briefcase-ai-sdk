"""Concrete GuardrailEnv implementations.

Importing this package registers the built-in environments with the
global guardrail registry, so they can be constructed by id:

    import briefcase.guardrails.envs
    from briefcase.guardrails.framework import make

    env = make("rbac-env-v1", agents=["agent-a"], allowed_actions=["read"])
"""

from briefcase.guardrails.envs.abac import ABACGuardrailEnv, ABACRule
from briefcase.guardrails.envs.rbac import RBACGuardrailEnv
from briefcase.guardrails.framework import _default_registry

__all__ = [
    "RBACGuardrailEnv",
    "ABACGuardrailEnv",
    "ABACRule",
]

_BUILTIN_ENVS = [
    (
        "rbac-env-v1",
        "briefcase.guardrails.envs.rbac:RBACGuardrailEnv",
        "Role-based access control with glob resource matching",
        ["rbac", "access-control"],
    ),
    (
        "abac-env-v1",
        "briefcase.guardrails.envs.abac:ABACGuardrailEnv",
        "Attribute-based access control over request context",
        ["abac", "access-control"],
    ),
]


def _register_builtin_envs() -> None:
    """Register the built-in envs, skipping ids that already exist."""
    for env_id, entry_point, description, tags in _BUILTIN_ENVS:
        if env_id in _default_registry._specs:
            continue
        _default_registry.register(
            id=env_id,
            entry_point=entry_point,
            description=description,
            tags=list(tags),
        )


_register_builtin_envs()
