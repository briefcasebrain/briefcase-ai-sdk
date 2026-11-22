"""
briefcase.capture  Decorator for capturing arbitrary function executions.

Wraps sync or async functions and records inputs, outputs, timing, and errors
as Briefcase decision records, then exports via the configured exporter.

Usage:
    import briefcase

    @briefcase.capture
    def classify_risk(claim_data):
        return model.predict(claim_data)

    @briefcase.capture(decision_type="risk_classification", context_version="v3")
    async def async_classify(data):
        return await model.apredict(data)
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import random
import reprlib
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, List, Optional, TypeVar, overload

from briefcase import prompts as _prompts
from briefcase._export_mixin import ExportMixin
from briefcase._otel import HAS_OTEL, trace as _otel_trace

try:
    from briefcase.correlation.workflow import get_current_workflow
except ImportError:  # pragma: no cover - correlation is part of the base package
    def get_current_workflow():  # type: ignore[misc]
        return None

if TYPE_CHECKING:
    from typing_extensions import ParamSpec

    _P = ParamSpec("_P")

_R = TypeVar("_R")

# Version of the record layout emitted by @capture. Bumped when fields are
# added or change meaning, so consumers can branch on it.
#   "1.1": adds schema_version itself, the optional correlation fields
#          (workflow_id, trace_id, parent_span_id, trace_flags), the optional
#          status field ("cancelled"), and stream outputs for generators.
#   "1.2": adds the optional prompt_name / prompt_version fields (records
#          made under briefcase.prompt), and on provider records
#          (briefcase.integrations.providers) parent_run_id / parent_framework
#          linking a provider record to the framework LLM run it was made in.
#          Readers accept both "1.1" and "1.2": every 1.2 field is optional.
SCHEMA_VERSION = "1.2"

# How much of the call's content a record may carry:
#   "full"  bounded reprs (optionally rewritten by a redact hook)
#   "hash"  SHA-256 digests + character counts + type names, never the content
#   "none"  shape only (arg counts, result type) with no content and no digests
CAPTURE_CONTENT_MODES = ("full", "hash", "none")

# Ceiling on how many yielded chunks a generator capture keeps in memory for
# the output field. Chunks past it are counted but not retained.
MAX_COLLECTED_CHUNKS = 1000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


#  Internal export helper

class _RecordExporter(ExportMixin):
    """Minimal ExportMixin-backed object used by the capture decorator."""
    def __init__(self, exporter: Any, async_capture: bool) -> None:
        self._exporter = exporter
        self.async_capture = async_capture


#  Decorator implementation

@overload
def capture(fn: Callable[_P, _R]) -> Callable[_P, _R]: ...


@overload
def capture(
    fn: None = None,
    *,
    decision_type: Optional[str] = None,
    context_version: Optional[str] = None,
    max_input_chars: int = 1000,
    max_output_chars: int = 1000,
    exporter: Any = None,
    async_capture: bool = True,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
    sample_rate: float = 1.0,
    rng: Optional[random.Random] = None,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]: ...


@overload
def capture(
    fn: Callable[_P, _R],
    *,
    decision_type: Optional[str] = None,
    context_version: Optional[str] = None,
    max_input_chars: int = 1000,
    max_output_chars: int = 1000,
    exporter: Any = None,
    async_capture: bool = True,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
    sample_rate: float = 1.0,
    rng: Optional[random.Random] = None,
) -> Callable[_P, _R]: ...


def capture(
    fn: Any = None,
    *,
    decision_type: Optional[str] = None,
    context_version: Optional[str] = None,
    max_input_chars: int = 1000,
    max_output_chars: int = 1000,
    exporter: Any = None,
    async_capture: bool = True,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
    sample_rate: float = 1.0,
    rng: Optional[random.Random] = None,
) -> Any:
    """Decorator that captures function execution as a Briefcase decision record.

    Can be used with or without arguments:

        @briefcase.capture
        def my_func(x): ...

        @briefcase.capture(decision_type="classify", context_version="v2")
        async def my_async_func(x): ...

    Works on plain functions, coroutine functions, generator functions, and
    async generator functions; the decorated function keeps its signature
    for type checkers. A generator's record is emitted once, when the
    generator finishes, is closed, or raises: its output is the collected
    chunks (bounded) and its duration covers the whole iteration.

    Captured fields:
        - schema_version (layout version of the record, currently "1.2")
        - decision_id (uuid)
        - decision_type (function qualname or custom value)
        - function_name
        - inputs (bounded repr of positional + keyword args)
        - outputs (bounded repr of the return value; for generators the
          collected chunks plus chunk_count / stream_completed)
        - error (if an exception was raised; exception is re-raised)
        - status ("cancelled", only when the call was cancelled, e.g. by
          asyncio.wait_for or task cancellation; the cancellation re-raises)
        - started_at / ended_at (ISO 8601)
        - execution_time_ms
        - context_version (if provided)
        - workflow_id (inside a briefcase.correlation workflow)
        - trace_id / parent_span_id / trace_flags (when an OpenTelemetry
          span is active at call time)

    Args:
        decision_type:   Override the decision_type field. Defaults to function.__qualname__.
        context_version: Optional version tag added to all records.
        max_input_chars: Bound on the serialized input repr. Oversized values
                         render as both ends around an ellipsis and cost
                         O(bound), not O(len(value)).
        max_output_chars: Same bound for the serialized output repr.
        exporter:        Briefcase exporter instance. Falls back to BriefcaseConfig.get().exporter.
        async_capture:   If True (default), export runs in a background thread.
        capture_content: One of "full" (default), "hash", or "none". "hash" replaces
                         content with SHA-256 digests, counts, and type names; "none"
                         records shape only. In both non-full modes the error field
                         carries the exception class name, never the message.
        redact:          Optional str -> str hook applied to the bounded
                         inputs/outputs/error text in "full" mode. Ignored in
                         other modes; when no exporter is configured the hook
                         does not run at all.
        sample_rate:     Fraction of calls to record, 0.0-1.0 (default 1.0 =
                         every call). Unsampled calls run with no capture
                         overhead.
        rng:             ``random.Random`` used for the sampling decision;
                         pass a seeded instance for deterministic tests.
    """
    if capture_content not in CAPTURE_CONTENT_MODES:
        raise ValueError(
            f"capture_content must be one of {CAPTURE_CONTENT_MODES}, got {capture_content!r}"
        )
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float)) \
            or not 0.0 <= sample_rate <= 1.0:
        raise ValueError(f"sample_rate must be a number in [0.0, 1.0], got {sample_rate!r}")

    def decorator(func: Any) -> Any:
        return _make_wrapper(
            func,
            decision_type=decision_type,
            context_version=context_version,
            max_input_chars=max_input_chars,
            max_output_chars=max_output_chars,
            exporter=exporter,
            async_capture=async_capture,
            capture_content=capture_content,
            redact=redact,
            sample_rate=float(sample_rate),
            rng=rng,
        )

    if fn is not None:
        # Called as @capture or capture(fn, ...); fn is the decorated function
        return decorator(fn)
    # Called as @capture(...)  return a decorator
    return decorator


#  Internal wrapper factory

class _ChunkCollector:
    """Keeps a bounded prefix of a generator's yielded values and counts all
    of them."""

    __slots__ = ("limit", "chunks", "count")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.chunks: List[Any] = []
        self.count = 0

    def add(self, value: Any) -> None:
        self.count += 1
        if len(self.chunks) < self.limit:
            self.chunks.append(value)

    def summary(self, completed: bool) -> dict:
        info = {"chunk_count": self.count, "stream_completed": completed}
        if self.count > len(self.chunks):
            info["chunks_truncated"] = True
        return info


class _GeneratorTee:
    """Iterator proxy over a generator that reports each yielded value.

    ``yield from`` delegates ``send``/``throw``/``close`` to these methods,
    so the wrapped generator keeps full generator semantics."""

    __slots__ = ("_gen", "_add")

    def __init__(self, gen: Any, add: Callable[[Any], None]) -> None:
        self._gen = gen
        self._add = add

    def __iter__(self) -> "_GeneratorTee":
        return self

    def __next__(self) -> Any:
        value = next(self._gen)
        self._add(value)
        return value

    def send(self, sent: Any) -> Any:
        value = self._gen.send(sent)
        self._add(value)
        return value

    def throw(self, *exc_info: Any) -> Any:
        value = self._gen.throw(*exc_info)
        self._add(value)
        return value

    def close(self) -> None:
        self._gen.close()


def _make_wrapper(func, *, decision_type, context_version, max_input_chars,
                  max_output_chars, exporter, async_capture, capture_content,
                  redact, sample_rate=1.0, rng=None):
    _dt = decision_type or func.__qualname__
    _exporter_obj = _RecordExporter(exporter=exporter, async_capture=async_capture)
    # Full mode renders through a bounded repr so oversized values cost
    # O(limit); hash mode keeps the exact full repr (its digests and char
    # counts are defined over it).
    if capture_content == "full":
        _input_render = _bounded_repr(max_input_chars)
        _output_render = _bounded_repr(max_output_chars)
        # Every rendered chunk costs at least 3 chars ("x, "), so chunks past
        # this count could never appear in the bounded output anyway.
        _chunk_limit = min(MAX_COLLECTED_CHUNKS, max(0, max_output_chars) // 3 + 2)
    else:
        _input_render = _output_render = _safe_repr
        _chunk_limit = MAX_COLLECTED_CHUNKS if capture_content == "hash" else 0
    _rng = rng if rng is not None else random.Random()

    def _begin():
        """Resolve the exporter for this call, or None when the call is not
        captured (nothing configured, or sampled out). With no exporter
        configured anywhere, capture costs nothing: no repr, no redact, no
        record."""
        resolved = _exporter_obj._resolve_exporter()
        if resolved is None:
            return None
        if sample_rate < 1.0 and _rng.random() >= sample_rate:
            return None
        return resolved

    def _record(args, kwargs):
        return _build_record(_dt, context_version, args, kwargs, max_input_chars,
                             func.__name__, capture_content, redact,
                             render=_input_render)

    def _finish(record, started_at, resolved, *, result=None, error=None, stream=None):
        _finalize_record(record, started_at, result=result, error=error,
                         max_output_chars=max_output_chars,
                         capture_content=capture_content, redact=redact,
                         render=_output_render, stream=stream)
        _exporter_obj._trigger_export(record, exporter=resolved)

    if inspect.isasyncgenfunction(func):
        @functools.wraps(func)
        async def asyncgen_wrapper(*args, **kwargs):
            resolved = _begin()
            agen = func(*args, **kwargs)
            if resolved is None:
                collector = None
            else:
                record = _record(args, kwargs)
                started_at = datetime.now(timezone.utc)
                collector = _ChunkCollector(_chunk_limit)
            try:
                try:
                    value = await agen.__anext__()
                    while True:
                        if collector is not None:
                            collector.add(value)
                        try:
                            sent = yield value
                        except GeneratorExit:
                            await agen.aclose()
                            raise
                        except BaseException as thrown:
                            value = await agen.athrow(thrown)
                        else:
                            value = await agen.asend(sent)
                except StopAsyncIteration:
                    pass
            except GeneratorExit:
                # The consumer stopped early (break / aclose()): not an error.
                if collector is not None:
                    _finish(record, started_at, resolved, result=collector.chunks,
                            stream=collector.summary(False))
                raise
            except BaseException as exc:
                if collector is not None:
                    _finish(record, started_at, resolved, error=exc,
                            stream=collector.summary(False))
                raise
            if collector is not None:
                _finish(record, started_at, resolved, result=collector.chunks,
                        stream=collector.summary(True))

        return asyncgen_wrapper

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            resolved = _begin()
            if resolved is None:
                return await func(*args, **kwargs)
            record = _record(args, kwargs)
            started_at = datetime.now(timezone.utc)
            try:
                result = await func(*args, **kwargs)
            except BaseException as exc:
                # BaseException, not Exception: a cancelled or timed-out
                # call (asyncio.CancelledError) must still leave a record.
                _finish(record, started_at, resolved, error=exc)
                raise
            _finish(record, started_at, resolved, result=result)
            return result

        return async_wrapper

    if inspect.isgeneratorfunction(func):
        @functools.wraps(func)
        def generator_wrapper(*args, **kwargs):
            resolved = _begin()
            if resolved is None:
                return (yield from func(*args, **kwargs))
            record = _record(args, kwargs)
            started_at = datetime.now(timezone.utc)
            collector = _ChunkCollector(_chunk_limit)
            try:
                returned = yield from _GeneratorTee(func(*args, **kwargs), collector.add)
            except GeneratorExit:
                # The consumer stopped early (break / close()): not an error.
                _finish(record, started_at, resolved, result=collector.chunks,
                        stream=collector.summary(False))
                raise
            except BaseException as exc:
                _finish(record, started_at, resolved, error=exc,
                        stream=collector.summary(False))
                raise
            _finish(record, started_at, resolved, result=collector.chunks,
                    stream=collector.summary(True))
            return returned

        return generator_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        resolved = _begin()
        if resolved is None:
            return func(*args, **kwargs)
        record = _record(args, kwargs)
        started_at = datetime.now(timezone.utc)
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            _finish(record, started_at, resolved, error=exc)
            raise
        _finish(record, started_at, resolved, result=result)
        return result

    return sync_wrapper


def _safe_repr(value: Any) -> str:
    """repr that never raises; record building must not alter the call."""
    try:
        return repr(value)
    except Exception:
        return "<unreprable>"


def _bounded_repr(limit: int) -> Callable[[Any], str]:
    """A repr bounded at build time: an oversized value costs O(limit), not
    O(len(value)), and renders as both ends around an ellipsis. Aggregate
    container caps are raised so bounding applies to element size, not to
    how many elements render. Never raises."""
    r = reprlib.Repr()
    r.maxstring = limit
    r.maxother = limit
    r.maxlong = limit
    big = max(64, limit)
    r.maxdict = r.maxlist = r.maxtuple = r.maxset = r.maxfrozenset = big
    r.maxdeque = r.maxarray = big
    r.maxlevel = 20

    def _render(value: Any) -> str:
        try:
            return r.repr(value)
        except Exception:
            return "<unreprable>"

    return _render


def _full_text(text: str, redact: Optional[Callable[[str], str]], limit: int) -> str:
    if redact is not None:
        # Fail closed: when the redactor raises, substitute a placeholder
        # rather than emitting the unredacted text or breaking the call.
        try:
            text = redact(text)
        except Exception:
            text = "<redaction-failed>"
    return text[:limit]


def _build_record(
    decision_type: str,
    context_version: Optional[str],
    args: tuple,
    kwargs: dict,
    max_input_chars: int,
    function_name: str,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
    render: Callable[[Any], str] = _safe_repr,
) -> dict:
    """Build the initial decision record with inputs."""
    inputs: dict = {}
    if capture_content == "full":
        if args:
            inputs["args"] = _full_text(render(args), redact, max_input_chars)
        if kwargs:
            inputs["kwargs"] = _full_text(render(kwargs), redact, max_input_chars)
    elif capture_content == "hash":
        if args:
            text = _safe_repr(args)
            inputs["args_sha256"] = _sha256(text)
            inputs["args_chars"] = len(text)
        if kwargs:
            text = _safe_repr(kwargs)
            inputs["kwargs_sha256"] = _sha256(text)
            inputs["kwargs_chars"] = len(text)
    else:  # "none"
        if args:
            inputs["args_count"] = len(args)
        if kwargs:
            inputs["kwargs_count"] = len(kwargs)

    record = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": str(uuid.uuid4()),
        "decision_type": decision_type,
        "function_name": function_name,
        "inputs": inputs,
        "outputs": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if context_version is not None:
        record["context_version"] = context_version
    _add_correlation(record)
    _prompts.on_record_start(record)  # prompt_name / prompt_version (briefcase.prompts)
    return record


def _add_correlation(record: dict) -> None:
    """Attach ids from the ambient correlation context, when there is one:
    the active briefcase.correlation workflow and the active OpenTelemetry
    span. The span context is read here, on the caller's thread, because the
    background export worker cannot see it; OTelExporter uses these fields to
    parent the decision span. Never raises."""
    try:
        workflow = get_current_workflow()
        if workflow is not None:
            record["workflow_id"] = workflow.workflow_id
        if HAS_OTEL:
            span_context = _otel_trace.get_current_span().get_span_context()
            if span_context.is_valid:
                record["trace_id"] = format(span_context.trace_id, "032x")
                record["parent_span_id"] = format(span_context.span_id, "016x")
                record["trace_flags"] = int(span_context.trace_flags)
    except Exception:
        pass


def _finalize_record(
    record: dict,
    started_at: datetime,
    *,
    result: Any = None,
    error: Optional[BaseException] = None,
    max_output_chars: int = 1000,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
    render: Callable[[Any], str] = _safe_repr,
    stream: Optional[dict] = None,
) -> None:
    """Mutate record in-place with timing and output/error.

    ``stream`` is set for generator captures: ``result`` is then the list of
    collected chunks and the dict (chunk_count, stream_completed, ...) is
    merged into the outputs."""
    ended_at = datetime.now(timezone.utc)
    # The record's timestamps and its duration come from the same two
    # instants. ``_build_record`` stamps a provisional ``started_at`` before
    # correlation lookup; the wrapper's ``started_at`` (taken immediately
    # before the call) replaces it so that ended_at - started_at equals
    # execution_time_ms exactly.
    record["started_at"] = started_at.isoformat()
    record["ended_at"] = ended_at.isoformat()
    record["execution_time_ms"] = (ended_at - started_at).total_seconds() * 1000

    if error is not None:
        if isinstance(error, asyncio.CancelledError):
            record["status"] = "cancelled"
        if capture_content == "full":
            # An exception with no message (CancelledError, bare ValueError())
            # is named by its class rather than recorded as an empty string.
            record["error"] = _full_text(
                str(error) or type(error).__name__, redact, max_output_chars
            )
        elif capture_content == "hash":
            record["error"] = f"{type(error).__name__}:sha256:{_sha256(str(error))}"
        else:  # "none"
            record["error"] = type(error).__name__
    elif capture_content == "full":
        record["outputs"] = {"result": _full_text(render(result), redact, max_output_chars)}
    elif capture_content == "hash":
        text = _safe_repr(result)
        record["outputs"] = {
            "result_sha256": _sha256(text),
            "result_chars": len(text),
            "result_type": type(result).__name__,
        }
    else:  # "none"
        record["outputs"] = {"result_type": type(result).__name__}

    if stream is not None:
        outputs = record["outputs"]
        if "result_type" in outputs:
            outputs["result_type"] = "generator"
        outputs.update(stream)
    _prompts.on_record_end(record)
