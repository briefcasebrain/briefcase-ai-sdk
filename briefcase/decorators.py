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

import asyncio
import functools
import hashlib
import reprlib
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from briefcase._export_mixin import ExportMixin

# How much of the call's content a record may carry:
#   "full"  bounded reprs (optionally rewritten by a redact hook)
#   "hash"  SHA-256 digests + character counts + type names, never the content
#   "none"  shape only (arg counts, result type) with no content and no digests
CAPTURE_CONTENT_MODES = ("full", "hash", "none")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


#  Internal export helper

class _RecordExporter(ExportMixin):
    """Minimal ExportMixin-backed object used by the capture decorator."""
    def __init__(self, exporter: Any, async_capture: bool) -> None:
        self._exporter = exporter
        self.async_capture = async_capture


#  Decorator implementation

def capture(
    fn=None,
    *,
    decision_type: Optional[str] = None,
    context_version: Optional[str] = None,
    max_input_chars: int = 1000,
    max_output_chars: int = 1000,
    exporter: Any = None,
    async_capture: bool = True,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
):
    """Decorator that captures function execution as a Briefcase decision record.

    Can be used with or without arguments:

        @briefcase.capture
        def my_func(x): ...

        @briefcase.capture(decision_type="classify", context_version="v2")
        async def my_async_func(x): ...

    Captured fields:
        - decision_id (uuid)
        - decision_type (function qualname or custom value)
        - function_name
        - inputs (bounded repr of positional + keyword args)
        - outputs (bounded repr of the return value)
        - error (if an exception was raised; exception is re-raised)
        - started_at / ended_at (ISO 8601)
        - execution_time_ms
        - context_version (if provided)

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
    """
    if capture_content not in CAPTURE_CONTENT_MODES:
        raise ValueError(
            f"capture_content must be one of {CAPTURE_CONTENT_MODES}, got {capture_content!r}"
        )

    if fn is not None:
        # Called as @capture or capture(fn, ...); fn is the decorated function
        return _make_wrapper(
            fn,
            decision_type=decision_type,
            context_version=context_version,
            max_input_chars=max_input_chars,
            max_output_chars=max_output_chars,
            exporter=exporter,
            async_capture=async_capture,
            capture_content=capture_content,
            redact=redact,
        )

    # Called as @capture(...)  return a decorator
    def decorator(func):
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
        )

    return decorator


#  Internal wrapper factory

def _make_wrapper(func, *, decision_type, context_version, max_input_chars,
                  max_output_chars, exporter, async_capture, capture_content,
                  redact):
    _dt = decision_type or func.__qualname__
    _exporter_obj = _RecordExporter(exporter=exporter, async_capture=async_capture)
    # Full mode renders through a bounded repr so oversized values cost
    # O(limit); hash mode keeps the exact full repr (its digests and char
    # counts are defined over it).
    if capture_content == "full":
        _input_render = _bounded_repr(max_input_chars)
        _output_render = _bounded_repr(max_output_chars)
    else:
        _input_render = _output_render = _safe_repr

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            # With no exporter configured anywhere, capture costs nothing:
            # no repr, no redact, no record.
            resolved = _exporter_obj._resolve_exporter()
            if resolved is None:
                return await func(*args, **kwargs)
            record = _build_record(_dt, context_version, args, kwargs, max_input_chars,
                                   func.__name__, capture_content, redact,
                                   render=_input_render)
            started_at = datetime.now(timezone.utc)
            try:
                result = await func(*args, **kwargs)
                _finalize_record(record, started_at, result=result,
                                 max_output_chars=max_output_chars,
                                 capture_content=capture_content, redact=redact,
                                 render=_output_render)
                _exporter_obj._trigger_export(record, exporter=resolved)
                return result
            except Exception as exc:
                _finalize_record(record, started_at, error=exc,
                                 max_output_chars=max_output_chars,
                                 capture_content=capture_content, redact=redact,
                                 render=_output_render)
                _exporter_obj._trigger_export(record, exporter=resolved)
                raise

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        resolved = _exporter_obj._resolve_exporter()
        if resolved is None:
            return func(*args, **kwargs)
        record = _build_record(_dt, context_version, args, kwargs, max_input_chars,
                               func.__name__, capture_content, redact,
                               render=_input_render)
        started_at = datetime.now(timezone.utc)
        try:
            result = func(*args, **kwargs)
            _finalize_record(record, started_at, result=result,
                             max_output_chars=max_output_chars,
                             capture_content=capture_content, redact=redact,
                             render=_output_render)
            _exporter_obj._trigger_export(record, exporter=resolved)
            return result
        except Exception as exc:
            _finalize_record(record, started_at, error=exc,
                             max_output_chars=max_output_chars,
                             capture_content=capture_content, redact=redact,
                             render=_output_render)
            _exporter_obj._trigger_export(record, exporter=resolved)
            raise

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
        "decision_id": str(uuid.uuid4()),
        "decision_type": decision_type,
        "function_name": function_name,
        "inputs": inputs,
        "outputs": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if context_version is not None:
        record["context_version"] = context_version
    return record


def _finalize_record(
    record: dict,
    started_at: datetime,
    *,
    result: Any = None,
    error: Optional[Exception] = None,
    max_output_chars: int = 1000,
    capture_content: str = "full",
    redact: Optional[Callable[[str], str]] = None,
    render: Callable[[Any], str] = _safe_repr,
) -> None:
    """Mutate record in-place with timing and output/error."""
    ended_at = datetime.now(timezone.utc)
    record["ended_at"] = ended_at.isoformat()
    record["execution_time_ms"] = (ended_at - started_at).total_seconds() * 1000

    if error is not None:
        if capture_content == "full":
            record["error"] = _full_text(str(error), redact, max_output_chars)
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
