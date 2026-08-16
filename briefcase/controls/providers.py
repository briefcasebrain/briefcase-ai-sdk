"""
Provider registry for LLM text completions.

A Strategy-pattern seam between "which model vendors are configured" and
"run this completion": providers implement a small Protocol, a registry
holds them and yields candidates in a deterministic order (preferred first,
then an injectable fallback order, deduplicated), and ``complete_text``
walks that ladder returning the first success.

The SDK ships no concrete provider classes; applications register their own
implementations (OpenAI, Anthropic, Bedrock, or anything else) so this
module stays dependency-free.

The scoped-credential seam supports bring-your-own-key tenancy: a caller
can pin a credential to one provider for the duration of a request via
``scoped_credential``. While a scoped credential is in force,
``resolve_credential`` returns it for that provider only and refuses to
fall through to another provider's platform credential, so a tenant that
supplied a key for provider X never has traffic billed to the platform's
account on provider Y. Credential values never leave this module.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    Iterator,
    MutableMapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)


@dataclass(frozen=True)
class TextCompletion:
    """Result of one text completion. Token counts are 0 when the provider
    does not report usage."""

    text: str
    input_tokens: int
    output_tokens: int
    model_id: str
    provider: str


@runtime_checkable
class LLMProvider(Protocol):
    """Provider Strategy contract.

    Implementations are cheap to construct and hold configuration only;
    expensive resources (HTTP clients, sessions) are created lazily inside
    ``complete``. Applications may extend this Protocol with their own
    methods; the registry uses only these three members.
    """

    name: str

    def available(self) -> bool:
        """True when this provider's credentials or role are present at
        runtime. Must not perform network calls."""
        ...

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> TextCompletion:
        """Send a (system, prompt) pair and return the text plus usage."""
        ...


class NoProviderAvailable(RuntimeError):
    """No registered provider could produce a completion. Carries the last
    provider error as ``__cause__`` when at least one provider was tried."""


# One ContextVar for the whole process, mirroring how platform credentials
# live in process-wide env vars. The value is (provider_name, credential).
_scoped_credential: ContextVar[Optional[Tuple[str, str]]] = ContextVar(
    "briefcase_controls_scoped_credential", default=None
)


@contextmanager
def scoped_credential(provider: Optional[str], credential: Optional[str]) -> Iterator[None]:
    """Run a block with a specific provider credential in force.

    Passing None or an empty string for either argument is a no-op, so
    callers do not need to branch on whether a tenant configured a key.
    The provider name is normalized (stripped, lowercased) before storage.
    """
    if not provider or not credential:
        yield
        return
    token = _scoped_credential.set((provider.strip().lower(), credential))
    try:
        yield
    finally:
        _scoped_credential.reset(token)


def scoped_provider_name() -> Optional[str]:
    """The provider the current scoped credential belongs to, or None. The
    credential value itself is never exposed by this function."""
    current = _scoped_credential.get()
    return current[0] if current else None


def resolve_credential(provider: str, env_var: str) -> Optional[str]:
    """The credential ``provider`` should use right now.

    A scoped credential wins for its own provider only. While a scoped
    credential for a different provider is in force, this returns None
    rather than the platform credential from ``env_var``: a tenant that
    supplied a key for one provider has not consented to its traffic
    running on another provider's platform account. With no scoped
    credential in force, the ``env_var`` environment variable supplies the
    platform credential (injectable per provider by the caller).
    """
    current = _scoped_credential.get()
    if current is not None:
        if current[0] == provider:
            return current[1]
        return None
    return os.environ.get(env_var) or None


class ProviderRegistry:
    """Holds providers and selects them in a deterministic order.

    ``fallback_order`` lists provider names to try when the preferred one
    is missing or unavailable. ``fallback_label`` is what ``resolve_mode``
    returns when nothing is available (for example a "rule_based" or
    "disabled" label the application surfaces in health checks).
    ``preferred_env_var`` optionally names an environment variable that
    supplies the preferred provider for ``complete_text`` when the caller
    does not pass one. ``providers`` optionally supplies the backing
    mutable mapping; the registry reads it live, so an application can keep
    its own module-level mapping as the single source of truth.
    """

    def __init__(
        self,
        *,
        fallback_order: Sequence[str] = (),
        fallback_label: str = "unavailable",
        preferred_env_var: Optional[str] = None,
        providers: Optional[MutableMapping[str, LLMProvider]] = None,
    ) -> None:
        self._providers: MutableMapping[str, LLMProvider] = (
            providers if providers is not None else {}
        )
        self.fallback_order: Tuple[str, ...] = tuple(fallback_order)
        self.fallback_label = fallback_label
        self.preferred_env_var = preferred_env_var

    @property
    def providers(self) -> MutableMapping[str, LLMProvider]:
        """The live backing mapping of name to provider."""
        return self._providers

    def register(self, provider: LLMProvider) -> LLMProvider:
        """Add (or replace) a provider under its own ``name``; returns it so
        registration can wrap construction."""
        self._providers[provider.name] = provider
        return provider

    def select_providers(self, *, preferred: Optional[str] = None) -> Iterator[LLMProvider]:
        """Yield available providers in the order callers should try them:
        the preferred provider first (when registered and available), then
        the fallback order with duplicates skipped. Callers iterate and
        break on the first success."""
        seen: set = set()

        def _emit(name: str) -> Iterator[LLMProvider]:
            if name in seen:
                return
            seen.add(name)
            provider = self._providers.get(name)
            if provider is not None and provider.available():
                yield provider

        if preferred:
            yield from _emit(preferred.strip().lower())
        for name in self.fallback_order:
            yield from _emit(name)

    def complete_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 600,
        preferred: Optional[str] = None,
    ) -> TextCompletion:
        """Run a completion through the provider ladder and return the first
        success.

        Raises ``NoProviderAvailable`` when no provider is configured or
        every configured provider errored; the message carries the last
        failure truncated to 200 characters and the caller decides how to
        degrade. When ``preferred`` is not passed and ``preferred_env_var``
        is set, that environment variable supplies the preference.
        """
        pref = preferred
        if not pref and self.preferred_env_var:
            pref = os.environ.get(self.preferred_env_var) or None
        last_exc: Optional[Exception] = None
        for provider in self.select_providers(preferred=pref):
            try:
                return provider.complete(system=system, prompt=prompt, max_tokens=max_tokens)
            except Exception as exc:  # try the next provider, remember the last error
                last_exc = exc
        if last_exc is not None:
            raise NoProviderAvailable(str(last_exc)[:200]) from last_exc
        raise NoProviderAvailable("no provider configured")

    def resolve_mode(self, *, preferred: Optional[str] = None) -> str:
        """The name of the first provider ``select_providers`` would yield,
        or ``fallback_label`` when nothing is available. Never constructs a
        network client, so it is safe for health checks."""
        for provider in self.select_providers(preferred=preferred):
            return provider.name
        return self.fallback_label


__all__ = [
    "LLMProvider",
    "NoProviderAvailable",
    "ProviderRegistry",
    "TextCompletion",
    "resolve_credential",
    "scoped_credential",
    "scoped_provider_name",
]
