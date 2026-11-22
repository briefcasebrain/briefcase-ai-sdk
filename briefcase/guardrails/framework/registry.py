"""String-based guardrail registration and instantiation.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)


# Namespace identifier pattern — alphanumeric, hyphens, underscores only
_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# ============================================================================
# 9. REGISTRY — String-based instantiation (gym.register / gym.make)
# ============================================================================
#
# This is THE critical abstraction for ecosystem growth. Gymnasium has
# >1,000 registered environments because of gym.register() + gym.make().
#
# Without a registry, every new guardrail type requires an import and
# explicit construction. With a registry, it's:
#
#   guardrails.make("pii-check-v1", confidence_threshold=0.85)
#
# Three registries, mirroring Gymnasium's layered approach:
#   - GuardrailRegistry   → envs (gym.envs.registry)
#   - WrapperRegistry     → wrappers (gym.wrappers)
#   - InjectionRegistry   → adversarial tests (agentdojo.injections)
#

@dataclass
class EnvSpec:
    """Specification for a registered guardrail environment.

    Like gymnasium.envs.registration.EnvSpec — stores everything needed
    to reconstruct a GuardrailEnv from a string identifier.

    The entry_point is a string in "module:ClassName" format, resolved
    lazily at make() time. This allows registration without importing
    the implementation (critical for plugin discovery).
    """
    id: str                               # e.g. "rbac-env-v1"
    entry_point: str                      # e.g. "mypackage.guardrails:MyGuardrailEnv"
    kwargs: Dict[str, Any] = field(default_factory=dict)
    version: Optional[int] = None         # extracted from id if "-vN" suffix
    namespace: Optional[str] = None       # e.g. "acme" → "acme/pii-check-v1"
    description: str = ""
    tags: List[str] = field(default_factory=list)  # e.g. ["rbac", "access-control"]
    max_eval_time_ms: Optional[float] = None  # performance contract


class GuardrailRegistry:
    """Global registry for string-based guardrail instantiation.

    Like gymnasium.envs.registry but for guardrails.

    Usage:
        registry = GuardrailRegistry()
        registry.register(
            id="my-check-v1",
            entry_point="mypackage.guardrails:MyGuardrailEnv",
            kwargs={"confidence_threshold": 0.85},
            tags=["access-control"],
        )

        env = registry.make("my-check-v1")
        env = registry.make("my-check-v1", confidence_threshold=0.9)  # override

    The register/make split enables:
      - Plugin authors register guardrails at import time
      - Users instantiate by string without knowing the module path
      - CI/CD systems enumerate all registered guardrails for testing
      - Domain packs register bundles of guardrails in one call
    """

    def __init__(self, plugins: bool = False) -> None:
        self._specs: Dict[str, EnvSpec] = {}
        # With ``plugins``, guardrails that installed packages declare under the
        # ``briefcase.guardrails`` entry point group are offered after the
        # registered ones (a registered id always wins). They are listed from
        # package metadata and imported only by make().
        self._plugins = plugins

    def _plugin_specs(self) -> Dict[str, EnvSpec]:
        if not self._plugins:
            return {}
        from briefcase import plugins as _plugins

        out: Dict[str, EnvSpec] = {}
        for plugin in _plugins.entries(_PLUGIN_GROUP):
            if plugin.name in self._specs or plugin.name in out:
                continue
            if plugin.state not in (_plugins.AVAILABLE, _plugins.LOADED):
                continue
            out[plugin.name] = EnvSpec(
                id=plugin.name, entry_point=plugin.value,
                description=f"plugin from {plugin.dist or 'an unknown distribution'}",
                tags=["plugin"],
            )
        return out

    def register(
        self,
        id: str,
        entry_point: str,
        kwargs: Optional[Dict[str, Any]] = None,
        namespace: Optional[str] = None,
        description: str = "",
        tags: Optional[List[str]] = None,
        max_eval_time_ms: Optional[float] = None,
    ) -> None:
        """Register a guardrail environment.

        Args:
            id: Unique identifier, e.g. "pii-check-v1". Convention:
                {domain}-{name}-v{version}
            entry_point: "module.path:ClassName" — resolved at make() time
            kwargs: Default constructor arguments
            namespace: Optional vendor/org prefix. If provided, the
                effective id becomes "namespace/id" (e.g. "acme/pii-check-v1").
                The env can be made via either the full namespaced id or
                the short id (if unambiguous).
            description: Human-readable description
            tags: Searchable tags for discovery
            max_eval_time_ms: Performance contract (optional)
        """
        if namespace and not _NAMESPACE_RE.match(namespace):
            raise ValueError(
                f"Invalid namespace '{namespace}': must match [a-zA-Z0-9_-]+"
            )

        effective_id = f"{namespace}/{id}" if namespace else id

        if effective_id in self._specs:
            raise ValueError(f"Guardrail '{effective_id}' already registered")

        version = None
        bare_id = id  # version extracted from the bare id, not the namespace
        if "-v" in bare_id:
            try:
                version = int(bare_id.rsplit("-v", 1)[1])
            except (ValueError, IndexError):
                pass

        self._specs[effective_id] = EnvSpec(
            id=effective_id,
            entry_point=entry_point,
            kwargs=kwargs or {},
            version=version,
            namespace=namespace,
            description=description,
            tags=tags or [],
            max_eval_time_ms=max_eval_time_ms,
        )

    def _resolve_id(self, id: str) -> str:
        """Resolve a potentially short id to its full registered key.

        Lookup order:
          1. Exact match (works for both namespaced and bare ids)
          2. Short-id fallback: if id has no '/', search for specs
             whose bare suffix matches. Succeeds only if exactly one
             match exists (unambiguous).
          3. Version-aware fallback: if id has no '-v' suffix and no '/',
             search for specs matching '{id}-v{N}' and resolve to the
             highest version number.
        """
        if id in self._specs:
            return id
        if "/" not in id:
            matches = [
                key for key in self._specs
                if "/" in key and key.split("/", 1)[1] == id
            ]
            if len(matches) == 1:
                return matches[0]

            # Version-aware: if no '-v' suffix, search for versioned variants
            if "-v" not in id:
                versioned = []
                for key, spec in self._specs.items():
                    # Match bare keys like '{id}-v{N}' or namespaced '{ns}/{id}-v{N}'
                    bare = key.split("/", 1)[1] if "/" in key else key
                    if bare.startswith(id + "-v") and spec.version is not None:
                        versioned.append((spec.version, key))
                if versioned:
                    versioned.sort(key=lambda x: x[0], reverse=True)
                    return versioned[0][1]

        return id  # a plugin id, or a KeyError at the caller

    def make(self, id: str, **kwargs: Any) -> GuardrailEnv:
        """Instantiate a registered guardrail by id.

        Like gym.make() — resolves the entry_point, merges kwargs,
        and returns a ready-to-use GuardrailEnv.

        Accepts both full namespaced ids ("acme/pii-check-v1") and
        short ids ("pii-check-v1") when unambiguous.

        Args:
            id: Registered guardrail identifier
            **kwargs: Override default constructor arguments
        """
        resolved = self._resolve_id(id)
        spec = self._specs.get(resolved)
        plugin_spec = self._plugin_specs().get(resolved) if spec is None else None
        if spec is None and plugin_spec is None:
            available = ", ".join(sorted(list(self._specs) + list(self._plugin_specs()))) or "(none)"
            raise KeyError(
                f"Guardrail '{id}' not registered. Available: {available}"
            )

        if plugin_spec is not None:
            from briefcase import plugins as _plugins

            cls = _plugins.get(_PLUGIN_GROUP, resolved, self._specs)
            if cls is None:
                raise KeyError(f"Guardrail '{id}' cannot be used: "
                               f"{_plugins.explain(_PLUGIN_GROUP, resolved)}")
            spec = plugin_spec
        else:
            assert spec is not None
            cls = self._resolve_entry_point(spec.entry_point)
        merged = {**spec.kwargs, **kwargs}
        env = cls(**merged)

        if not isinstance(env, GuardrailEnv):
            raise TypeError(
                f"{spec.entry_point} returned {type(env).__name__}, "
                f"which does not satisfy GuardrailEnv protocol"
            )
        return env

    def spec(self, id: str) -> EnvSpec:
        """Get the spec for a registered guardrail."""
        resolved = self._resolve_id(id)
        if resolved in self._specs:
            return self._specs[resolved]
        plugin_spec = self._plugin_specs().get(resolved)
        if plugin_spec is None:
            raise KeyError(f"Guardrail '{id}' not registered")
        return plugin_spec

    def list(
        self,
        tags: Optional[List[str]] = None,
        namespace: Optional[str] = None,
    ) -> List[EnvSpec]:
        """List registered guardrails, optionally filtered by tags/namespace.

        Args:
            tags: If provided, only return specs matching ALL tags.
            namespace: If provided, only return specs in this namespace.
        """
        specs = list(self._specs.values()) + list(self._plugin_specs().values())
        if tags:
            tag_set = set(tags)
            specs = [s for s in specs if tag_set.issubset(set(s.tags))]
        if namespace is not None:
            specs = [s for s in specs if s.namespace == namespace]
        return sorted(specs, key=lambda s: s.id)

    def _resolve_entry_point(self, entry_point: str) -> type:
        """Resolve 'module.path:ClassName' to a class object."""
        if ":" not in entry_point:
            raise ValueError(
                f"entry_point must be 'module:ClassName', got: {entry_point}"
            )
        module_path, class_name = entry_point.rsplit(":", 1)
        import importlib
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls


# Module-level singleton — like gymnasium.envs.registry. It also offers the
# guardrails installed packages declare under the ``briefcase.guardrails`` entry
# point group (see briefcase.plugins).
_PLUGIN_GROUP = "briefcase.guardrails"
_default_registry = GuardrailRegistry(plugins=True)


def builtin_ids() -> List[str]:
    """Ids registered in code (the built-in envs and anything registered at import time);
    a plugin with one of these ids is shadowed."""
    try:
        import briefcase.guardrails.envs  # noqa: F401  (registers the built-in envs)
    except Exception:  # pragma: no cover - a broken optional env must not hide the list
        pass
    return list(_default_registry._specs)


def register(id: str, entry_point: str, **kwargs: Any) -> None:
    """Register a guardrail in the global registry.

    Convenience function — equivalent to guardrail_registry.register().

    Registry parameters (kwargs, namespace, description, tags,
    max_eval_time_ms) are forwarded as-is; any other keyword arguments
    become default constructor arguments for the guardrail.

    Usage:
        from briefcase.guardrails.framework import register, make

        register("my-guardrail-v1", "my_package:MyGuardrail", threshold=0.9)
        env = make("my-guardrail-v1")
    """
    registry_params = {
        k: kwargs.pop(k)
        for k in ("kwargs", "namespace", "description", "tags", "max_eval_time_ms")
        if k in kwargs
    }
    ctor_kwargs = {**(registry_params.pop("kwargs", None) or {}), **kwargs}
    _default_registry.register(
        id=id,
        entry_point=entry_point,
        kwargs=ctor_kwargs or None,
        **registry_params,
    )


def make(id: str, **kwargs: Any) -> GuardrailEnv:
    """Instantiate a guardrail from the global registry.

    Convenience function — equivalent to guardrail_registry.make().
    """
    return _default_registry.make(id, **kwargs)


def list_registered(tags: Optional[List[str]] = None) -> List[EnvSpec]:
    """List all registered guardrails in the global registry."""
    return _default_registry.list(tags=tags)
