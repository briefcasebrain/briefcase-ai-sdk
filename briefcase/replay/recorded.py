"""Replay recorded provider calls and see what changed.

A JSONL file of decision records written by provider auto-instrumentation
(``briefcase.auto(capture_content=True)`` with a file exporter) can be
re-executed against the provider, or answered from a cassette, and each
answer compared with the recorded one::

    from briefcase.replay.recorded import (
        CassetteExecutor, load_records, make_plan, run, summarize,
    )

    calls = load_records("runs.jsonl")
    plan = make_plan(calls, CassetteExecutor())
    rows = run(plan, CassetteExecutor())
    print(summarize(rows))

or from the shell: ``briefcase replay run runs.jsonl``.

Pure Python; needs no compiled core. Live executors import the provider SDK
only when a record needs it. See ``docs/features/replay-recorded.md``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Union
from pathlib import Path

from briefcase.replay._recorded.compare import (
    LEVELS,
    Scorer,
    compare,
    normalize_text,
    unified_diff,
)
from briefcase.replay._recorded.executors import (
    REPLAY_HEADER,
    AnthropicExecutor,
    AutoExecutor,
    BedrockExecutor,
    CassetteExecutor,
    Execution,
    Executor,
    ExecutorError,
    GoogleGenAIExecutor,
    OpenAIExecutor,
    Overrides,
    replaying,
    resolve_request,
)
from briefcase.replay._recorded.records import (
    SURFACES,
    UNRECORDED_SETTINGS,
    RecordedCall,
    analyze,
    load_records,
)
from briefcase.replay._recorded.runner import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_CALLS,
    STATUSES,
    Plan,
    PlanError,
    make_plan,
    run,
    summarize,
)


def replay(source: Union[str, Path, Iterable[Dict[str, Any]]], executor: Optional[Any] = None,
           *, overrides: Optional[Overrides] = None, limit: Optional[int] = None,
           sample: Optional[int] = None, seed: int = 0,
           concurrency: int = DEFAULT_CONCURRENCY, max_calls: int = DEFAULT_MAX_CALLS,
           scorer: Optional[Scorer] = None, threshold: float = 0.9) -> List[Dict[str, Any]]:
    """Load, plan and run in one call. ``executor`` defaults to
    :class:`CassetteExecutor` (no network); pass ``AutoExecutor()`` to call
    the providers. Returns the report rows."""
    executor = executor if executor is not None else CassetteExecutor()
    calls = load_records(source)
    plan = make_plan(calls, executor, overrides, limit=limit, sample=sample, seed=seed)
    return run(plan, executor, concurrency=concurrency, max_calls=max_calls,
               scorer=scorer, threshold=threshold)


__all__ = [
    "AnthropicExecutor",
    "AutoExecutor",
    "BedrockExecutor",
    "CassetteExecutor",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MAX_CALLS",
    "Execution",
    "Executor",
    "ExecutorError",
    "GoogleGenAIExecutor",
    "LEVELS",
    "OpenAIExecutor",
    "Overrides",
    "Plan",
    "PlanError",
    "REPLAY_HEADER",
    "RecordedCall",
    "STATUSES",
    "SURFACES",
    "Scorer",
    "UNRECORDED_SETTINGS",
    "analyze",
    "compare",
    "load_records",
    "make_plan",
    "normalize_text",
    "replay",
    "replaying",
    "resolve_request",
    "run",
    "summarize",
    "unified_diff",
]
