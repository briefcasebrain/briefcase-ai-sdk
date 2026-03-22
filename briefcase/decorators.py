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
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from briefcase._export_mixin import ExportMixin


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
        - inputs (repr of positional + keyword args, truncated)
        - outputs (repr of return value, truncated)
        - error (if an exception was raised; exception is re-raised)
        - started_at / ended_at (ISO 8601)
        - execution_time_ms
        - context_version (if provided)

    Args:
        decision_type:   Override the decision_type field. Defaults to function.__qualname__.
        context_version: Optional version tag added to all records.
        max_input_chars: Maximum characters for the serialized input repr.
        max_output_chars: Maximum characters for the serialized output repr.
        exporter:        Briefcase exporter instance. Falls back to BriefcaseConfig.get().exporter.
        async_capture:   If True (default), export runs in a background thread.
    """
    if fn is not None:
        # Called as @capture (no arguments)  fn is the decorated function
        return _make_wrapper(
            fn,
            decision_type=None,
            context_version=None,
            max_input_chars=1000,
            max_output_chars=1000,
            exporter=None,
            async_capture=True,
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
        )

    return decorator


#  Internal wrapper factory 

def _make_wrapper(func, *, decision_type, context_version, max_input_chars,
                  max_output_chars, exporter, async_capture):
    _dt = decision_type or func.__qualname__
    _exporter_obj = _RecordExporter(exporter=exporter, async_capture=async_capture)

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            record = _build_record(_dt, context_version, args, kwargs, max_input_chars,
                                   func.__name__)
            started_at = datetime.now(timezone.utc)
            try:
                result = await func(*args, **kwargs)
                _finalize_record(record, started_at, result=result,
                                 max_output_chars=max_output_chars)
                _exporter_obj._trigger_export(record)
                return result
            except Exception as exc:
                _finalize_record(record, started_at, error=exc,
                                 max_output_chars=max_output_chars)
                _exporter_obj._trigger_export(record)
                raise

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        record = _build_record(_dt, context_version, args, kwargs, max_input_chars,
                               func.__name__)
        started_at = datetime.now(timezone.utc)
        try:
            result = func(*args, **kwargs)
            _finalize_record(record, started_at, result=result,
                             max_output_chars=max_output_chars)
            _exporter_obj._trigger_export(record)
            return result
        except Exception as exc:
            _finalize_record(record, started_at, error=exc,
                             max_output_chars=max_output_chars)
            _exporter_obj._trigger_export(record)
            raise

    return sync_wrapper


def _build_record(
    decision_type: str,
    context_version: Optional[str],
    args: tuple,
    kwargs: dict,
    max_input_chars: int,
    function_name: str,
) -> dict:
    """Build the initial decision record with inputs."""
    inputs: dict = {}
    if args:
        inputs["args"] = repr(args)[:max_input_chars]
    if kwargs:
        inputs["kwargs"] = repr(kwargs)[:max_input_chars]

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
) -> None:
    """Mutate record in-place with timing and output/error."""
    ended_at = datetime.now(timezone.utc)
    record["ended_at"] = ended_at.isoformat()
    record["execution_time_ms"] = (ended_at - started_at).total_seconds() * 1000

    if error is not None:
        record["error"] = str(error)
    else:
        record["outputs"] = {"result": repr(result)[:max_output_chars]}
