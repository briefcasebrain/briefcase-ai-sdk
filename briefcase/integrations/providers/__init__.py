"""Provider auto-instrumentation: one decision record per model call.

Each provider module patches the main call paths of one SDK so that an
application emits decision records without being edited. They are normally
reached through :func:`briefcase.auto`::

    import briefcase
    briefcase.observe()            # destination from the environment
    briefcase.auto()               # every installed framework and provider
    briefcase.auto("openai")       # or one by name

Providers:

=================  =====================  ==================================
name               import detected        module
=================  =====================  ==================================
``openai``         ``openai``             :mod:`.openai`
``anthropic``      ``anthropic``          :mod:`.anthropic`
``google-genai``   ``google.genai``       :mod:`.google_genai`
``bedrock``        ``botocore``           :mod:`.bedrock`
=================  =====================  ==================================

Records use the ``@briefcase.capture`` layout plus the provider fields
documented in :mod:`._base`. Prompt and completion content is not recorded
unless ``capture_content=True`` (or ``"full"`` / ``"hash"``) is passed.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, NamedTuple

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import ProviderInstrumentor

logger = get_logger(__name__)


class ProviderSpec(NamedTuple):
    name: str
    #: Import name whose presence means the SDK is installed.
    detect: str
    #: Module that must finish importing before the patches can be applied.
    trigger: str
    #: Module under this package exposing ``instrument(**options)``.
    module: str


PROVIDERS: Dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec("openai", "openai", "openai", "openai"),
        ProviderSpec("anthropic", "anthropic", "anthropic", "anthropic"),
        ProviderSpec("google-genai", "google.genai", "google.genai", "google_genai"),
        ProviderSpec("bedrock", "botocore", "botocore.client", "bedrock"),
    )
}


def instrument(name: str, **options: Any) -> ProviderInstrumentor:
    """Patch one provider SDK and return its instrumentor.

    Raises:
        ValueError: ``name`` is not a known provider.
        ImportError: the provider's SDK is not installed.
        RuntimeError: the SDK is installed but none of the known patch
            targets exist in it, so nothing was instrumented.
    """
    spec = PROVIDERS.get(name)
    if spec is None:
        raise ValueError(f"Unknown provider: {name!r}. Supported: {sorted(PROVIDERS)}")
    module = importlib.import_module(f"{__name__}.{spec.module}")
    inst: ProviderInstrumentor = module.instrument(**options)
    if not inst.patched:
        inst.uninstall()
        raise RuntimeError(
            f"briefcase.auto: none of the known {name} patch targets exist in "
            f"the installed SDK (version {inst.sdk_version}; verified against "
            f"{getattr(module, 'VERIFIED_VERSION', 'unknown')}), so nothing was "
            "instrumented."
        )
    logger.debug(
        "briefcase.auto[%s]: patched %d targets (sdk %s)",
        name, len(inst.patched), inst.sdk_version,
    )
    return inst


__all__ = ["PROVIDERS", "ProviderInstrumentor", "ProviderSpec", "instrument"]
