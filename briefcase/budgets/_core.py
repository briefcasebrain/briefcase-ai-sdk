"""Budget definitions, windows, cost arithmetic and the reserve / settle engine.

Money is counted in integer nano-dollars (1e-9 USD) everywhere below the
public API, rounded *up* at every step, so no sequence of calls can be
undercounted by float rounding.
"""

from __future__ import annotations

import fnmatch
import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple,
)

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import CallBlocked

logger = get_logger("briefcase.budgets")

NANO = 1_000_000_000
WINDOWS = ("hour", "day", "month", "total")
EFFECTS = ("block", "warn", "degrade")
UNPRICED_POLICIES = ("block", "allow", "warn")
#: Effects in record order of precedence (the record's ``budget_effect``).
_EFFECT_RANK = {"allow": 0, "soft_limit": 1, "unpriced": 2, "warn": 3, "degrade": 4, "block": 5}

DegradeFn = Callable[[str, "Budget", Dict[str, Any]], Optional[str]]


def usd_to_nano(usd: float) -> int:
    return int(math.ceil(round(usd * NANO, 3)))


def nano_to_usd(nano: int) -> float:
    return round(nano / NANO, 9)


# -- scope ----------------------------------------------------------------------

_scope: ContextVar[Tuple[Tuple[str, str], ...]] = ContextVar("briefcase_budget_scope", default=())


@contextmanager
def budget_scope(**values: Any) -> Iterator[Dict[str, str]]:
    """Attribute the provider calls made inside the block to a scope::

        with briefcase.budgets.budget_scope(tenant="acme", user="u-42"):
            client.chat.completions.create(...)

    Budgets with ``per="tenant"`` then count those calls against
    ``tenant=acme``. Nested scopes merge (inner values win). Uses a
    ``contextvars.ContextVar``, so it follows ``asyncio`` tasks and
    ``asyncio.to_thread``; a thread started by hand does not inherit it."""
    merged = dict(_scope.get())
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[str(key)] = str(value)
    token = _scope.set(tuple(sorted(merged.items())))
    try:
        yield dict(merged)
    finally:
        _scope.reset(token)


def current_scope() -> Dict[str, str]:
    """The scope set by the innermost :func:`budget_scope`."""
    return dict(_scope.get())


# -- windows ----------------------------------------------------------------------

def window_bounds(window: str, now: datetime) -> Tuple[str, Optional[datetime]]:
    """``(window id, reset time)`` of the calendar window holding ``now``
    (UTC). ``total`` never resets."""
    now = now.astimezone(timezone.utc)
    if window == "hour":
        start = now.replace(minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%dT%H"), datetime.fromtimestamp(
            start.timestamp() + 3600, timezone.utc)
    if window == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%d"), datetime.fromtimestamp(
            start.timestamp() + 86400, timezone.utc)
    if window == "month":
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return now.strftime("%Y-%m"), datetime(year, month, 1, tzinfo=timezone.utc)
    if window == "total":
        return "total", None
    raise ValueError(f"window must be one of {WINDOWS}, got {window!r}")


# -- budgets ----------------------------------------------------------------------

