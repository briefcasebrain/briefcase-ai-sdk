"""Budgets: hard and soft spending limits on the LLM calls an app makes,
enforced in-process before each call is sent.

::

    import briefcase
    from briefcase import budgets

    briefcase.auto()                                   # instrument the provider SDKs
    budgets.budget("daily", usd=20, soft_usd=15)       # block once $20/day is spent
    budgets.budget("per-tenant", usd=2, per="tenant", window="month",
                   on_exceed="degrade", degrade_to="gpt-4o-mini")

    with budgets.budget_scope(tenant="acme"):
        client.chat.completions.create(model="gpt-4o", messages=[...], max_tokens=300)

How a call is counted (every call made through an SDK that
``briefcase.auto()`` instrumented: openai, anthropic, google-genai, Bedrock):

1. Before the request is sent, the most it can cost is *reserved*: input
   tokens (estimated high, or counted by your ``token_counter``) plus the
   request's output cap (``max_tokens`` & co; the model's maximum when the
   request sets none), priced with :mod:`briefcase.pricing`. A reservation
   that would cross a hard limit is refused: ``on_exceed="block"`` raises
   :class:`BudgetExceeded` and nothing is sent.
2. When the response (or the end of the stream) arrives, the reservation is
   *settled* to the cost of the usage the provider reported, and the
   difference is released. When no usage was reported (a stream without
   usage, an abandoned stream, a timeout) the reservation stays counted and
   the record says ``cost_source: "reserved"``; a 4xx error or a call that
   was never sent is released (``"not_billed"``).

Windows are calendar windows in UTC (``hour``, ``day``, ``month``) or
``total``. Counters live in an in-process store by default; pass a
:class:`RedisBudgetStore` or :class:`SQLBudgetStore` (or ``store="redis://..."``)
to share limits across processes and hosts.

Every decision is added to the call's record: ``budget_effect``,
``budget_scope``, ``budget_checks``, ``cost_usd``, ``cost_source``,
``cost_reserved_usd`` and ``budget_degraded_from``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

from briefcase._logging import get_logger
from briefcase.budgets._core import (
    Budget,
    BudgetExceeded,
    Budgets,
    Reservation,
    budget_scope,
    current_scope,
    nano_to_usd,
    window_bounds,
)
from briefcase.budgets._hook import BudgetHook, GatewayTally, _tally
from briefcase.budgets.stores import (
    MemoryBudgetStore,
    RedisBudgetStore,
    SQLBudgetStore,
    store_from_url,
)
from briefcase.integrations.providers._base import add_call_hook, remove_call_hook

logger = get_logger("briefcase.budgets")

_lock = threading.Lock()
_engine: Optional[Budgets] = None
_hook: Optional[BudgetHook] = None


def _install(engine: Budgets) -> Budgets:
    global _engine, _hook
    with _lock:
        if _hook is not None:
            remove_call_hook(_hook)
        _engine = engine
        _hook = BudgetHook(engine)
        add_call_hook(_hook)
    _check_instrumented()
    return engine


def _check_instrumented() -> None:
    from briefcase.integrations.providers._base import active_providers

    if not active_providers():
        logger.warning("briefcase budgets: no provider SDK is instrumented yet, so no call is "
                       "counted until one is. Call briefcase.auto() (or briefcase.auto('openai')).")


def configure(budgets: Sequence[Budget] = (), *, store: Union[str, Any, None] = None,
              unpriced: str = "warn", default_max_tokens: int = 4096,
              token_counter: Optional[Callable[..., int]] = None,
              pricing: Any = None, clock: Any = None, on_error: str = "allow") -> Budgets:
    """Replace the active budgets (see :class:`Budgets` for the arguments;
    ``store`` may also be a URL). Returns the engine."""
    if isinstance(store, str):
        store = store_from_url(store)
    return _install(Budgets(budgets, store=store, unpriced=unpriced,
                            default_max_tokens=default_max_tokens, token_counter=token_counter,
                            pricing=pricing, clock=clock, on_error=on_error))


def budget(name: str, *, usd: Optional[float] = None, tokens: Optional[int] = None,
           soft_usd: Optional[float] = None, soft_tokens: Optional[int] = None,
           window: str = "day", per: Optional[str] = None, models: Sequence[str] = (),
           where: Optional[Dict[str, str]] = None, on_exceed: str = "block",
           degrade_to: Optional[str] = None, degrade: Optional[Callable[..., Any]] = None) -> Budget:
    """Add (or replace) one budget on the active engine, creating an engine
    with the in-process store if there is none. See :class:`Budget`."""
    item = Budget(name=name, usd=usd, tokens=tokens, soft_usd=soft_usd, soft_tokens=soft_tokens,
                  window=window, per=per, models=tuple(models), where=dict(where or {}),
                  on_exceed=on_exceed, degrade_to=degrade_to, degrade=degrade)
    engine = _engine if _engine is not None else _install(Budgets())
    engine.add(item)
    return item


def enable(path: Optional[Union[str, Path]] = None, *, store: Union[str, Any, None] = None,
           **options: Any) -> Budgets:
    """Load ``budgets.toml`` (``path``, else ``$BRIEFCASE_BUDGETS_FILE``,
    else ``~/.briefcase/budgets.toml``) and enforce it. ``store`` and the
    other :func:`configure` options override the file."""
    from briefcase.budgets import config

    data = config.read(path)
    settings: Dict[str, Any] = {k: data[k] for k in ("unpriced", "default_max_tokens", "on_error")
                                if k in data}
    settings.update(options)
    chosen = store if store is not None else data.get("store")
    engine = configure(config.budgets_from(data), store=chosen, **settings)
    if not engine.budgets:
        logger.warning("briefcase budgets: %s defines no budget; nothing is limited. "
                       "Add one: briefcase budget set NAME --usd 10",
                       path or config.default_path())
    return engine


def disable() -> None:
    """Stop enforcing budgets (counters in a shared store are kept)."""
    global _engine, _hook
    with _lock:
        if _hook is not None:
            remove_call_hook(_hook)
        _engine, _hook = None, None


def active() -> Optional[Budgets]:
    """The engine in use, or None."""
    return _engine


def status() -> List[Dict[str, Any]]:
    """Spend against each limit in the current window (see
    :meth:`Budgets.status`); empty when budgets are not enabled."""
    return _engine.status() if _engine is not None else []


class BudgetRequest:
    """What a :class:`briefcase.controls.Gateway` call will ask for, so the
    gateway can reserve before running it.

    Args:
        model: Model id the call will use.
        input_tokens: Input tokens (counted or estimated).
        max_tokens: The call's output cap (None: the model's maximum).
        scope: Extra scope values; the gateway adds ``tenant=<tenant_id>``.
        usage_of: Optional ``result -> {"prompt_tokens", "completion_tokens"}``
            for calls that do not go through an instrumented SDK.
    """

    def __init__(self, model: str, input_tokens: int, max_tokens: Optional[int] = None, *,
                 scope: Optional[Dict[str, str]] = None,
                 usage_of: Optional[Callable[[Any], Optional[Dict[str, int]]]] = None) -> None:
        self.model = model
        self.input_tokens = int(input_tokens)
        self.max_tokens = max_tokens
        self.scope = dict(scope or {})
        self.usage_of = usage_of


def gateway_model() -> Optional[str]:
    """Inside ``Gateway.invoke``: the model the budget decided on (the
    degraded model when a budget with ``on_exceed="degrade"`` fired)."""
    tally = _tally.get()
    return tally.model if tally is not None else None


@contextmanager
def gateway_scope(engine: Budgets, model: Optional[str] = None) -> Iterator[GatewayTally]:
    """Used by :class:`briefcase.controls.Gateway`: inside the block the
    provider hook does not reserve (the gateway already did) and adds each
    call's actual cost to the returned tally instead."""
    tally = GatewayTally(engine, model=model)
    token = _tally.set(tally)
    try:
        yield tally
    finally:
        _tally.reset(token)


__all__ = [
    "Budget",
    "BudgetExceeded",
    "BudgetRequest",
    "Budgets",
    "MemoryBudgetStore",
    "RedisBudgetStore",
    "Reservation",
    "SQLBudgetStore",
    "active",
    "budget",
    "budget_scope",
    "configure",
    "current_scope",
    "disable",
    "enable",
    "gateway_model",
    "gateway_scope",
    "nano_to_usd",
    "status",
    "store_from_url",
    "window_bounds",
]
