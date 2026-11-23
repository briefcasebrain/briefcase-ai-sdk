"""Budget arithmetic: reservation, settle and release, unknown usage, unpriced
models, windows and rollover, effects (block / warn / degrade / soft limit),
scopes, and concurrency on the in-process store."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone

import pytest

from briefcase import pricing
from briefcase.budgets import (
    Budget, BudgetExceeded, Budgets, budget_scope, current_scope, window_bounds,
)
from briefcase.budgets._core import actual_cost, nano_to_usd, reserve_cost, usd_to_nano
from briefcase.budgets.stores import MemoryBudgetStore

# gpt-4o in the packaged table: $5 / $15 per million tokens.
IN, OUT = 5e-6, 15e-6


class Clock:
    def __init__(self, when):
        self.now = when

    def __call__(self):
        return self.now


@pytest.fixture
def table():
    return pricing.load(environ={"BRIEFCASE_HOME": "/nonexistent-briefcase-home"})


def engine(table, *budgets, **kw):
    kw.setdefault("clock", Clock(datetime(2026, 11, 22, 10, 30, tzinfo=timezone.utc)))
    return Budgets(budgets, pricing=table, **kw)


def used(eng, name="daily"):
    rows = [r for r in eng.status() if r["budget"] == name]
    return rows[0]["used_usd"], rows[0]["used_tokens"], rows[0]["inflight_usd"]


# -- arithmetic ----------------------------------------------------------------------------

def test_reservation_is_input_plus_max_output_at_list_price(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 1000, 500)
    assert res.nano == usd_to_nano(1000 * IN + 500 * OUT)
    assert res.tokens == 1500
    assert used(eng) == (pytest.approx(0.0125), 1500, pytest.approx(0.0125))


def test_settle_replaces_the_reservation_with_actual_cost(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 1000, 500)
    fields = eng.settle(res, {"prompt_tokens": 900, "completion_tokens": 100})
    assert fields["cost_source"] == "usage"
    assert fields["cost_usd"] == pytest.approx(900 * IN + 100 * OUT)
    spent, tokens, inflight = used(eng)
    assert spent == pytest.approx(900 * IN + 100 * OUT)
    assert tokens == 1000
    assert inflight == 0  # the difference was released


def test_settle_can_exceed_the_reservation_and_is_counted(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 10, 10)
    eng.settle(res, {"prompt_tokens": 1000, "completion_tokens": 10})
    assert used(eng)[0] == pytest.approx(1000 * IN + 10 * OUT)


def test_settle_is_idempotent(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 100, 100)
    eng.settle(res, {"prompt_tokens": 1, "completion_tokens": 1})
    eng.settle(res, {"prompt_tokens": 50, "completion_tokens": 50})
    assert used(eng)[1] == 2


def test_unknown_usage_keeps_the_reservation(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 1000, 500)
    fields = eng.settle(res, None)
    assert fields["cost_source"] == "reserved"
    assert fields["cost_usd"] == pytest.approx(0.0125)
    assert used(eng) == (pytest.approx(0.0125), 1500, 0)


def test_half_reported_usage_is_unknown(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 1000, 500)
    assert eng.settle(res, {"prompt_tokens": 900, "completion_tokens": None})["cost_source"] \
        == "reserved"


@pytest.mark.parametrize("kwargs", [{"sent": False}, {"http_status": 429}, {"http_status": 400}])
def test_not_sent_or_4xx_is_released(table, kwargs):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 1000, 500)
    fields = eng.settle(res, None, **kwargs)
    assert fields["cost_source"] == "not_billed"
    assert fields["cost_usd"] == 0
    assert used(eng) == (0, 0, 0)


def test_5xx_without_usage_keeps_the_reservation(table):
    eng = engine(table, Budget("daily", usd=10))
    res = eng.reserve("gpt-4o", 1000, 500)
    assert eng.settle(res, None, http_status=503)["cost_source"] == "reserved"


def test_nano_rounding_never_undercounts():
    entry = {"input_cost_per_1k_tokens": 0.00015, "output_cost_per_1k_tokens": 0.0006}
    # 1 token at $0.15/M = 150 nano exactly; 1 output token = 600 nano.
    assert reserve_cost(entry, 1, 1) == 750
    tiny = {"input_cost_per_1k_tokens": 0.0000001, "output_cost_per_1k_tokens": 0.0}
    assert reserve_cost(tiny, 1, 0) == 1  # 0.1 nano rounds up, not down
    assert nano_to_usd(usd_to_nano(0.1)) == 0.1


def test_cache_tokens_are_priced_as_an_upper_bound(table):
    entry = table.resolve("claude-sonnet-4-5")[0]
    usage = {"prompt_tokens": 100, "completion_tokens": 50,
             "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}
    nano, source = actual_cost(table, entry, "claude-sonnet-4-5", usage)
    assert source == "usage_upper_bound"
    inp = entry["input_cost_per_1k_tokens"] / 1000
    out = entry["output_cost_per_1k_tokens"] / 1000
    write = 6.0 / 1e6  # write_1h_per_mtok, the highest write rate
    exact_floor = (100 * inp + 1000 * 0.3e-6 + 200 * 3.75e-6 + 50 * out) * 1e9
    assert nano >= exact_floor
    assert nano == pytest.approx((1100 * inp + 200 * write + 50 * out) * 1e9, abs=2)


def test_openai_cached_subset_is_not_double_counted(table):
    entry = table.resolve("gpt-4o")[0]
    nano, source = actual_cost(table, entry, "gpt-4o",
                               {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 60})
    assert source == "usage_upper_bound"
    assert nano == pytest.approx((100 * IN + 10 * OUT) * 1e9, abs=2)


def test_openai_cache_writes_are_part_of_the_prompt_and_priced_as_writes(table):
    # OpenAI reports cache_write_tokens inside prompt_tokens (the published
    # OpenAPI spec, tests/contracts): never billed twice, never at less
    # than the cache-write rate.
    entry = table.resolve("gpt-4o")[0]
    nano, source = actual_cost(table, entry, "gpt-4o",
                               {"prompt_tokens": 100, "completion_tokens": 10, "cache_write_tokens": 40})
    assert source == "usage_upper_bound"
    assert nano == pytest.approx((60 * IN + 40 * 2 * IN + 10 * OUT) * 1e9, abs=2)


def test_long_context_tier_reserves_at_the_tier_price(table):
    entry = table.resolve("gemini-2.5-pro")[0]
    tier = entry["rate_card_extras"]["long_context_tiers"][0]
    assert reserve_cost(entry, 1000, 0) == pytest.approx(1000 * tier["input_per_mtok"] * 1e3, abs=1)


# -- effects ---------------------------------------------------------------------------------

def test_block_raises_before_anything_is_counted(table):
    eng = engine(table, Budget("daily", usd=0.01))
    with pytest.raises(BudgetExceeded) as info:
        eng.reserve("gpt-4o", 1000, 1000)  # $0.02
    exc = info.value
    assert exc.budget == "daily" and exc.reason == "limit" and exc.limit_usd == 0.01
    assert exc.requested_usd == pytest.approx(0.02)
    assert "resets 2026-11-23 00:00 UTC" in str(exc)
    assert "briefcase budget status" in str(exc)
    assert used(eng) == (0, 0, 0)


def test_hard_limit_is_exact(table):
    eng = engine(table, Budget("daily", usd=0.02))
    res = eng.reserve("gpt-4o", 1000, 1000)          # exactly $0.02
    assert res.effect == "allow"
    with pytest.raises(BudgetExceeded):
        eng.reserve("gpt-4o", 1, 0)


def test_token_limit(table):
    eng = engine(table, Budget("daily", tokens=1000))
    eng.reserve("gpt-4o", 400, 400)
    with pytest.raises(BudgetExceeded, match="token limit"):
        eng.reserve("gpt-4o", 100, 101)


def test_soft_limit_warns_once_a_minute_and_allows(table, caplog):
    eng = engine(table, Budget("daily", usd=1, soft_usd=0.01))
    with caplog.at_level(logging.WARNING, logger="briefcase.budgets"):
        first = eng.reserve("gpt-4o", 1000, 1000)
        second = eng.reserve("gpt-4o", 1000, 1000)
    assert first.effect == second.effect == "soft_limit"
    assert sum("soft limit" in r.getMessage() for r in caplog.records) == 1


def test_warn_lets_the_call_through_and_counts_it(table, caplog):
    eng = engine(table, Budget("daily", usd=0.01, on_exceed="warn"))
    with caplog.at_level(logging.WARNING, logger="briefcase.budgets"):
        res = eng.reserve("gpt-4o", 1000, 1000)
    assert res.effect == "warn"
    assert used(eng)[0] == pytest.approx(0.02)
    assert any("on_exceed='warn'" in r.getMessage() for r in caplog.records)


def test_degrade_switches_to_the_cheaper_model(table):
    eng = engine(table, Budget("daily", usd=0.01, on_exceed="degrade", degrade_to="gpt-4o-mini"))
    res = eng.reserve("gpt-4o", 1000, 1000)
    assert res.model == "gpt-4o-mini" and res.requested_model == "gpt-4o"
    assert res.effect == "degrade"
    assert res.fields()["budget_degraded_from"] == "gpt-4o"
    assert used(eng)[0] == pytest.approx(1000 * 0.15e-6 + 1000 * 0.6e-6)


def test_degrade_callable_gets_the_context(table):
    seen = {}

    def cheaper(model, budget, info):
        seen.update(model=model, budget=budget.name, **info)
        return "gpt-4o-mini"

    eng = engine(table, Budget("per", usd=0.01, per="tenant", on_exceed="degrade",
                               degrade=cheaper))
    res = eng.reserve("gpt-4o", 1000, 1000, scope={"tenant": "acme"})
    assert res.model == "gpt-4o-mini"
    assert seen["model"] == "gpt-4o" and seen["budget"] == "per"
    assert seen["scope"] == {"tenant": "acme"}


def test_degrade_that_still_does_not_fit_blocks(table):
    eng = engine(table, Budget("daily", usd=0.0001, on_exceed="degrade",
                               degrade_to="gpt-4o-mini"))
    with pytest.raises(BudgetExceeded):
        eng.reserve("gpt-4o", 10000, 1000)
    assert used(eng) == (0, 0, 0)


def test_degrade_callable_returning_none_blocks(table):
    eng = engine(table, Budget("daily", usd=0.001, on_exceed="degrade",
                               degrade=lambda *a: None))
    with pytest.raises(BudgetExceeded):
        eng.reserve("gpt-4o", 1000, 1000)


def test_several_budgets_are_all_or_nothing(table):
    eng = engine(table, Budget("daily", usd=10), Budget("tiny", usd=0.001))
    with pytest.raises(BudgetExceeded) as info:
        eng.reserve("gpt-4o", 1000, 1000)
    assert info.value.budget == "tiny"
    assert used(eng, "daily") == (0, 0, 0)  # the first hold was released


# -- unpriced models -------------------------------------------------------------------------

def test_unpriced_warn_logs_once_naming_the_fix(table, caplog):
    eng = engine(table, Budget("daily", usd=1, tokens=10_000))
    with caplog.at_level(logging.WARNING, logger="briefcase.budgets"):
        res = eng.reserve("my-private-model", 100, 100)
        eng.reserve("my-private-model", 100, 100)
    lines = [r.getMessage() for r in caplog.records if "no price" in r.getMessage()]
    assert len(lines) == 1 and "briefcase pricing set my-private-model" in lines[0]
    assert res.effect == "unpriced" and res.priced is False
    assert used(eng)[:2] == (0, 400)  # token limits still count
    fields = eng.settle(res, {"prompt_tokens": 50, "completion_tokens": 5})
    assert fields["cost_source"] == "unpriced" and fields["cost_usd"] is None


def test_unpriced_allow_is_silent(table, caplog):
    eng = engine(table, Budget("daily", usd=1), unpriced="allow")
    with caplog.at_level(logging.WARNING, logger="briefcase.budgets"):
        eng.reserve("my-private-model", 100, 100)
    assert not caplog.records


def test_unpriced_block(table):
    eng = engine(table, Budget("daily", usd=1), unpriced="block")
    with pytest.raises(BudgetExceeded) as info:
        eng.reserve("my-private-model", 100, 100)
    assert info.value.reason == "unpriced"
    assert "briefcase pricing set" in str(info.value)


def test_unpriced_with_only_token_budgets_is_fine(table, caplog):
    eng = engine(table, Budget("daily", tokens=1000), unpriced="block")
    assert eng.reserve("my-private-model", 100, 100).effect == "allow"


def test_missing_output_cap_reserves_the_model_maximum(table):
    eng = engine(table, Budget("daily", usd=100))
    res = eng.reserve("gpt-4o", 10, None)
    assert res.max_output_tokens == 16384  # gpt-4o max_output_tokens in the table
    eng2 = engine(table, Budget("daily", tokens=10**9), default_max_tokens=777)
    assert eng2.reserve("my-private-model", 10, None).max_output_tokens == 777


# -- windows and scopes ---------------------------------------------------------------------

def test_window_bounds():
    t = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert window_bounds("hour", t) == ("2026-12-31T23", datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert window_bounds("day", t) == ("2026-12-31", datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert window_bounds("month", t) == ("2026-12", datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert window_bounds("total", t) == ("total", None)
    with pytest.raises(ValueError):
        window_bounds("week", t)


def test_window_rollover_starts_a_new_counter(table):
    clock = Clock(datetime(2026, 11, 22, 23, 59, tzinfo=timezone.utc))
    eng = engine(table, Budget("daily", usd=0.02), clock=clock)
    eng.reserve("gpt-4o", 1000, 1000)
    with pytest.raises(BudgetExceeded):
        eng.reserve("gpt-4o", 1000, 1000)
    clock.now = datetime(2026, 11, 23, 0, 0, 1, tzinfo=timezone.utc)
    assert eng.reserve("gpt-4o", 1000, 1000).effect == "allow"
    assert eng.status()[0]["window_id"] == "2026-11-23"


def test_settle_after_rollover_goes_to_the_reserved_window(table):
    clock = Clock(datetime(2026, 11, 22, 23, 59, tzinfo=timezone.utc))
    eng = engine(table, Budget("daily", usd=1), clock=clock)
    res = eng.reserve("gpt-4o", 1000, 1000)
    clock.now = datetime(2026, 11, 23, 0, 1, tzinfo=timezone.utc)
    eng.settle(res, {"prompt_tokens": 1, "completion_tokens": 1})
    assert eng.store.get("daily|*|2026-11-22")[2] == 0  # released where it was reserved
    assert eng.store.get("daily|*|2026-11-23") == (0, 0, 0, 0)


def test_budget_scope_contextvar_and_per_tenant_counters(table):
    eng = engine(table, Budget("per", usd=0.02, per="tenant"))
    with budget_scope(tenant="acme", user="u1"):
        assert current_scope() == {"tenant": "acme", "user": "u1"}
        with budget_scope(user=None):
            assert current_scope() == {"tenant": "acme"}
        eng.reserve("gpt-4o", 1000, 1000)
        with pytest.raises(BudgetExceeded) as info:
            eng.reserve("gpt-4o", 1000, 1000)
        assert info.value.scope == "tenant=acme"
    with budget_scope(tenant="globex"):
        assert eng.reserve("gpt-4o", 1000, 1000).effect == "allow"
    assert eng.reserve("gpt-4o", 1000, 1000).holds[0].scope == "tenant=-"
    assert current_scope() == {}


async def _scoped(eng, tenant):
    with budget_scope(tenant=tenant):
        await asyncio.sleep(0)
        return eng.reserve("gpt-4o", 1, 1).holds[0].scope


def test_budget_scope_follows_asyncio_tasks(table):
    eng = engine(table, Budget("per", usd=1, per="tenant"))

    async def main():
        return await asyncio.gather(*(_scoped(eng, f"t{i}") for i in range(5)))

    assert asyncio.run(main()) == [f"tenant=t{i}" for i in range(5)]


def test_models_where_and_per_model_filters(table):
    eng = engine(table, Budget("big", usd=0.001, models=["gpt-4o"]),
                 Budget("acme", usd=0.001, where={"tenant": "acme"}),
                 Budget("each", tokens=10**6, per="model"))
    res = eng.reserve("gpt-4o-mini", 100, 100)        # "big" does not match gpt-4o-mini
    assert [h.budget.name for h in res.holds] == ["each"]
    assert res.holds[0].scope == "model=gpt-4o-mini"
    with pytest.raises(BudgetExceeded) as info:
        eng.reserve("gpt-4o-mini", 2000, 2000, scope={"tenant": "acme"})
    assert info.value.budget == "acme"


def test_dry_run_writes_nothing_and_explains(table):
    eng = engine(table, Budget("daily", usd=0.01))
    res = eng.reserve("gpt-4o", 1000, 1000, dry_run=True)
    assert res.effect == "block" and "would be exceeded" in res.reasons[0]
    assert eng.store.scan("") == []


def test_budget_validation():
    with pytest.raises(ValueError, match="sets no limit"):
        Budget("x")
    with pytest.raises(ValueError, match="window"):
        Budget("x", usd=1, window="week")
    with pytest.raises(ValueError, match="degrade_to"):
        Budget("x", usd=1, on_exceed="degrade")
    with pytest.raises(ValueError, match="non-negative"):
        Budget("x", usd=-1)
    with pytest.raises(ValueError, match="name"):
        Budget("a|b", usd=1)
    with pytest.raises(ValueError, match="unpriced"):
        Budgets(unpriced="maybe")


# -- concurrency -----------------------------------------------------------------------------

def test_threads_reserving_never_overshoot(table):
    eng = engine(table, Budget("daily", usd=1.0))   # $0.02 a call -> 50 fit
    won, lock = [], threading.Lock()

    def worker():
        for _ in range(10):
            try:
                res = eng.reserve("gpt-4o", 1000, 1000)
            except BudgetExceeded:
                continue
            with lock:
                won.append(res)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 50
    assert used(eng)[0] == pytest.approx(1.0)
    for res in won:  # settling to half frees half the budget
        eng.settle(res, {"prompt_tokens": 500, "completion_tokens": 500})
    assert used(eng)[0] == pytest.approx(0.5)
    assert used(eng)[2] == 0


def test_store_default_is_memory():
    assert isinstance(Budgets().store, MemoryBudgetStore)