@dataclass
class Budget:
    """One spending limit.

    Args:
        name: Unique name, used in keys, records and the CLI.
        usd: Hard limit in USD per window (None: no USD limit).
        tokens: Hard limit in tokens (input + output) per window.
        soft_usd, soft_tokens: Soft limits: crossing one logs a warning and
            marks the record ``budget_effect="soft_limit"``; the call goes on.
        window: ``"hour"``, ``"day"``, ``"month"`` (calendar windows in UTC)
            or ``"total"`` (never resets).
        per: None (one counter), ``"model"`` (one counter per model) or a
            scope key such as ``"tenant"``, ``"user"``, ``"session"``,
            ``"key"``, set with :func:`budget_scope`. Calls with no value for
            that key share the counter ``<key>=-``.
        models: Only count calls to these models (``fnmatch`` patterns
            against the requested model id). Empty: every model.
        where: Only count calls whose scope has these values, e.g.
            ``{"tenant": "acme"}``.
        on_exceed: What happens when a call would cross the hard limit:
            ``"block"`` raises :class:`BudgetExceeded` before the request is
            sent; ``"warn"`` lets it through, logs and records it;
            ``"degrade"`` switches the call to ``degrade_to`` (or what
            ``degrade(model, budget, info)`` returns) and blocks if that
            model does not fit either.
    """

    name: str
    usd: Optional[float] = None
    tokens: Optional[int] = None
    soft_usd: Optional[float] = None
    soft_tokens: Optional[int] = None
    window: str = "day"
    per: Optional[str] = None
    models: Sequence[str] = ()
    where: Mapping[str, str] = field(default_factory=dict)
    on_exceed: str = "block"
    degrade_to: Optional[str] = None
    degrade: Optional[DegradeFn] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or any(
                c in self.name for c in ":| \t\n"):
            raise ValueError(f"budget name must be a non-empty word without ':', '|' or spaces, "
                             f"got {self.name!r}")
        if self.window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}, got {self.window!r}")
        if self.on_exceed not in EFFECTS:
            raise ValueError(f"on_exceed must be one of {EFFECTS}, got {self.on_exceed!r}")
        for label, value in (("usd", self.usd), ("tokens", self.tokens),
                             ("soft_usd", self.soft_usd), ("soft_tokens", self.soft_tokens)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or value < 0 or not math.isfinite(value)):
                raise ValueError(f"{label} must be a non-negative number, got {value!r}")
        if self.usd is None and self.tokens is None and self.soft_usd is None \
                and self.soft_tokens is None:
            raise ValueError(f"budget {self.name!r} sets no limit: give usd, tokens, soft_usd "
                             "or soft_tokens")
        if self.on_exceed == "degrade" and not self.degrade_to and self.degrade is None:
            raise ValueError(f"budget {self.name!r}: on_exceed='degrade' needs degrade_to=MODEL "
                             "or degrade=callable")
        if isinstance(self.models, str):
            self.models = (self.models,)
        self.models = tuple(self.models)
        self.where = {str(k): str(v) for k, v in dict(self.where).items()}

    @property
    def limit_nano(self) -> Optional[int]:
        return None if self.usd is None else usd_to_nano(self.usd)

    @property
    def soft_nano(self) -> Optional[int]:
        return None if self.soft_usd is None else usd_to_nano(self.soft_usd)

    def applies(self, model: Optional[str], scope: Mapping[str, str]) -> bool:
        if self.models and not (model and any(fnmatch.fnmatchcase(model, p) for p in self.models)):
            return False
        return all(scope.get(k) == v for k, v in self.where.items())

    def scope_label(self, model: Optional[str], scope: Mapping[str, str]) -> str:
        if self.per is None:
            return "*"
        if self.per == "model":
            return f"model={model or '-'}"
        return f"{self.per}={scope.get(self.per) or '-'}"

    def key(self, model: Optional[str], scope: Mapping[str, str],
            now: datetime) -> Tuple[str, str, str, Optional[datetime]]:
        """``(store key, scope label, window id, reset time)``."""
        label = self.scope_label(model, scope)
        window_id, reset = window_bounds(self.window, now)
        return f"{self.name}|{label}|{window_id}", label, window_id, reset

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name, "usd": self.usd, "tokens": self.tokens,
            "soft_usd": self.soft_usd, "soft_tokens": self.soft_tokens,
            "window": self.window, "per": self.per, "models": list(self.models),
            "where": dict(self.where), "on_exceed": self.on_exceed,
            "degrade_to": self.degrade_to,
            "degrade": getattr(self.degrade, "__name__", None) if self.degrade else None,
        }


