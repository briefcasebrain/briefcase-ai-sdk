"""Score a function against a dataset version, offline.

Each case calls the target once. Results are logged through
:class:`briefcase.integrations.evals.EvalRun`, so they are ordinary
``eval.case`` / ``eval.run`` decision records sent to whichever exporter is
given. Case order, scoring and the summary are deterministic for a
deterministic target, whatever ``concurrency`` is.

Calling convention: a case input of the form ``{"args": [...], "kwargs":
{...}}`` (what ``@capture`` records decode to) is spread into the call,
``target(*args, **kwargs)``; any other input is passed as one argument.
Coroutine functions are awaited.

Scorers get ``(output, reference, case)`` and return a bool, a number (passes
at >= 1.0), or ``{"score": float, "passed": bool}``.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import inspect
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from briefcase import prompts as _prompts
from briefcase.datasets.core import DatasetError, DatasetVersion, canonical, jsonable, now_iso

BUILTIN_SCORERS = ("exact", "contains", "json")
_MISSING = object()


# -- loading user code ------------------------------------------------------

def load_callable(spec: str, what: str = "target") -> Callable[..., Any]:
    """``"package.module:function"`` -> the function. The current directory is
    importable, as it is for ``python -m``."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise DatasetError(f"--{what} must be module:function (for example --{what} myapp.triage:classify), "
                           f"got {spec!r}")
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise DatasetError(f"cannot import {module_name!r} for --{what} {spec}: {type(exc).__name__}: {exc}. "
                           "Run from your project root or set PYTHONPATH.") from exc
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part, _MISSING)
        if obj is _MISSING:
            raise DatasetError(f"{module_name} has no attribute {attr!r} (--{what} {spec})")
    if not callable(obj):
        raise DatasetError(f"--{what} {spec} is not callable")
    return obj


# -- scorers ----------------------------------------------------------------

def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)


def score_exact(output: Any, reference: Any, case: dict) -> bool:
    if isinstance(output, str) and isinstance(reference, str):
        return output.strip() == reference.strip()
    return canonical(jsonable(output)) == canonical(reference)


def score_contains(output: Any, reference: Any, case: dict) -> bool:
    return _as_text(reference).strip() in _as_text(output)


def score_json(output: Any, reference: Any, case: dict) -> dict:
    def parse(value: Any, side: str) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                raise ValueError(f"{side} is not JSON")
        return jsonable(value)

    try:
        left, right = parse(output, "output"), parse(reference, "reference")
    except ValueError as exc:
        return {"score": 0.0, "passed": False, "note": str(exc)}
    same = canonical(left) == canonical(right)
    return {"score": 1.0 if same else 0.0, "passed": same}


_SCORERS = {"exact": score_exact, "contains": score_contains, "json": score_json}


def resolve_scorer(spec: str) -> Tuple[str, Callable[..., Any], bool]:
    """``(name, fn, needs_reference)``."""
    if spec in _SCORERS:
        return spec, _SCORERS[spec], True
    if ":" in spec:
        return spec, load_callable(spec, "scorer"), False
    raise DatasetError(f"unknown --scorer {spec!r}: use one of {', '.join(BUILTIN_SCORERS)} or module:function")


def normalize_score(value: Any) -> Tuple[float, bool, Optional[str]]:
    if isinstance(value, bool):
        return (1.0 if value else 0.0), value, None
    if isinstance(value, (int, float)):
        return float(value), float(value) >= 1.0, None
    if isinstance(value, dict) and "score" in value:
        score = float(value["score"])
        passed = bool(value["passed"]) if "passed" in value else score >= 1.0
        return score, passed, value.get("note")
    raise TypeError(f"scorer returned {type(value).__name__}; return bool, a number, "
                    "or {'score': float, 'passed': bool}")


# -- running ----------------------------------------------------------------

@dataclass
class CaseResult:
    case_id: str
    output: Any = None
    score: float = 0.0
    passed: bool = False
    error: Optional[str] = None
    note: Optional[str] = None
    prompt_name: Optional[str] = None
    prompt_version: Optional[str] = None
    duration_ms: float = 0.0
    reference: Any = None
    input: Any = None


def call_target(target: Callable[..., Any], case_input: Any) -> Any:
    if isinstance(case_input, dict) and case_input and set(case_input) <= {"args", "kwargs"}:
        result = target(*(case_input.get("args") or []), **(case_input.get("kwargs") or {}))
    else:
        result = target(case_input)
    if inspect.isawaitable(result):
        async def _await() -> Any:
            return await result
        result = asyncio.run(_await())
    return result


def _run_case(target: Callable[..., Any], scorer: Callable[..., Any], needs_reference: bool,
              case: dict) -> CaseResult:
    res = CaseResult(case_id=case["id"], reference=case.get("reference"), input=case.get("input"))
    frame: dict = {}
    _prompts.on_record_start(frame)
    started = time.perf_counter()
    try:
        res.output = call_target(target, case.get("input"))
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
    res.duration_ms = (time.perf_counter() - started) * 1000
    _prompts.on_record_end(frame)
    res.prompt_name = frame.get("prompt_name")
    res.prompt_version = frame.get("prompt_version")
    if res.error is None:
        if needs_reference and case.get("reference") is None:
            res.error = "no reference (dataset made with --reference none); use --scorer module:function"
        else:
            try:
                res.score, res.passed, res.note = normalize_score(
                    scorer(res.output, case.get("reference"), case))
            except Exception as exc:
                res.error = f"scorer failed: {type(exc).__name__}: {exc}"
    return res


