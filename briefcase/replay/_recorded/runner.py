"""Plan and run a replay of recorded calls, producing one report row per record.

Statuses are honest: ``matched`` and ``changed`` only for a record whose
replay ran and was compared; ``not_replayable`` (with the reasons) when it
could not be rebuilt or run by the chosen executor; ``error`` when the
replay call failed.
"""

from __future__ import annotations

import contextvars
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from briefcase.replay._recorded.compare import Scorer, compare, unified_diff
from briefcase.replay._recorded.executors import (
    Execution,
    ExecutorError,
    Overrides,
    cassette_entry,
    resolve_request,
)
from briefcase.replay._recorded.records import RecordedCall

RESULT_TYPE = "briefcase.replay_result"
STATUSES = ("matched", "changed", "not_replayable", "error")

DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_CALLS = 100

# Output-token caps, by the parameter name each API records.
_CAP_PARAMS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maxTokens")


class PlanError(Exception):
    """The plan cannot be run as requested (e.g. over ``max_calls``)."""


@dataclass
class Plan:
    calls: List[RecordedCall]
    selected: List[RecordedCall]
    blocked: Dict[int, List[str]]
    executor: str
    live: bool
    overrides: Overrides = field(default_factory=Overrides)

    @property
    def to_run(self) -> List[RecordedCall]:
        return [c for c in self.selected if c.index not in self.blocked]

    def summary(self) -> Dict[str, Any]:
        """Content-free plan: counts, per-model counts and a token estimate."""
        models: Dict[str, int] = {}
        prompt = 0
        bound = 0
        no_usage = 0
        uncapped = 0
        for call in self.to_run:
            model, params = resolve_request(call, self.overrides)
            models[str(model)] = models.get(str(model), 0) + 1
            usage = call.usage or {}
            p = usage.get("prompt_tokens")
            c = usage.get("completion_tokens")
            if not isinstance(p, int):
                no_usage += 1
                continue
            prompt += p
            cap = next((params[k] for k in _CAP_PARAMS if isinstance(params.get(k), int)), None)
            if cap is None:
                uncapped += 1
                bound += p + (c if isinstance(c, int) else 0)
            else:
                bound += p + cap
        replayable = [c for c in self.calls if c.replayable]
        return {
            "executor": self.executor,
            "live": self.live,
            "records": len(self.calls),
            "replayable": len(replayable),
            "not_replayable": len(self.calls) - len(replayable),
            "selected": len(self.selected),
            "blocked_by_executor": len(self.blocked),
            "blocked_reasons": _count_reasons(self.blocked.values()),
            "calls": len(self.to_run),
            "per_model": dict(sorted(models.items())),
            "tokens": {
                "recorded_prompt_tokens": prompt,
                "upper_bound_tokens": bound if uncapped == 0 else None,
                "estimate_tokens": bound,
                "calls_without_usage": no_usage,
                "calls_without_output_cap": uncapped,
            },
        }


