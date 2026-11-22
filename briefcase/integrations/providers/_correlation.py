"""Correlation between framework handlers and provider instrumentation.

An application that runs LangChain (or LlamaIndex, CrewAI, the OpenAI Agents
SDK) on top of an instrumented provider SDK produces two records for one
model call: the framework handler's LLM run and the provider record. Both
are kept by default and tied together here, so a consumer can tell them
apart and count tokens once.

How it works:

- A framework handler calls :func:`llm_run_started` when the framework
  reports that an LLM run began and :func:`llm_run_finished` when it ended.
  The run is published in a :mod:`contextvars` variable, so it is visible
  to the provider call made *in the same thread or asyncio task* (and in
  tasks or ``contextvars.copy_context()`` runs started from it), and to
  nothing else.
- :class:`~briefcase.integrations.providers._base.ProviderInstrumentor`
  reads it when a provider call starts. The provider record then carries
  ``parent_run_id`` (the framework run's id, the field the LangChain and
  LlamaIndex handlers already use for run nesting) and
  ``parent_framework``.
- ``briefcase.auto(dedupe=...)`` chooses what happens to the pair:
  ``"none"`` (default) keeps both, ``"provider"`` drops the provider record
  of a call made under a framework LLM run, ``"framework"`` asks the
  handler to drop its LLM-level record when a provider record was emitted
  for it.

The scope object is shared between contexts on purpose: ``finished`` is a
flag on the object, not a context reset, so a framework that reports the
end of a run from another thread still deactivates it, and a stale scope
left behind in some context can never be attributed to a later call.

A run whose end the framework never reports (a callback that raised, a
stream the application abandoned) must not capture every later call made
in that context. Two guards bound that: a new LLM run starting in a context
supersedes whatever run was still marked active there (model calls do not
nest), and a run older than ``MAX_RUN_AGE_SECONDS`` is no longer offered to
provider calls at all.

What this cannot do: when a framework invokes the *start* callback in a
context the model call does not inherit (LangChain runs non-inline sync
handlers of an async chain in an executor with a copied context), the
provider call sees no scope. Both records are then emitted without a link,
whatever ``dedupe`` says.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any, Optional

DEDUPE_MODES = ("none", "provider", "framework")

#: A framework LLM run older than this is treated as abandoned: provider
#: calls are no longer linked to it. Generous on purpose (a long streamed
#: completion is still one run); it only has to stop a leak.
MAX_RUN_AGE_SECONDS = 900.0

_dedupe = "none"
_dedupe_lock = threading.Lock()


class LLMRunScope:
    """One framework LLM run that may contain provider calls."""

    __slots__ = ("framework", "run_id", "active", "provider_records", "started", "_token")

    def __init__(self, framework: str, run_id: str) -> None:
        self.framework = framework
        self.run_id = run_id
        self.started = time.monotonic()
        #: False once the framework reported the end of the run.
        self.active = True
        #: Provider records emitted while this run was active.
        self.provider_records = 0
        self._token: Any = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"LLMRunScope({self.framework!r}, {self.run_id!r}, "
                f"active={self.active}, provider_records={self.provider_records})")


_current: "contextvars.ContextVar[Optional[LLMRunScope]]" = contextvars.ContextVar(
    "briefcase_framework_llm_run", default=None,
)


def set_dedupe(mode: str) -> None:
    """Set the process-wide dedupe mode (see the module docstring)."""
    global _dedupe
    if mode not in DEDUPE_MODES:
        raise ValueError(f"dedupe must be one of {DEDUPE_MODES}, got {mode!r}")
    with _dedupe_lock:
        _dedupe = mode


def get_dedupe() -> str:
    return _dedupe


def llm_run_started(framework: str, run_id: Any) -> Optional[LLMRunScope]:
    """Publish "a framework LLM run is active" for the current context.

    Called by a framework handler from its LLM-start callback. Returns the
    scope to hand back to :func:`llm_run_finished`. Never raises."""
    try:
        previous = _current.get()
        if previous is not None:
            # Its end was never reported, or it is an outer run this one is
            # nested in; either way the new run is the one in flight.
            previous.active = False
        scope = LLMRunScope(str(framework), str(run_id))
        scope._token = _current.set(scope)
        return scope
    except Exception:
        return None


def llm_run_finished(scope: Optional[LLMRunScope]) -> bool:
    """Mark the run as ended. Safe to call from any thread or task, more
    than once, and with ``None``.

    Returns True when the handler should drop its own LLM-level record for
    this run: ``dedupe="framework"`` is set and at least one provider
    record was emitted under the run. Never raises."""
    if scope is None:
        return False
    try:
        scope.active = False
        token, scope._token = scope._token, None
        if token is not None:
            try:
                _current.reset(token)
            except Exception:
                # Ended in a different context than it started in: the
                # variable there cannot be reset from here, and does not
                # need to be, since the scope is now inactive.
                pass
        return _dedupe == "framework" and scope.provider_records > 0
    except Exception:
        return False


def current_llm_run() -> Optional[LLMRunScope]:
    """The framework LLM run active in this context, or None."""
    try:
        scope = _current.get()
    except Exception:
        return None
    if scope is None or not scope.active:
        return None
    if time.monotonic() - scope.started > MAX_RUN_AGE_SECONDS:
        scope.active = False
        return None
    return scope


def reset_context() -> None:
    """Forget the run published in the current context (tests, and code
    that reuses a thread for unrelated work)."""
    try:
        scope = _current.get()
        if scope is not None:
            scope.active = False
        _current.set(None)
    except Exception:
        pass


__all__ = [
    "DEDUPE_MODES",
    "LLMRunScope",
    "current_llm_run",
    "get_dedupe",
    "llm_run_finished",
    "llm_run_started",
    "reset_context",
    "set_dedupe",
]
