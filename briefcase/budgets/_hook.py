"""The call hook that enforces budgets on every instrumented provider call."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from briefcase._logging import get_logger
from briefcase.budgets._core import (
    BudgetExceeded, Budgets, Reservation, actual_cost, billable_tokens, nano_to_usd,
)
from briefcase.integrations.providers._requests import estimate_input_tokens

logger = get_logger("briefcase.budgets")

STATE = "budgets"


@dataclass
class GatewayTally:
    """Costs of the provider calls made inside one ``Gateway.invoke`` that
    holds its own budget reservation (the provider hook then does not
    reserve again; it reports actual cost here)."""

    engine: Budgets
    model: Optional[str] = None
    nano: int = 0
    tokens: int = 0
    calls: int = 0
    unknown: int = 0
    sources: List[str] = field(default_factory=list)


_tally: ContextVar[Optional[GatewayTally]] = ContextVar("briefcase_budget_gateway", default=None)


def input_tokens(engine: Budgets, ctx: Any) -> int:
    if engine.token_counter is not None:
        try:
            return int(engine.token_counter(ctx.provider, ctx.model, ctx.kwargs))
        except Exception:
            logger.warning("briefcase budgets: token_counter failed; using the estimate",
                           exc_info=True)
    return estimate_input_tokens(ctx.spec, ctx.kwargs)


class BudgetHook:
    """Reserve before the request, settle after the response (see
    :meth:`Budgets.reserve` and :meth:`Budgets.settle`)."""

    def __init__(self, engine: Budgets) -> None:
        self.engine = engine

    def before(self, ctx: Any) -> None:
        tally = _tally.get()
        if tally is not None:
            ctx.state[STATE] = tally
            return
        engine = self.engine
        if not engine.budgets:
            return
        model = ctx.model
        try:
            res = engine.reserve(model, input_tokens(engine, ctx), ctx.max_output_tokens())
        except BudgetExceeded as exc:
            fields = dict(exc.details.get("fields") or {})
            fields.update(budget_effect="block", cost_usd=0.0, cost_source="not_billed")
            ctx.fields.update(fields)
            raise
        except Exception as exc:
            ctx.fields.update(budget_effect="error", budget_error=type(exc).__name__)
            if engine.on_error == "block":
                ctx.fields.update(budget_effect="block", cost_usd=0.0, cost_source="not_billed")
                raise BudgetExceeded(
                    f"the budget store failed ({type(exc).__name__}: {exc}) and on_error='block'; "
                    "the request was not sent. Check: briefcase-ai budget status",
                    reason="store_error", model=model) from exc
            logger.warning("briefcase budgets: the budget store failed (%s: %s); the call goes "
                           "through uncounted (on_error='allow')", type(exc).__name__, exc)
            return
        if res.model and res.model != model:
            ctx.set_model(res.model)
        ctx.state[STATE] = res
        ctx.fields.update(res.fields())

    def after(self, ctx: Any, outcome: Any) -> None:
        state = ctx.state.get(STATE)
        if isinstance(state, GatewayTally):
            self._tally(ctx, state, outcome)
            return
        if not isinstance(state, Reservation):
            return
        fields = self.engine.settle(state, outcome.usage, model=outcome.model, sent=outcome.sent,
                                    http_status=outcome.http_status)
        ctx.fields.update(fields)

    def _tally(self, ctx: Any, tally: GatewayTally, outcome: Any) -> None:
        engine = tally.engine
        tally.calls += 1
        model = outcome.model or ctx.model
        entry = engine.price_entry(model)
        if entry is None and ctx.model:
            model, entry = ctx.model, engine.price_entry(ctx.model)
        priced = actual_cost(engine.pricing, entry, model or "", outcome.usage) \
            if entry is not None and outcome.usage else None
        fields: Dict[str, Any] = {"budget_effect": "gateway"}
        if priced is not None:
            tally.nano += priced[0]
            tally.sources.append(priced[1])
            fields.update(cost_usd=nano_to_usd(priced[0]), cost_source=priced[1])
        else:
            if outcome.sent:
                tally.unknown += 1
            fields.update(cost_usd=None, cost_source="unpriced" if entry is None else "reserved")
        billed = billable_tokens(outcome.usage) if outcome.usage else None
        if billed is not None:
            tally.tokens += billed[0] + billed[1]
        ctx.fields.update(fields)


__all__ = ["BudgetHook", "GatewayTally"]