def run_cases(cases: Sequence[dict], target: Callable[..., Any], scorer: Callable[..., Any],
              *, needs_reference: bool = True, concurrency: int = 1) -> List[CaseResult]:
    """Run every case; results come back in dataset order. Each case runs in
    a copy of the caller's context, so prompt tags never leak between cases."""
    def one(case: dict) -> CaseResult:
        return contextvars.copy_context().run(_run_case, target, scorer, needs_reference, case)

    if concurrency <= 1:
        return [one(c) for c in cases]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(one, cases))


# -- summarising ------------------------------------------------------------

@dataclass
class GroupStats:
    target: str
    prompt_version: Optional[str]
    prompt_name: Optional[str]
    cases: int = 0
    passed: int = 0
    errors: int = 0
    score_total: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.cases if self.cases else 0.0

    @property
    def mean_score(self) -> float:
        return self.score_total / self.cases if self.cases else 0.0

    def as_dict(self) -> dict:
        return {"target": self.target, "prompt_name": self.prompt_name, "prompt_version": self.prompt_version,
                "cases": self.cases, "passed": self.passed, "errors": self.errors,
                "pass_rate": round(self.pass_rate, 6), "mean_score": round(self.mean_score, 6)}


def group_results(target: str, results: Sequence[CaseResult]) -> List[GroupStats]:
    groups: Dict[Tuple[Optional[str], Optional[str]], GroupStats] = {}
    for r in results:
        key = (r.prompt_name, r.prompt_version)
        g = groups.setdefault(key, GroupStats(target, r.prompt_version, r.prompt_name))
        g.cases += 1
        g.passed += int(r.passed)
        g.errors += int(r.error is not None)
        g.score_total += r.score
    return list(groups.values())


def worst_first(results: Sequence[CaseResult]) -> List[CaseResult]:
    failing = [r for r in results if not r.passed]
    return sorted(failing, key=lambda r: (r.error is None, r.score, r.case_id))


# -- logging ----------------------------------------------------------------

def default_records_path(home: Path) -> Path:
    return home / "evals" / "records.jsonl"


def resolve_exporter(to: Optional[str], home: Path, env: Optional[Mapping[str, str]] = None) -> Tuple[Any, str]:
    """``(exporter, description)``. ``--to`` wins; then ``BRIEFCASE_EXPORTER``
    or the OpenTelemetry variables; else a local JSONL under ``home``."""
    from briefcase._observe import _exporter_from_env, _resolve_exporter

    environ: Mapping[str, str] = os.environ if env is None else env
    if to:
        return _resolve_exporter(to), to
    configured = (environ.get("BRIEFCASE_EXPORTER") or "").strip()
    otel = (environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or "").strip()
    if configured or otel:
        return _exporter_from_env(environ), configured or f"otlp ({otel})"
    from briefcase.exporters.file import JSONLFileExporter

    path = default_records_path(home)
    return JSONLFileExporter(path), str(path)


def _digest(value: Any) -> str:
    from briefcase.datasets.core import sha256_text

    return "sha256:" + sha256_text(canonical(jsonable(value)))


def log_results(version: DatasetVersion, target_spec: str, scorer_name: str, results: Sequence[CaseResult],
                exporter: Any, *, capture_content: bool = False) -> dict:
    """Send results as eval.case + eval.run records; return the run record.

    Without ``capture_content`` the records carry SHA-256 digests of the
    input, output and reference instead of the content itself."""
    from briefcase.integrations.evals import EvalRun

    versions = sorted({r.prompt_version for r in results if r.prompt_version})
    run = EvalRun(
        f"eval:{version.ref}", exporter=exporter, async_capture=False,
        metadata={"dataset": version.name, "dataset_version": version.tag, "dataset_hash": version.hash,
                  "target": target_spec, "scorer": scorer_name, "prompt_versions": versions},
    )
    for r in results:
        meta: Dict[str, Any] = {"dataset": version.ref, "target": target_spec, "duration_ms": round(r.duration_ms, 3)}
        tags = []
        if r.prompt_version:
            meta.update(prompt_name=r.prompt_name, prompt_version=r.prompt_version)
            tags.append(f"prompt_version:{r.prompt_version}")
        if r.error:
            meta["error"] = r.error
        if capture_content:
            inputs, outputs, reference = r.input, r.output, r.reference
        else:
            inputs, outputs = _digest(r.input), _digest(r.output)
            reference = None if r.reference is None else _digest(r.reference)
        run.log_case(r.case_id, inputs=inputs, outputs=outputs, target=reference,
                     scores={"score": r.score}, passed=r.passed, tags=tags, metadata=meta)
    return run.finish()


def flush_exporter(exporter: Any) -> None:
    for method in ("flush", "close"):
        fn = getattr(exporter, method, None)
        if fn is None:
            continue
        try:
            result = fn()
            if inspect.isawaitable(result):
                async def _await(r: Any = result) -> Any:
                    return await r
                asyncio.run(_await())
        except Exception:
            pass


# -- history (no content: counts only) ---------------------------------------

def history_path(home: Path) -> Path:
    return home / "evals" / "history.jsonl"


def append_history(home: Path, entry: dict) -> None:
    path = history_path(home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now_iso(), **entry}, sort_keys=True) + "\n")


def last_history(home: Path) -> List[dict]:
    """Entries of the most recent ``eval run`` invocation (one per target)."""
    path = history_path(home)
    try:
        entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []
    if not entries:
        return []
    last = entries[-1].get("invocation")
    if last is None:
        return [entries[-1]]
    return [e for e in entries if e.get("invocation") == last]


__all__ = [
    "BUILTIN_SCORERS", "CaseResult", "GroupStats", "append_history", "call_target", "group_results",
    "last_history", "load_callable", "log_results", "normalize_score", "resolve_exporter", "resolve_scorer",
    "run_cases", "score_contains", "score_exact", "score_json", "worst_first",
]