def _count_reasons(groups: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for reasons in groups:
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def make_plan(calls: List[RecordedCall], executor: Any, overrides: Optional[Overrides] = None, *,
              limit: Optional[int] = None, sample: Optional[int] = None,
              seed: int = 0) -> Plan:
    """Choose which replayable records to run (``sample`` draws a seeded
    random subset, then ``limit`` keeps the first N) and ask the executor
    which of them it cannot run."""
    overrides = overrides or Overrides()
    candidates = [c for c in calls if c.replayable]
    if sample is not None and sample < len(candidates):
        picked = set(id(c) for c in random.Random(seed).sample(candidates, sample))
        candidates = [c for c in candidates if id(c) in picked]
    if limit is not None:
        candidates = candidates[:max(0, limit)]
    blocked: Dict[int, List[str]] = {}
    for call in candidates:
        reasons = executor.check(call, overrides)
        if reasons:
            blocked[call.index] = list(reasons)
    return Plan(calls, candidates, blocked, getattr(executor, "name", "custom"),
                bool(getattr(executor, "live", True)), overrides)


def _delta(a: Any, b: Any) -> Optional[float]:
    if isinstance(a, bool) or isinstance(b, bool):
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(b - a, 6)
    return None


def _token_delta(recorded: Optional[Dict[str, Any]], replayed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    recorded, replayed = recorded or {}, replayed or {}
    return {
        "recorded": recorded or None,
        "replay": replayed or None,
        "delta": {
            key: _delta(recorded.get(key), replayed.get(key))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        } if recorded and replayed else None,
    }


def _price(usage: Optional[Dict[str, Any]], model: Optional[str],
           prices: Optional[Dict[str, Dict[str, float]]]) -> Optional[float]:
    if not prices or not usage or not model or model not in prices:
        return None
    rate = prices[model]
    try:
        return round(usage.get("prompt_tokens", 0) * float(rate["input_per_mtok"]) / 1e6
                     + usage.get("completion_tokens", 0) * float(rate["output_per_mtok"]) / 1e6, 8)
    except (KeyError, TypeError, ValueError):
        return None


def _base_row(call: RecordedCall, executor: str) -> Dict[str, Any]:
    return {
        "record_type": RESULT_TYPE,
        "index": call.index,
        "decision_id": call.decision_id,
        "surface": call.surface,
        "provider": call.provider,
        "model": call.model,
        "executor": executor,
        "warnings": list(call.warnings),
    }


def not_replayable_row(call: RecordedCall, executor: str,
                       reasons: Optional[List[str]] = None) -> Dict[str, Any]:
    row = _base_row(call, executor)
    row.update(status="not_replayable", match_level=None,
               reasons=list(reasons if reasons is not None else call.reasons))
    return row


def result_row(call: RecordedCall, executor: str, overrides: Overrides,
               execution: Optional[Execution] = None, error: Optional[str] = None, *,
               scorer: Optional[Scorer] = None, threshold: float = 0.9,
               prices: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Any]:
    row = _base_row(call, executor)
    model, params = resolve_request(call, overrides)
    row["replay_model"] = model
    row["overrides"] = {
        "model": overrides.model, "params": overrides.params, "drop": sorted(overrides.drop),
    } if not overrides.empty else None
    if execution is None:
        row.update(status="error", match_level=None, error=error, reasons=[])
        return row
    recorded = call.output or {}
    result = compare(recorded, execution.output, scorer=scorer, threshold=threshold)
    row.update(
        status="matched" if result["match_level"] else "changed",
        match_level=result["match_level"],
        checks=result["checks"],
        score=result["score"],
        reasons=[],
        diff=unified_diff(recorded, execution.output) if result["match_level"] != "exact" else "",
        recorded=recorded,
        replay=execution.output,
        served_model=execution.model,
        finish_reason={"recorded": call.finish_reason, "replay": execution.finish_reason},
        tokens=_token_delta(call.usage, execution.usage),
        latency_ms={"recorded": call.latency_ms, "replay": execution.latency_ms,
                    "delta": _delta(call.latency_ms, execution.latency_ms)},
    )
    if "scorer_error" in result:
        row["scorer_error"] = result["scorer_error"]
    cost_a = call.cost_usd if call.cost_usd is not None else _price(call.usage, call.model, prices)
    cost_b = execution.cost_usd if execution.cost_usd is not None else _price(
        execution.usage, model, prices)
    row["cost_usd"] = {"recorded": cost_a, "replay": cost_b, "delta": _delta(cost_a, cost_b)}
    return row


def run(plan: Plan, executor: Any, *, concurrency: int = DEFAULT_CONCURRENCY,
        max_calls: int = DEFAULT_MAX_CALLS, scorer: Optional[Scorer] = None,
        threshold: float = 0.9, prices: Optional[Dict[str, Dict[str, float]]] = None,
        cassette: Optional[List[Dict[str, Any]]] = None,
        on_row: Optional[Callable[[Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
    """Execute the plan. Raises :class:`PlanError` before any call when the
    plan exceeds ``max_calls``. Rows come back in record order: one per
    selected record, plus one per record that is not replayable."""
    to_run = plan.to_run
    if plan.live and len(to_run) > max_calls:
        raise PlanError(
            f"the plan has {len(to_run)} live calls, over --max-calls {max_calls}; narrow it with "
            "--limit N or --sample N, or raise --max-calls"
        )
    rows: Dict[int, Dict[str, Any]] = {}
    for call in plan.calls:
        if not call.replayable:
            rows[call.index] = not_replayable_row(call, plan.executor)
    for index, reasons in plan.blocked.items():
        call = next(c for c in plan.calls if c.index == index)
        rows[index] = not_replayable_row(call, plan.executor, reasons)

    budget = threading.Semaphore(max_calls)
    lock = threading.Lock()

    def one(call: RecordedCall) -> Dict[str, Any]:
        if plan.live and not budget.acquire(blocking=False):  # pragma: no cover - guard
            return result_row(call, plan.executor, plan.overrides,
                              error="max-calls reached; not run")
        try:
            execution = executor.execute(call, plan.overrides)
        except ExecutorError as exc:
            return result_row(call, plan.executor, plan.overrides, error=str(exc))
        except Exception as exc:  # an executor bug must not abort the whole run
            return result_row(call, plan.executor, plan.overrides,
                              error=f"{type(exc).__name__}: {exc}")
        if cassette is not None:
            with lock:
                cassette.append(cassette_entry(call, plan.overrides, execution))
        return result_row(call, plan.executor, plan.overrides, execution,
                          scorer=scorer, threshold=threshold, prices=prices)

    workers = max(1, min(concurrency, len(to_run) or 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="briefcase-replay") as pool:
        futures = [(c, pool.submit(contextvars.copy_context().run, one, c)) for c in to_run]
        for call, future in futures:
            row = future.result()
            rows[call.index] = row
            if on_row is not None:
                on_row(row)
    return [rows[i] for i in sorted(rows)]


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts by status and match level plus summed deltas (content-free)."""
    counts = {status: 0 for status in STATUSES}
    levels: Dict[str, int] = {}
    tokens = 0.0
    have_tokens = False
    latency: List[float] = []
    cost = 0.0
    have_cost = False
    for row in rows:
        if row.get("record_type") != RESULT_TYPE:
            continue
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row.get("match_level"):
            levels[row["match_level"]] = levels.get(row["match_level"], 0) + 1
        delta = ((row.get("tokens") or {}).get("delta") or {}).get("total_tokens")
        if isinstance(delta, (int, float)):
            tokens += delta
            have_tokens = True
        lat = (row.get("latency_ms") or {}).get("delta")
        if isinstance(lat, (int, float)):
            latency.append(lat)
        c = (row.get("cost_usd") or {}).get("delta")
        if isinstance(c, (int, float)):
            cost += c
            have_cost = True
    return {
        "rows": sum(counts.values()),
        "status": counts,
        "match_levels": levels,
        "total_tokens_delta": tokens if have_tokens else None,
        "mean_latency_delta_ms": round(sum(latency) / len(latency), 3) if latency else None,
        "cost_usd_delta": round(cost, 8) if have_cost else None,
    }


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MAX_CALLS",
    "Plan",
    "PlanError",
    "RESULT_TYPE",
    "STATUSES",
    "make_plan",
    "not_replayable_row",
    "result_row",
    "run",
    "summarize",
]