class BudgetExceeded(CallBlocked):
    """A call was stopped before it was sent because it would cross a hard
    budget limit (or its model has no price and ``unpriced="block"``).

    Attributes: ``budget`` (name), ``scope`` (e.g. ``"tenant=acme"``),
    ``window`` (id), ``reason`` (``"limit"`` or ``"unpriced"``), ``model``,
    ``spent_usd``, ``limit_usd``, ``requested_usd``, ``spent_tokens``,
    ``limit_tokens``, ``requested_tokens`` and ``resets_at`` (datetime or
    None)."""

    record_effect = "block"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details
        for key in ("budget", "scope", "window", "reason", "model", "spent_usd", "limit_usd",
                    "requested_usd", "spent_tokens", "limit_tokens", "requested_tokens",
                    "resets_at"):
            setattr(self, key, details.get(key))


# -- prices ------------------------------------------------------------------------

def _rates(entry: Mapping[str, Any]) -> Tuple[float, float]:
    """Highest input / output price per token for an entry, over the base
    rate and every long-context tier (so a reservation never undercounts)."""
    inp = float(entry["input_cost_per_1k_tokens"]) / 1000.0
    out = float(entry["output_cost_per_1k_tokens"]) / 1000.0
    extras = entry.get("rate_card_extras") or {}
    for tier in extras.get("long_context_tiers") or ():
        inp = max(inp, float(tier.get("input_per_mtok") or 0) / 1e6)
        out = max(out, float(tier.get("output_per_mtok") or 0) / 1e6)
    return inp, out


def _cache_write_rate(entry: Mapping[str, Any], input_rate: float) -> float:
    cache = (entry.get("rate_card_extras") or {}).get("cache_rates") or {}
    rates = [float(cache[k]) / 1e6 for k in ("write_5m_per_mtok", "write_1h_per_mtok")
             if isinstance(cache.get(k), (int, float))]
    return max(rates + [2.0 * input_rate])


def reserve_cost(entry: Mapping[str, Any], input_tokens: int, output_tokens: int) -> int:
    inp, out = _rates(entry)
    return int(math.ceil((input_tokens * inp + output_tokens * out) * NANO))


#: Usage counters billed as input in addition to ``prompt_tokens``.
_EXTRA_INPUT = ("cache_read_input_tokens", "cache_creation_input_tokens",
                "cache_write_input_tokens", "tool_use_prompt_token_count")
#: Cache writes, priced at the cache-write rate. The first two (Anthropic,
#: Bedrock) are billed on top of ``prompt_tokens`` (see ``_EXTRA_INPUT``);
#: OpenAI's ``cache_write_tokens`` is a part of ``prompt_tokens``.
_CACHE_WRITE = ("cache_creation_input_tokens", "cache_write_input_tokens", "cache_write_tokens")
#: Usage counters billed as output in addition to ``completion_tokens``.
_EXTRA_OUTPUT = ("thoughts_token_count",)
#: Discounted subsets of ``prompt_tokens`` (priced at the full input rate here).
_DISCOUNTED = ("cached_tokens", "cached_content_token_count")


def billable_tokens(usage: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    """``(input, output)`` tokens to bill from a record's ``token_usage``;
    None when either side was not reported."""
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    inp = prompt + sum(int(usage.get(k) or 0) for k in _EXTRA_INPUT)
    out = completion + sum(int(usage.get(k) or 0) for k in _EXTRA_OUTPUT)
    return inp, out


def actual_cost(table: Any, entry: Mapping[str, Any], model: str,
                usage: Mapping[str, Any]) -> Optional[Tuple[int, str]]:
    """``(nano-USD, cost_source)`` from reported usage, or None when the usage
    is incomplete.

    ``usage``: flat per-token pricing through :meth:`PricingTable.estimate`
    (the same arithmetic as the native engine). ``usage_upper_bound``: the
    call used prompt caching or crossed a long-context tier, which the
    pure-Python estimator does not price; cache reads are then charged at the
    full input rate, cache writes at the highest write rate (or twice the
    input rate), and every token at the highest tier rate. Never less than
    the real charge for the shipped price table."""
    tokens = billable_tokens(usage)
    if tokens is None:
        return None
    inp, out = tokens
    special = any(int(usage.get(k) or 0) for k in _EXTRA_INPUT + _DISCOUNTED + _CACHE_WRITE)
    if not special:
        try:
            result = table.estimate(model, inp, out)
            return int(math.ceil(result["total_cost"] * NANO)), "usage"
        except Exception:
            pass  # tiers, context-window checks: fall through to the bound
    in_rate, out_rate = _rates(entry)
    writes = sum(int(usage.get(k) or 0) for k in _CACHE_WRITE)
    cost = (inp - writes) * in_rate + writes * _cache_write_rate(entry, in_rate) + out * out_rate
    return int(math.ceil(cost * NANO)), "usage_upper_bound"


# -- reservations ----------------------------------------------------------------------

@dataclass
class Hold:
    """One budget counter a call holds a reservation on."""

    budget: Budget
    key: str
    scope: str
    window: str
    resets_at: Optional[datetime]
    nano: int
    tokens: int
    used_nano: int = 0
    used_tokens: int = 0
    effect: str = "allow"


@dataclass
class Reservation:
    """What one call reserved; settle it exactly once."""

    model: Optional[str]
    requested_model: Optional[str]
    scope: Dict[str, str]
    input_tokens: int
    max_output_tokens: int
    nano: int
    tokens: int
    priced: bool
    holds: List[Hold] = field(default_factory=list)
    effect: str = "allow"
    reasons: List[str] = field(default_factory=list)
    settled: bool = False

    def fields(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "budget_effect": self.effect,
            "budget_scope": dict(self.scope),
            "cost_reserved_usd": nano_to_usd(self.nano) if self.priced else None,
            "budget_checks": [{
                "budget": h.budget.name, "scope": h.scope, "window": h.window,
                "effect": h.effect,
                "used_usd": nano_to_usd(h.used_nano), "limit_usd": h.budget.usd,
                "used_tokens": h.used_tokens, "limit_tokens": h.budget.tokens,
            } for h in self.holds],
        }
        if self.requested_model and self.requested_model != self.model:
            out["budget_degraded_from"] = self.requested_model
        return out


def _rank(*effects: str) -> str:
    return max(effects, key=lambda e: _EFFECT_RANK.get(e, 0))


class Budgets:
    """A set of budgets over one store.

    Args:
        budgets: :class:`Budget` objects.
        store: Where counters live. Default an in-process
            :class:`~briefcase.budgets.stores.MemoryBudgetStore`.
        unpriced: A call to a model with no price: ``"warn"`` (default; one
            log line per model naming ``briefcase-ai pricing set``; token limits
            still apply, USD limits cannot), ``"allow"`` (the same, silent) or
            ``"block"`` (raise :class:`BudgetExceeded`).
        default_max_tokens: Output tokens reserved when the request sets no
            cap and the price table has no ``max_output_tokens`` for the model.
        token_counter: Optional ``(provider, model, kwargs) -> int`` that
            counts input tokens exactly; default a high estimate (UTF-8
            bytes / 3).
        pricing: A :class:`briefcase.pricing.PricingTable`; default
            ``briefcase.pricing.load()`` on first use.
        clock: ``() -> datetime`` (UTC), for tests.
        on_error: When the store fails (Redis or the database is down):
            ``"allow"`` (default) lets the call through uncounted, with a
            WARNING and ``budget_effect: "error"`` in the record; ``"block"``
            raises :class:`BudgetExceeded` (``reason="store_error"``).
    """

    def __init__(self, budgets: Sequence[Budget] = (), *, store: Any = None,
                 unpriced: str = "warn", default_max_tokens: int = 4096,
                 token_counter: Optional[Callable[[str, Optional[str], Dict[str, Any]], int]] = None,
                 pricing: Any = None, clock: Optional[Callable[[], datetime]] = None,
                 on_error: str = "allow") -> None:
        if on_error not in ("allow", "block"):
            raise ValueError(f"on_error must be 'allow' or 'block', got {on_error!r}")
        self.on_error = on_error
        if unpriced not in UNPRICED_POLICIES:
            raise ValueError(f"unpriced must be one of {UNPRICED_POLICIES}, got {unpriced!r}")
        from briefcase.budgets.stores import MemoryBudgetStore

        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Counters age by the engine's clock: a store on the wall clock drops a window the injected clock still calls
        # current, and spend reads back as zero.
        if store is None:
            store = MemoryBudgetStore(clock=lambda: self.clock().timestamp())
        self.store = store
        self.unpriced = unpriced
        self.default_max_tokens = int(default_max_tokens)
        self.token_counter = token_counter
        self._pricing = pricing
        self._budgets: Dict[str, Budget] = {}
        self._lock = threading.Lock()
        self._warned_unpriced: set = set()
        self._last_log: Dict[str, float] = {}
        for item in budgets:
            self.add(item)

    # -- configuration -----------------------------------------------------------

    @property
    def budgets(self) -> List[Budget]:
        return list(self._budgets.values())

    def add(self, item: Budget) -> Budget:
        with self._lock:
            self._budgets[item.name] = item
        return item

    def remove(self, name: str) -> bool:
        with self._lock:
            return self._budgets.pop(name, None) is not None

    @property
    def pricing(self) -> Any:
        if self._pricing is None:
            from briefcase import pricing

            self._pricing = pricing.load()
        return self._pricing

    def price_entry(self, model: Optional[str]) -> Optional[Dict[str, Any]]:
        if not model:
            return None
        try:
            resolved = self.pricing.resolve(model)
        except Exception:
            logger.warning("briefcase budgets: the price table could not be loaded; "
                           "treating %s as unpriced", model, exc_info=True)
            return None
        return resolved[0] if resolved else None

    def max_output_for(self, model: Optional[str], requested: Optional[int]) -> int:
        if requested is not None:
            return requested
        entry = self.price_entry(model)
        cap = entry.get("max_output_tokens") if entry else None
        return int(cap) if isinstance(cap, int) and cap > 0 else self.default_max_tokens

    # -- the decision -------------------------------------------------------------

    def _log(self, key: str, level: int, message: str, *args: Any) -> None:
        """Log at most once a minute per key, so a warn budget cannot flood."""
        now = time.monotonic()
        if now - self._last_log.get(key, -1e9) < 60:
            return
        self._last_log[key] = now
        logger.log(level, message, *args)

    def _unpriced(self, model: Optional[str]) -> None:
        if self.unpriced == "warn" and model not in self._warned_unpriced:
            self._warned_unpriced.add(model)
            logger.warning(
                "briefcase budgets: model %r has no price, so USD limits cannot count it "
                "(token limits still do). Add one: briefcase-ai pricing set %s --input USD_PER_MTOK "
                "--output USD_PER_MTOK --context-window N --provider NAME --source-url URL",
                model, model or "MODEL")

    def reserve(self, model: Optional[str], input_tokens: int,
                max_output_tokens: Optional[int], *, scope: Optional[Mapping[str, str]] = None,
                allow_degrade: bool = True, dry_run: bool = False) -> Reservation:
        """Reserve the most this call can cost against every budget that
        applies. Raises :class:`BudgetExceeded` for a ``block``; for
        ``degrade`` the returned reservation's ``model`` is the model to call.
        With ``dry_run`` nothing is written and nothing raises: the
        reservation says what would happen (``effect``, ``reasons``)."""
        scope = dict(current_scope() if scope is None else scope)
        requested_model = model
        result = self._reserve(model, input_tokens, max_output_tokens, scope, dry_run)
        if result.effect != "degrade_needed":
            return result
        # Degrade: ask the budget that fired for a cheaper model, once.
        hold = next(h for h in result.holds if h.effect == "degrade_needed")
        new_model: Optional[str] = None
        if allow_degrade:
            info = {"scope": scope, "used_usd": nano_to_usd(hold.used_nano),
                    "limit_usd": hold.budget.usd, "window": hold.window}
            try:
                new_model = (hold.budget.degrade(model or "", hold.budget, info)
                             if hold.budget.degrade is not None else hold.budget.degrade_to)
            except Exception:
                logger.warning("briefcase budgets: degrade callable of %r failed; blocking",
                               hold.budget.name, exc_info=True)
                new_model = None
        if new_model and new_model != model:
            retry = self._reserve(new_model, input_tokens, max_output_tokens, scope, dry_run,
                                  degraded=True)
            if retry.effect not in ("degrade_needed", "block"):
                retry.requested_model = requested_model
                retry.effect = _rank(retry.effect, "degrade")
                retry.reasons.insert(0, f"budget {hold.budget.name!r} ({hold.scope}) is spent; "
                                        f"switched {model} -> {new_model}")
                for h in retry.holds:
                    if h.budget.name == hold.budget.name:
                        h.effect = "degrade"
                self._log(f"degrade:{hold.key}", 30,
                          "briefcase budgets: %s", retry.reasons[0])
                return retry
            result = retry
            hold = next((h for h in retry.holds if h.effect in ("degrade_needed", "block")), hold)
        return self._block(result, hold, requested_model, dry_run)

    def _block(self, result: Reservation, hold: Hold, model: Optional[str],
               dry_run: bool) -> Reservation:
        hold.effect = "block"
        result.effect = "block"
        budget = hold.budget
        reset = hold.resets_at.strftime("%Y-%m-%d %H:%M UTC") if hold.resets_at else "never"
        if budget.usd is not None and hold.used_nano + result.nano > (budget.limit_nano or 0):
            what = (f"${nano_to_usd(hold.used_nano):.6f} spent + ${nano_to_usd(result.nano):.6f} "
                    f"reserved for this call > ${budget.usd:g} limit")
        else:
            what = (f"{hold.used_tokens} tokens used + {result.tokens} reserved for this call "
                    f"> {budget.tokens} token limit")
        message = (f"budget {budget.name!r} ({hold.scope}, window {hold.window}) would be "
                   f"exceeded: {what}; resets {reset}. The request was not sent. Check: "
                   f"briefcase-ai budget status")
        result.reasons.append(message)
        if dry_run:
            return result
        raise BudgetExceeded(
            message, budget=budget.name, scope=hold.scope, window=hold.window, reason="limit",
            model=model, spent_usd=nano_to_usd(hold.used_nano), limit_usd=budget.usd,
            requested_usd=nano_to_usd(result.nano), spent_tokens=hold.used_tokens,
            limit_tokens=budget.tokens, requested_tokens=result.tokens, resets_at=hold.resets_at,
            fields=result.fields())

    def _reserve(self, model: Optional[str], input_tokens: int, max_output: Optional[int],
                 scope: Dict[str, str], dry_run: bool, degraded: bool = False) -> Reservation:
        out_tokens = self.max_output_for(model, max_output)
        entry = self.price_entry(model)
        priced = entry is not None
        nano = reserve_cost(entry, input_tokens, out_tokens) if entry else 0
        tokens = input_tokens + out_tokens
        res = Reservation(model, model, scope, input_tokens, out_tokens, nano, tokens, priced)
        applicable = [b for b in self.budgets if b.applies(model, scope)]
        if not applicable:
            return res
        if not priced:
            if any(b.usd is not None or b.soft_usd is not None for b in applicable):
                if self.unpriced == "block":
                    message = (f"model {model!r} has no price, and the budget policy blocks "
                               f"unpriced models (unpriced='block'). The request was not sent. "
                               f"Add a price: briefcase-ai pricing set {model or 'MODEL'} ...")
                    if dry_run:
                        res.effect = "block"
                        res.reasons.append(message)
                        return res
                    raise BudgetExceeded(message, reason="unpriced", model=model,
                                         budget=applicable[0].name, fields=dict(
                                             res.fields(), budget_effect="block",
                                             cost_source="unpriced"))
                self._unpriced(model)
                res.effect = "unpriced"
                res.reasons.append(f"model {model!r} has no price; USD limits do not count it")
        now = self.clock()
        for budget in applicable:
            key, label, window_id, reset = budget.key(model, scope, now)
            limit_nano = budget.limit_nano if priced else None
            hold = Hold(budget, key, label, window_id, reset, nano if priced else 0, tokens)
            ok, used_n, used_t = self._try(hold, limit_nano, budget.tokens, dry_run, force=False)
            if not ok and budget.on_exceed == "warn":
                ok, used_n, used_t = self._try(hold, None, None, dry_run, force=True)
                hold.effect = "warn"
                res.reasons.append(f"budget {budget.name!r} ({label}) is over its hard limit; "
                                   "on_exceed='warn' lets the call through")
                self._log(f"warn:{key}", 30, "briefcase budgets: budget %r (%s, window %s) is "
                          "over its hard limit; on_exceed='warn' let a call through",
                          budget.name, label, window_id)
            hold.used_nano, hold.used_tokens = used_n, used_t
            res.holds.append(hold)
            if not ok:
                self._release_holds(res, dry_run)
                if budget.on_exceed == "degrade" and not degraded:
                    hold.effect = "degrade_needed"
                    res.effect = "degrade_needed"
                    return res
                return self._block(res, hold, model, dry_run)
            if hold.effect == "allow" and (
                    (priced and budget.soft_nano is not None and used_n > budget.soft_nano)
                    or (budget.soft_tokens is not None and used_t > budget.soft_tokens)):
                hold.effect = "soft_limit"
                res.reasons.append(f"budget {budget.name!r} ({label}) passed its soft limit")
                self._log(f"soft:{key}", 30, "briefcase budgets: budget %r (%s, window %s) "
                          "passed its soft limit: $%.6f / %s tokens used", budget.name, label,
                          window_id, nano_to_usd(used_n), used_t)
            res.effect = _rank(res.effect, hold.effect)
        return res

    def _try(self, hold: Hold, limit_nano: Optional[int], limit_tokens: Optional[int],
             dry_run: bool, force: bool) -> Tuple[bool, int, int]:
        """Reserve one hold. ``(ok, used nano, used tokens)``: the counter
        after the reservation, or as it stands when it was refused."""
        if dry_run:
            used_n, used_t = self.store.get(hold.key)[:2]
            ok = force or ((limit_nano is None or used_n + hold.nano <= limit_nano)
                           and (limit_tokens is None or used_t + hold.tokens <= limit_tokens))
            return (ok, used_n + hold.nano, used_t + hold.tokens) if ok else (False, used_n, used_t)
        return self.store.reserve(hold.key, hold.nano, hold.tokens, limit_nano, limit_tokens,
                                  _expiry(hold.resets_at), force=force)

    def _release_holds(self, res: Reservation, dry_run: bool) -> None:
        """Undo the holds taken before a counter refused (all or nothing)."""
        if dry_run:
            return
        for hold in res.holds[:-1]:
            try:
                self.store.adjust(hold.key, -hold.nano, -hold.tokens, -hold.nano, -hold.tokens,
                                  _expiry(hold.resets_at))
            except Exception:
                logger.warning("briefcase budgets: could not release %s", hold.key,
                               exc_info=True)

    def settle(self, res: Reservation, usage: Optional[Mapping[str, Any]], *,
               model: Optional[str] = None, sent: bool = True,
               http_status: Optional[int] = None) -> Dict[str, Any]:
        """Replace the reservation by the actual cost; returns the record
        fields. ``usage`` is the record's ``token_usage``.

        - usage known: the cost from usage (``cost_source`` ``usage`` or
          ``usage_upper_bound``); the difference to the reservation is
          released (or, if the call cost more, added).
        - not sent, or the provider answered 4xx: nothing was billed; the
          reservation is released (``not_billed``).
        - otherwise (usage unknown: a stream without usage, an abandoned
          stream, a timeout): the reservation stays spent (``reserved``).
        """
        fields = res.fields()
        if res.settled:
            return fields
        res.settled = True
        served = model or res.model
        cost: Optional[int] = None
        tokens: Optional[int] = None
        if not sent or (http_status is not None and 400 <= http_status < 500 and not usage):
            cost, tokens, source = 0, 0, "not_billed"
        else:
            billed = billable_tokens(usage) if usage else None
            if billed is not None:
                tokens = billed[0] + billed[1]
            price_model = served
            entry = self.price_entry(served)
            if entry is None and res.priced:
                # The served id (a dated snapshot, a provider alias) has no
                # price of its own: price it as the model that was requested.
                price_model, entry = res.model, self.price_entry(res.model)
            priced = None
            if usage and entry is not None:
                priced = actual_cost(self.pricing, entry, price_model or "", usage)
            if priced is not None:
                cost, source = priced
            elif not res.priced and billed is not None:
                cost, source = None, "unpriced"
            else:
                cost, source = res.nano, "reserved"
                tokens = res.tokens if tokens is None else tokens
        return self._apply(res, fields, cost, tokens, source)

    def settle_amount(self, res: Reservation, usd: Optional[float], tokens: Optional[int],
                      source: str) -> Dict[str, Any]:
        """Settle to a known cost (used by the controls gateway, which adds
        up the provider calls made inside one invocation)."""
        fields = res.fields()
        if res.settled:
            return fields
        res.settled = True
        cost = None if usd is None else int(math.ceil(round(usd * NANO, 3)))
        return self._apply(res, fields, cost, tokens, source)

    def _apply(self, res: Reservation, fields: Dict[str, Any], cost: Optional[int],
               tokens: Optional[int], source: str) -> Dict[str, Any]:
        delta_n = (cost if cost is not None else 0) - (res.nano if res.priced else 0)
        delta_t = (tokens if tokens is not None else res.tokens) - res.tokens
        for hold in res.holds:
            try:
                self.store.adjust(hold.key, delta_n if res.priced else 0, delta_t,
                                  -hold.nano, -hold.tokens, _expiry(hold.resets_at))
            except Exception:
                logger.warning("briefcase budgets: could not settle %s; the reservation stays "
                               "counted", hold.key, exc_info=True)
        fields["cost_usd"] = nano_to_usd(cost) if cost is not None else None
        fields["cost_source"] = source
        return fields

    # -- reading -------------------------------------------------------------------

    def status(self) -> List[Dict[str, Any]]:
        """One row per budget counter in its current window: spend, limit,
        percentage, reservations in flight and time left."""
        now = self.clock()
        rows: List[Dict[str, Any]] = []
        for budget in self.budgets:
            window_id, reset = window_bounds(budget.window, now)
            found = [(k, v) for k, v in self.store.scan(f"{budget.name}|")
                     if k.endswith(f"|{window_id}")]
            if not found:
                label = "*" if budget.per is None else f"{budget.per}=(no calls yet)"
                found = [(f"{budget.name}|{label}|{window_id}", (0, 0, 0, 0))]
            for key, (used_n, used_t, inflight_n, inflight_t) in found:
                scope_label = key.split("|")[1]
                pct = []
                if budget.usd:
                    pct.append(used_n / max(1, budget.limit_nano or 1) * 100)
                if budget.tokens:
                    pct.append(used_t / budget.tokens * 100)
                rows.append({
                    "budget": budget.name, "scope": scope_label, "window": budget.window,
                    "window_id": window_id, "used_usd": nano_to_usd(used_n),
                    "limit_usd": budget.usd, "soft_usd": budget.soft_usd,
                    "used_tokens": used_t, "limit_tokens": budget.tokens,
                    "inflight_usd": nano_to_usd(inflight_n), "inflight_tokens": inflight_t,
                    "percent": round(max(pct), 1) if pct else None,
                    "resets_at": reset.isoformat() if reset else None,
                    "seconds_left": int((reset - now).total_seconds()) if reset else None,
                    "on_exceed": budget.on_exceed,
                })
        return rows


def _expiry(reset: Optional[datetime]) -> Optional[int]:
    """Counters expire a day after their window ends (kept for status)."""
    return int(reset.timestamp()) + 86400 if reset is not None else None


__all__ = [
    "Budget", "BudgetExceeded", "Budgets", "EFFECTS", "Hold", "NANO", "Reservation",
    "UNPRICED_POLICIES", "WINDOWS", "actual_cost", "billable_tokens", "budget_scope",
    "current_scope", "nano_to_usd", "reserve_cost", "usd_to_nano", "window_bounds",
]
