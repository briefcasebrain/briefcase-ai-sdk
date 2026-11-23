"""Shared machinery for provider auto-instrumentation.

Each module under ``briefcase.integrations.providers`` patches the call
paths of one model-provider SDK so that every call emits one decision
record. This module holds what they share:

- :class:`ProviderInstrumentor`: owns the patches of one provider (apply,
  verify, undo) and the capture options, and exports records through the
  same :class:`~briefcase._export_mixin.ExportMixin` pipeline ``@capture``
  uses.
- :class:`ProviderCall`: one in-flight call. The record is built with the
  ``@capture`` helpers (``_build_record`` / ``_finalize_record``), so it
  carries the same ``schema_version``, ids, timing and trace fields, plus
  the provider fields described below.
- :func:`instrument_stream`, :func:`tee` and :func:`atee`: wrap a streaming
  response so the record is emitted once, when the stream is exhausted,
  closed, fails, or is dropped.

The contract every wrapper here keeps:

- The wrapped call behaves as if it were not wrapped: same return object,
  same exception object, same laziness. Our own failures are logged at
  DEBUG and never reach the caller.
- One record per call, emitted exactly once (``ProviderCall.finish`` is
  guarded by a lock, so a stream closed from one thread while another
  finishes reading it still produces one record).
- A record never states something that was not observed. A token counter
  the provider did not report is ``None``, not ``0``; a stream that was not
  read to its end has ``status="incomplete"``; anything that could not be
  captured is named in ``capture_limits``.

Provider fields added to the ``@capture`` record layout:

- ``model_parameters``: ``{"provider", "model_name", ...scalar request
  parameters}``; ``requested_model`` when the served model differs.
- ``token_usage``: always present. ``None`` when the provider reported no
  usage; otherwise ``{"prompt_tokens", "completion_tokens",
  "total_tokens"}`` (each an int, or ``None`` when that counter was not
  reported) plus provider-specific counters.
- ``status``: ``"ok"``; ``"error"`` (with ``error``, ``error_type`` and
  ``http_status`` when the SDK error carries one); ``"cancelled"``
  (``asyncio.CancelledError``, ``KeyboardInterrupt``, ``SystemExit``);
  ``"incomplete"`` (a stream that ended before its terminal event, with no
  error seen: the consumer stopped reading or dropped it).
- ``finish_reason``, ``response_id``, ``request_id`` when the SDK exposes
  them.
- ``stream`` (bool). For streams ``outputs`` carries ``chunk_count`` and
  ``stream_completed`` (the iterator was exhausted), and the record
  ``time_to_first_chunk_ms`` (request start to the first chunk reaching the
  consumer) and ``time_to_stream_ms`` (request start to the SDK call
  returning the stream). ``execution_time_ms`` of a stream runs to the end
  of the stream, so it includes the consumer's own time between chunks.
- ``capture_limits``: list of short notes, present only when capture was
  degraded.
- ``parent_run_id`` / ``parent_framework`` when the call was made inside a
  framework LLM run (see :mod:`._correlation`).

Prompt and completion content is not recorded unless ``capture_content`` is
turned on.

Call hooks
----------

Budgets (:mod:`briefcase.budgets`) and content guardrails
(:mod:`briefcase.guardrails.content`) act on every instrumented call through
one small interface: :func:`add_call_hook`. A hook has two methods:

- ``before(ctx)``: runs after the record is started and before the request
  is sent. It may change the request through ``ctx`` (``ctx.set_model``,
  ``ctx.rewrite_request``), add record fields (``ctx.fields``) or stop the
  call by raising a :class:`CallBlocked` subclass, which reaches the
  application unchanged (the record is emitted with ``status="error"``).
- ``after(ctx, outcome)``: runs once, when the record is completed (response
  read, stream ended or abandoned, error). ``outcome`` is a
  :class:`CallOutcome`. Raising :class:`CallBlocked` here stops the result
  from reaching the application: a non-streaming call raises from the SDK
  method, a stream raises from its final ``next()``.

With no hook registered the wrappers read one module global and do nothing
else (the ``_hooks`` tuple is empty). With a hook registered, a call is
followed even when no exporter is configured; its record is then built but
not exported.
"""

from __future__ import annotations

import asyncio
import atexit
import functools
import gc
import importlib
import inspect
import os
import queue
import threading
import time
import weakref
from datetime import datetime, timezone
from typing import (
    Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union,
)

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.decorators import _build_record, _finalize_record
from briefcase.integrations.frameworks._capture_state import (  # noqa: F401
    CAPTURE_CONTENT_MODES,
    CaptureOptions,
    normalize_capture_content,
)
from briefcase.integrations.providers import _correlation

logger = get_logger(__name__)

# Marker set on every wrapper this package installs, so a target is never
# wrapped twice.
WRAPPER_FLAG = "__briefcase_provider_wrapper__"

DEFAULT_MAX_CONTENT_CHARS = 8000
#: Bound on all captured strings of one ``inputs`` / ``outputs`` value
#: together, as a multiple of the per-string bound.
TOTAL_CONTENT_FACTOR = 16
_MAX_DEPTH = 8
_MAX_ITEMS = 200
TRUNCATED = "<truncated>"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
STATUS_INCOMPLETE = "incomplete"

_MISSING = object()
#: Returned by a stream ``on_item`` callback to keep an item from the
#: consumer (see the openai ``stream_usage`` option).
SKIP = object()

OnItem = Callable[[Any], Any]
OnEnd = Callable[[bool, Optional[BaseException]], None]


def jsonable(value: Any, limit: int = DEFAULT_MAX_CONTENT_CHARS, _depth: int = 0,
             _budget: Optional[List[int]] = None) -> Any:
    """Bounded, JSON-safe copy of request or response content.

    Strings are cut at ``limit`` characters, and all strings together at
    ``limit * TOTAL_CONTENT_FACTOR`` (what is past that becomes
    ``"<truncated>"``). Bytes are replaced by their length, pydantic models
    are dumped, and anything else (file handles, clients, arbitrary
    objects) is named by its type rather than repr'd. Iterators are never
    consumed. Never raises.
    """
    if _budget is None:
        _budget = [max(0, limit) * TOTAL_CONTENT_FACTOR]
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if _budget[0] <= 0:
                return TRUNCATED
            text = value if len(value) <= limit else value[:limit] + "..."
            if len(text) > _budget[0]:
                text = text[:_budget[0]] + "..."
            _budget[0] -= len(text)
            return text
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<{len(value)} bytes>"
        if _depth >= _MAX_DEPTH:
            return f"<{type(value).__name__}>"
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= _MAX_ITEMS or _budget[0] <= 0:
                    out[TRUNCATED] = len(value) - index
                    break
                out[str(key)[:200]] = jsonable(item, limit, _depth + 1, _budget)
            return out
        if isinstance(value, (list, tuple)):
            items: List[Any] = []
            for index, item in enumerate(value):
                if index >= _MAX_ITEMS or _budget[0] <= 0:
                    items.append(TRUNCATED)
                    break
                items.append(jsonable(item, limit, _depth + 1, _budget))
            return items
        dump = getattr(value, "model_dump", None)
        if callable(dump) and not isinstance(value, type):
            return jsonable(dump(mode="json", exclude_none=True), limit, _depth + 1, _budget)
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, (str, int)) and not isinstance(enum_value, bool):
            return jsonable(enum_value, limit, _depth + 1, _budget)
    except Exception:
        pass
    return f"<{type(value).__name__}>"


def scalar_params(source: Any, names: Tuple[str, ...]) -> Dict[str, Any]:
    """Pick the scalar request parameters named in ``names`` out of a
    mapping or object. Non-scalars (and SDK "not given" sentinels) are
    skipped, so no content can ride along."""
    out: Dict[str, Any] = {}
    for name in names:
        try:
            value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        except Exception:
            continue
        if isinstance(value, (bool, int, float)):
            out[name] = value
        elif isinstance(value, str) and len(value) <= 200:
            out[name] = value
    return out


def read(obj: Any, *path: str) -> Any:
    """Follow attribute / mapping keys along ``path``; ``None`` when any hop
    is missing. Never raises."""
    current = obj
    for name in path:
        if current is None:
            return None
        try:
            if isinstance(current, dict):
                current = current.get(name)
            else:
                current = getattr(current, name, None)
        except Exception:
            return None
    return current


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return int(value)


def token_usage(
    prompt: Any = None,
    completion: Any = None,
    total: Any = None,
    **extras: Any,
) -> Optional[Dict[str, Optional[int]]]:
    """Normalize provider token counters onto ``prompt_tokens`` /
    ``completion_tokens`` / ``total_tokens``, keeping provider-specific
    counters in ``extras`` when they are numbers.

    ``None`` when the provider reported nothing at all. A counter that was
    not reported stays ``None``: it is never replaced by ``0``, and
    ``total_tokens`` is only computed when both sides are known."""
    prompt_i, completion_i, total_i = _int(prompt), _int(completion), _int(total)
    if prompt_i is None and completion_i is None and total_i is None:
        return None
    if total_i is None and prompt_i is not None and completion_i is not None:
        total_i = prompt_i + completion_i
    usage: Dict[str, Optional[int]] = {
        "prompt_tokens": prompt_i,
        "completion_tokens": completion_i,
        "total_tokens": total_i,
    }
    for key, value in extras.items():
        extra = _int(value)
        if extra is not None:
            usage[key] = extra
    return usage


def http_status_of(error: BaseException) -> Optional[int]:
    """HTTP status carried by an SDK error: ``status_code`` (openai,
    anthropic), botocore's ``response["ResponseMetadata"]`` or
    google-genai's ``code``."""
    for candidate in (
        getattr(error, "status_code", None),
        read(error, "response", "ResponseMetadata", "HTTPStatusCode"),
        getattr(error, "code", None),
    ):
        status = _int(candidate)
        if status is not None and 100 <= status <= 599:
            return status
    return None


# -- finishing a record from inside the garbage collector ---------------------
#
# An abandoned stream is usually part of a reference cycle (the SDK's stream
# object and its generator refer to each other), so its record is produced
# while the cyclic garbage collector runs, on whatever thread happened to
# allocate, at an arbitrary point of that thread's code. Exporting from there
# could re-enter a lock that thread already holds (the export queue's mutex,
# a lock inside an exporter) and hang the program. So a record completed
# during a collection is handed to a queue that is safe to use re-entrantly
# (``queue.SimpleQueue``) and exported by a small daemon thread.

_gc_state = threading.local()
_gc_users = 0
#: ``(instrumentor, record, exporter)`` to export, or ``(None, fn, None)`` to run.
_deferred: "queue.SimpleQueue[Tuple[Any, Any, Any]]" = queue.SimpleQueue()
_wake: "queue.SimpleQueue[Any]" = queue.SimpleQueue()
_drain_lock = threading.Lock()
_reaper_lock = threading.Lock()
_reaper: Optional[threading.Thread] = None
_reaper_pid = 0
_atexit_registered = False


def _gc_callback(phase: str, info: Dict[str, Any]) -> None:
    depth = getattr(_gc_state, "depth", 0)
    _gc_state.depth = depth + 1 if phase == "start" else max(0, depth - 1)


def in_gc() -> bool:
    """True while the calling thread is running a garbage collection."""
    return getattr(_gc_state, "depth", 0) > 0


def _watch_gc(enable: bool) -> None:
    global _gc_users
    with _reaper_lock:
        if enable:
            _gc_users += 1
            if _gc_callback not in gc.callbacks:
                gc.callbacks.append(_gc_callback)
        else:
            _gc_users = max(0, _gc_users - 1)
            if _gc_users == 0:
                try:
                    gc.callbacks.remove(_gc_callback)
                except ValueError:
                    pass


def _reaper_loop(wake: "queue.SimpleQueue[Any]") -> None:
    while True:
        wake.get()
        try:
            flush_abandoned()
        except Exception:  # pragma: no cover - flush_abandoned does not raise
            logger.debug("deferred provider export failed", exc_info=True)


def _ensure_reaper() -> None:
    """Start the thread that exports records completed during a garbage
    collection. Restarted in a forked child, where threads do not survive."""
    global _reaper, _reaper_pid, _atexit_registered
    pid = os.getpid()
    if _reaper is not None and _reaper_pid == pid and _reaper.is_alive():
        return
    with _reaper_lock:
        if _reaper is not None and _reaper_pid == pid and _reaper.is_alive():
            return
        thread = threading.Thread(
            target=_reaper_loop, args=(_wake,), name="briefcase-provider-finalizer", daemon=True,
        )
        thread.start()
        _reaper, _reaper_pid = thread, pid
        if not _atexit_registered:
            # atexit runs last-registered first. The export pipeline
            # registers its own drain when its worker starts; start it now
            # so that ours is registered after it and therefore runs before
            # it, while the pipeline is still accepting records.
            try:
                from briefcase import _export_mixin

                _export_mixin._ensure_worker()
            except Exception:
                logger.debug("could not pre-start the export worker", exc_info=True)
            atexit.register(flush_abandoned)
            _atexit_registered = True


def _after_fork_in_child() -> None:
    """A forked child has none of the parent's threads: forget the
    finalizer thread (the next stream starts one), recreate the locks it or
    another thread may have held at the fork, and drop the copied queue of
    records the parent will export itself."""
    global _deferred, _wake, _drain_lock, _reaper_lock, _reaper, _reaper_pid
    _deferred = queue.SimpleQueue()
    _wake = queue.SimpleQueue()
    _drain_lock = threading.Lock()
    _reaper_lock = threading.Lock()
    _reaper, _reaper_pid = None, 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def flush_abandoned() -> int:
    """Export the records of streams that were finalized by the garbage
    collector. Runs on the finalizer thread and at interpreter exit; call it
    yourself (after ``gc.collect()``) to make that deterministic in a test.
    Returns how many records were handed to the export pipeline."""
    count = 0
    if in_gc():  # never export from inside a collection
        return count
    with _drain_lock:
        while True:
            try:
                inst, record, exporter = _deferred.get_nowait()
            except queue.Empty:
                break
            try:
                if inst is None:
                    record()  # a call finished by the collector: hooks, then export
                else:
                    inst._trigger_export(record, exporter=exporter)
                count += 1
            except Exception:
                logger.debug("deferred provider export failed", exc_info=True)
    return count


# -- call hooks ---------------------------------------------------------------


class CallBlocked(Exception):
    """Raised by a call hook to stop a provider call. Unlike a capture
    failure, it reaches the application. ``record_effect`` names the
    decision for the record."""


#: Registered hooks, in registration order. Replaced (never mutated) so the
#: wrappers can read it without a lock.
_hooks: Tuple[Any, ...] = ()
_hooks_lock = threading.Lock()


def add_call_hook(hook: Any) -> None:
    """Register a hook (an object with ``before(ctx)`` and
    ``after(ctx, outcome)``). Registering the same object twice is a no-op."""
    global _hooks
    with _hooks_lock:
        if any(h is hook for h in _hooks):
            return
        _hooks = _hooks + (hook,)


def remove_call_hook(hook: Any) -> None:
    """Unregister a hook; unknown hooks are ignored."""
    global _hooks
    with _hooks_lock:
        _hooks = tuple(h for h in _hooks if h is not hook)


def call_hooks() -> Tuple[Any, ...]:
    """The registered hooks."""
    return _hooks


_instrumentors: "weakref.WeakSet[Any]" = weakref.WeakSet()


def active_providers() -> List[str]:
    """Providers with at least one patched call path right now (what the
    call hooks can see)."""
    return sorted({i.provider for i in list(_instrumentors) if i.active and i.patched})


class CallOutcome:
    """What ``after`` hooks see about a finished call.

    ``sent``: the request was handed to the SDK (False when a ``before``
    hook stopped it). ``completed``: the whole response was received (a
    stream read to its terminal event). ``usage``: the record's
    ``token_usage`` (None when the provider reported none). ``info``: the
    provider module's response summary; an ``after`` hook may change
    ``info["content"]`` (its ``"content"`` text) to redact what the record
    keeps. ``text``: the completion text, only when a hook set
    ``ctx.wants_text``."""

    __slots__ = ("sent", "completed", "usage", "model", "error", "info", "stream", "http_status")

    def __init__(self, *, sent: bool, completed: bool, usage: Any, model: Optional[str],
                 error: Optional[BaseException], info: Dict[str, Any], stream: bool) -> None:
        self.sent = sent
        self.completed = completed
        self.usage = usage
        self.model = model
        self.error = error
        self.info = info
        self.stream = stream
        self.http_status = http_status_of(error) if error is not None else None

    @property
    def text(self) -> Optional[str]:
        content = self.info.get("content")
        if isinstance(content, dict):
            content = content.get("content")
        return content if isinstance(content, str) else None

    def set_text(self, text: str) -> None:
        content = self.info.get("content")
        if isinstance(content, dict):
            self.info["content"] = dict(content, content=text)
        elif isinstance(content, str):
            self.info["content"] = text


class CallContext:
    """One provider call as the hooks see it.

    ``kwargs`` is the wrapper's own copy of the request keyword arguments
    (the caller's dict is never changed); hooks change it only through
    :meth:`set_model` and :meth:`rewrite_request`, which copy what they
    change instead of mutating the caller's objects."""

    def __init__(self, spec: Any, kwargs: Dict[str, Any], is_async: bool,
                 hooks: Tuple[Any, ...]) -> None:
        self.spec = spec
        self.provider: str = spec.provider
        self.operation: str = spec.operation
        self.kwargs = kwargs
        self.is_async = is_async
        self.hooks = hooks
        self.stream: bool = bool(spec.is_stream(kwargs))
        #: Added to the record (``record.update``) when it is completed.
        self.fields: Dict[str, Any] = {}
        #: Per-hook private state, keyed by the hook's own name.
        self.state: Dict[str, Any] = {}
        #: Set by a hook that reads the completion text (``outcome.text``).
        self.wants_text = False
        self.text_limit = 1_000_000
        #: Called with the text received so far on each stream chunk; may
        #: raise CallBlocked to stop the stream early.
        self.chunk_check: Optional[Callable[[str], None]] = None
        #: ``str -> str`` applied to the text of the returned (non-stream)
        #: SDK object in place, after the record is complete.
        self.output_rewrite: Optional[Callable[[str], str]] = None
        self.pending: Optional[BaseException] = None
        self.sent = False
        self.request_rewritten = False
        self._done = False
        self._lock = threading.Lock()

    # -- request ------------------------------------------------------------

    @property
    def model(self) -> Optional[str]:
        return self.spec.get_model(self.kwargs)

    def set_model(self, model: str) -> None:
        self.spec.set_model(self.kwargs, model)

    def max_output_tokens(self) -> Optional[int]:
        return self.spec.max_output_tokens(self.kwargs)

    def request_texts(self) -> List[Tuple[str, str]]:
        """``[(source, text)]`` for every text in the request; source is one
        of ``system``, ``user``, ``assistant``, ``tool``, ``retrieved``."""
        return self.spec.request_texts(self.kwargs)

    def rewrite_request(self, edit: Callable[[str, str], Optional[str]]) -> bool:
        """Replace request texts: ``edit(text, source)`` returns the new text
        or None to keep it. False (and nothing changed) when some content
        could not be rewritten without changing its type."""
        ok = self.spec.rewrite_request(self.kwargs, edit)
        if ok:
            self.request_rewritten = True
        return ok

    def response_redactable(self) -> bool:
        """Whether the returned object's text can be replaced in place."""
        return (not self.stream) and bool(self.spec.response_redactable(self.kwargs))

    # -- dispatch -------------------------------------------------------------

    def run_before(self) -> None:
        for hook in self.hooks:
            try:
                hook.before(self)
            except CallBlocked:
                raise
            except Exception:
                logger.warning("briefcase call hook %r failed before the call; continuing",
                               type(hook).__name__, exc_info=True)

    def complete(self, info: Dict[str, Any], error: Optional[BaseException], completed: bool,
                 model: Optional[str], stream: bool) -> None:
        """Run every ``after`` hook, once."""
        with self._lock:
            if self._done:
                return
            self._done = True
        usage = info.get("usage")
        outcome = CallOutcome(
            sent=self.sent, completed=completed and error is None and self.sent,
            usage=usage if isinstance(usage, dict) and usage else None,
            model=model, error=error, info=info, stream=stream,
        )
        for hook in self.hooks:
            try:
                hook.after(self, outcome)
            except CallBlocked as exc:
                if self.pending is None:
                    self.pending = exc
            except Exception:
                logger.warning("briefcase call hook %r failed after the call",
                               type(hook).__name__, exc_info=True)

    def after_result(self, result: Any) -> Any:
        """For a non-stream result: apply ``output_rewrite`` to the returned
        object in place, then raise a pending :class:`CallBlocked`."""
        if self.output_rewrite is not None and not self.stream:
            try:
                self.spec.redact_response(result, self.output_rewrite)
            except Exception:
                logger.warning("briefcase could not redact the response", exc_info=True)
        self.raise_pending()
        return result

    def raise_pending(self) -> None:
        pending, self.pending = self.pending, None
        if pending is not None:
            raise pending


def _hooked(call: Optional["ProviderCall"], spec: Any, kwargs: Dict[str, Any],
            is_async: bool) -> Optional[CallContext]:
    """Start the hooks for a call that is about to be sent. Raises the
    hook's :class:`CallBlocked` after the call's record is finished."""
    hooks = _hooks
    if not hooks or spec is None or call is None:
        return None
    try:
        ctx = CallContext(spec, kwargs, is_async, hooks)
    except Exception:
        logger.debug("call hooks could not start", exc_info=True)
        return None
    call.attach(ctx)
    try:
        ctx.run_before()
    except CallBlocked as exc:
        _fail(call, exc)
        raise
    call.request_changed(ctx)
    ctx.sent = True
    return ctx


class ProviderCall:
    """One provider call in flight. ``finish`` builds the rest of the record
    and exports it exactly once; it never raises."""

    __slots__ = (
        "_inst", "_record", "_started_at", "_exporter", "_model", "_params",
        "_stream", "_once", "_t0", "_first_chunk_ms", "_stream_ms", "_scope",
        "_abandon", "limits", "_pid", "_inputs", "ctx", "__weakref__",
    )

    def __init__(
        self,
        inst: "ProviderInstrumentor",
        record: Dict[str, Any],
        started_at: datetime,
        exporter: Any,
        model: Optional[str],
        params: Dict[str, Any],
        stream: bool,
        scope: Optional[_correlation.LLMRunScope] = None,
        inputs: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._inst = inst
        self._inputs = inputs
        #: The call hooks' context, when hooks are registered.
        self.ctx: Optional[CallContext] = None
        self._record = record
        self._started_at = started_at
        self._exporter = exporter
        self._model = model
        self._params = params
        self._stream = stream
        self._once = threading.Lock()
        self._t0 = time.perf_counter()
        self._first_chunk_ms: Optional[float] = None
        self._stream_ms: Optional[float] = None
        self._scope = scope
        #: The process the call was made in. A forked child holds a copy of
        #: every call in flight at the fork; those belong to the parent and
        #: must not produce a second record from the child.
        self._pid = os.getpid()
        #: Called (once) when the call is dropped unfinished; set by whoever
        #: knows more about the call than ``__del__`` does.
        self._abandon: Optional[Callable[[], None]] = None
        #: Notes for the record's ``capture_limits``.
        self.limits: List[str] = []

    @property
    def done(self) -> bool:
        return self._once.locked()

    @property
    def is_stream(self) -> bool:
        return self._stream

    @property
    def want_content(self) -> bool:
        """Whether the provider module should read response text: content
        capture is on, or a call hook reads the completion."""
        ctx = self.ctx
        return self._inst.captures_content or (ctx is not None and ctx.wants_text)

    @property
    def text_limit(self) -> int:
        ctx = self.ctx
        if ctx is not None and ctx.wants_text:
            return max(self._inst.max_content_chars, ctx.text_limit)
        return self._inst.max_content_chars

    def attach(self, ctx: CallContext) -> None:
        self.ctx = ctx

    def request_changed(self, ctx: CallContext) -> None:
        """A hook changed the request: record the model actually requested
        and, with content capture on, the request actually sent."""
        try:
            model = ctx.model
            if isinstance(model, str) and model and model != self._model:
                self._model = model
            if ctx.request_rewritten and self._inputs is not None and self._inst.captures_content:
                content = jsonable(self._inputs(), self._inst.max_content_chars)
                self._record["inputs"] = self._inst.capture_options.content(content)
        except Exception:
            logger.debug("provider record could not follow the changed request", exc_info=True)

    def mark_stream(self) -> None:
        """Flag the call as streaming when that is only known from the
        result (not from the request)."""
        self._stream = True

    def stream_opened(self) -> None:
        """The SDK call returned its stream object."""
        if self._stream_ms is None:
            self._stream_ms = (time.perf_counter() - self._t0) * 1000

    def first_chunk(self) -> None:
        if self._first_chunk_ms is None:
            self._first_chunk_ms = (time.perf_counter() - self._t0) * 1000

    def limit(self, note: str) -> None:
        """Add a ``capture_limits`` note (once)."""
        if note and note not in self.limits:
            self.limits.append(note)

    def on_abandon(self, callback: Optional[Callable[[], None]]) -> None:
        self._abandon = callback

    def finish(
        self,
        info: Optional[Dict[str, Any]] = None,
        *,
        error: Optional[BaseException] = None,
        stream: Optional[Dict[str, Any]] = None,
        finished: bool = True,
    ) -> None:
        """Complete and export the record.

        ``info`` is what the provider module extracted from the response:
        any of ``model``, ``response_id``, ``request_id``, ``finish_reason``,
        ``usage`` (a :func:`token_usage` dict), ``content`` (outputs, kept
        only when content capture is on), ``limits`` (``capture_limits``
        notes) and ``extra`` (top-level fields). ``stream`` is
        ``{"chunk_count", "stream_completed"}`` for streams; ``finished``
        says whether the stream's terminal event was observed.
        """
        if not self._once.acquire(False):
            return
        self._abandon = None
        if self._pid != os.getpid():
            return  # a copy inherited through fork: the parent records it
        if self.ctx is not None and in_gc():
            # The call hooks touch stores and locks (a budget settles); like
            # the export, run them from the finalizer thread, never from
            # inside a collection.
            _ensure_reaper()
            _deferred.put((None, functools.partial(
                self._finish_quietly, info or {}, error, stream, finished), None))
            _wake.put(None)
            return
        self._finish_quietly(info or {}, error, stream, finished)

    def _finish_quietly(self, info: Dict[str, Any], error: Optional[BaseException],
                        stream: Optional[Dict[str, Any]], finished: bool) -> None:
        try:
            self._finish(info, error, stream, finished)
        except Exception:
            logger.debug("provider record could not be completed", exc_info=True)

    def _finish(
        self,
        info: Dict[str, Any],
        error: Optional[BaseException],
        stream: Optional[Dict[str, Any]],
        finished: bool,
    ) -> None:
        inst = self._inst
        record = self._record
        options = inst.capture_options
        mode = options.capture_content

        ctx = self.ctx
        if ctx is not None:
            done = error is None and (
                stream is None or bool(stream.get("stream_completed")) or finished)
            ctx.complete(info, error, done, info.get("model") or self._model, self._stream)

        if error is not None:
            _finalize_record(
                record, self._started_at, error=error,
                max_output_chars=inst.max_content_chars,
                capture_content=mode, redact=options.redact,
            )
            if isinstance(error, asyncio.CancelledError) or not isinstance(error, Exception):
                # CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit:
                # the call was interrupted, the provider did not fail.
                record["status"] = STATUS_CANCELLED
            else:
                record["status"] = STATUS_ERROR
            record["error_type"] = type(error).__name__
            http_status = http_status_of(error)
            if http_status is not None:
                record["http_status"] = http_status
            if not info.get("request_id"):
                request_id = getattr(error, "request_id", None) or read(
                    error, "response", "ResponseMetadata", "RequestId"
                )
                if isinstance(request_id, str):
                    info = dict(info, request_id=request_id)
        else:
            _finalize_record(record, self._started_at, capture_content="none")
            completed = stream is None or bool(stream.get("stream_completed")) or finished
            record["status"] = STATUS_OK if completed else STATUS_INCOMPLETE

        outputs: Any = {}
        content = info.get("content")
        if content and mode != "none":
            outputs = options.content(jsonable(content, inst.max_content_chars))
        if not isinstance(outputs, dict):
            outputs = {"content": outputs}
        if stream is not None:
            outputs.update(stream)
        record["outputs"] = outputs

        model = info.get("model") or self._model
        parameters: Dict[str, Any] = {"provider": inst.provider}
        if model:
            parameters["model_name"] = str(model)[:200]
        if self._model and model and str(self._model) != str(model):
            parameters["requested_model"] = str(self._model)[:200]
        parameters.update(self._params)
        record["model_parameters"] = parameters

        usage = info.get("usage")
        record["token_usage"] = usage if isinstance(usage, dict) and usage else None
        for key in ("finish_reason", "response_id", "request_id"):
            value = info.get(key)
            if value is not None:
                record[key] = str(value)[:200]
        record["stream"] = bool(self._stream)
        if self._first_chunk_ms is not None:
            record["time_to_first_chunk_ms"] = self._first_chunk_ms
        if self._stream and self._stream_ms is not None:
            record["time_to_stream_ms"] = self._stream_ms
        extra = info.get("extra")
        if isinstance(extra, dict):
            for key, value in extra.items():
                if value is not None:
                    record.setdefault(key, value)

        limits = list(self.limits)
        for note in info.get("limits") or ():
            if note not in limits:
                limits.append(note)
        if limits:
            record["capture_limits"] = limits
        if ctx is not None and ctx.fields:
            record.update(ctx.fields)

        if self._exporter is None:
            return  # followed for the call hooks only: nothing is exporting
        scope = self._scope
        if scope is not None:
            scope.provider_records += 1

        if in_gc():
            # Completed by the garbage collector: export from the finalizer
            # thread, never from here (see the note above flush_abandoned).
            _deferred.put((inst, record, self._exporter))
            _wake.put(None)
        else:
            inst._trigger_export(record, exporter=self._exporter)

    def __del__(self) -> None:
        # Backstop for a call whose stream was dropped without its
        # generator ever being closed (an async generator after its event
        # loop is gone, or a cycle the collector finalizes in another
        # order): the record still goes out, marked incomplete.
        try:
            if self._once.locked():
                return
            abandon = self._abandon
            if abandon is not None:
                abandon()
            if not self._once.locked() and self._stream:
                self.finish(
                    stream={"chunk_count": 0, "stream_completed": False}, finished=False,
                )
        except Exception:
            pass


class PatchTarget:
    """One attribute to replace: ``module.owner.attr`` with
    ``factory(original)``. ``requires`` names parameters the original must
    accept (by name or through ``**kwargs``) for the wrapper to read the
    request; a target that lost one is treated as changed, and skipped."""

    __slots__ = ("module", "owner", "attr", "factory", "requires")

    def __init__(self, module: str, owner: Optional[str], attr: str,
                 factory: Callable[[Any], Any], requires: Sequence[str] = ()) -> None:
        self.module = module
        self.owner = owner
        self.attr = attr
        self.factory = factory
        self.requires = tuple(requires)

    @property
    def label(self) -> str:
        return ".".join(p for p in (self.module, self.owner, self.attr) if p)


def _accepts(fn: Any, names: Sequence[str]) -> Optional[str]:
    """The first of ``names`` that ``fn`` does not accept, else None. A
    callable whose signature cannot be read is given the benefit of the
    doubt."""
    if not names:
        return None
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return None
    for name in names:
        if name not in parameters:
            return name
    return None


class ProviderInstrumentor(ExportMixin):
    """Patches one provider SDK and exports a decision record per call.

    Args:
        provider: Provider name recorded as ``model_parameters.provider``.
        exporter: Exporter for the records. Falls back to the global one
            (``briefcase.observe`` / ``briefcase.setup``); with neither, the
            wrappers add no work to a call.
        context_version: Optional version tag added to every record.
        async_capture: ``True`` (default) exports on the shared background
            queue; ``False`` exports inline.
        capture_content: ``False`` (default) records no prompt or completion
            content. ``True`` (or ``"full"``) records it, bounded by
            ``max_content_chars`` per string; ``"hash"`` records a SHA-256
            digest and a character count instead. In the non-full modes the
            ``error`` field is the exception class name, never the message.
        redact: Optional ``str -> str`` hook applied to every captured string
            in ``"full"`` mode (fails closed, like the framework handlers).
        max_content_chars: Bound on each captured string (and, times
            ``TOTAL_CONTENT_FACTOR``, on one whole ``inputs`` / ``outputs``).
    """

    def __init__(
        self,
        provider: str,
        *,
        exporter: Any = None,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        capture_content: Union[bool, str, None] = False,
        redact: Optional[Callable[[str], str]] = None,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    ) -> None:
        self.provider = provider
        self._exporter = exporter
        self.context_version = context_version
        self.async_capture = async_capture
        self.capture_options = CaptureOptions(
            normalize_capture_content(capture_content), redact
        )
        self.max_content_chars = max_content_chars
        self.sdk_version: Optional[str] = None
        #: Versions this module's patches were exercised against.
        self.tested_versions: Tuple[str, ...] = ()
        #: Versions reported working elsewhere, not re-run by the test-suite.
        self.reported_versions: Tuple[str, ...] = ()
        #: False once uninstalled: every wrapper then passes straight
        #: through, including wrappers that could not be removed (another
        #: library patched over them) or that an SDK object captured before
        #: the undo (``client.chat.completions.with_raw_response``).
        self.active = True
        self._undos: List[Tuple[Any, str, Any, Any]] = []
        self.patched: List[str] = []
        #: label -> reason, for every target (or group of targets) left alone.
        self.skipped: Dict[str, str] = {}
        #: Known gaps of this provider's capture, for ``auto.status()``.
        self.limits: List[str] = []
        #: Provider-specific options in effect, for ``auto.status()``.
        self.options: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._warned = False
        _watch_gc(True)
        _instrumentors.add(self)

    @property
    def captures_content(self) -> bool:
        return self.capture_options.capture_content != "none"

    # -- patching ---------------------------------------------------------

    def _resolve(self, target: PatchTarget) -> Tuple[Any, Any]:
        """``(owner, original)`` of a target, or raise with the reason."""
        owner: Any = importlib.import_module(target.module)
        if target.owner:
            owner = getattr(owner, target.owner)
        original = inspect.getattr_static(owner, target.attr)
        if getattr(original, WRAPPER_FLAG, False):
            raise RuntimeError("already instrumented by briefcase")
        if not callable(original):
            raise TypeError("not callable")
        missing = _accepts(original, target.requires)
        if missing is not None:
            raise TypeError(f"signature changed: no {missing!r} parameter")
        return owner, original

    def patch_group(self, name: str, targets: Sequence[PatchTarget]) -> bool:
        """Apply a group of targets all-or-nothing.

        Every target is resolved and checked first; if one is missing, not
        callable, already wrapped, or lost a parameter the wrapper reads,
        *none* of the group is patched and the group is listed in
        ``skipped`` with the reason. So a surface (say, the sync and async
        ``create`` and ``parse`` of one resource) is either fully recorded
        or untouched, never half. Never raises."""
        resolved: List[Tuple[PatchTarget, Any, Any]] = []
        for target in targets:
            try:
                owner, original = self._resolve(target)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                if isinstance(exc, (ImportError, AttributeError)):
                    reason = f"not found ({type(exc).__name__})"
                self.skipped[name] = f"{target.label}: {reason}"
                return False
            resolved.append((target, owner, original))

        applied: List[Tuple[Any, str, Any, Any]] = []
        try:
            for target, owner, original in resolved:
                wrapper = target.factory(original)
                setattr(wrapper, WRAPPER_FLAG, True)
                own = vars(owner).get(target.attr, _MISSING)
                setattr(owner, target.attr, wrapper)
                applied.append((owner, target.attr, own, wrapper))
        except Exception as exc:
            for owner, attr, own, _wrapper in reversed(applied):
                try:
                    if own is _MISSING:
                        delattr(owner, attr)
                    else:
                        setattr(owner, attr, own)
                except Exception:  # pragma: no cover - restoring what we just set
                    pass
            self.skipped[name] = f"could not patch ({type(exc).__name__}: {exc})"
            return False
        with self._lock:
            self._undos.extend(applied)
            self.patched.extend(target.label for target, _, _ in resolved)
        return True

    def patch(
        self,
        module_name: str,
        owner_name: Optional[str],
        attr: str,
        factory: Callable[[Any], Any],
        requires: Sequence[str] = (),
    ) -> bool:
        """Patch a single target (a group of one). Returns False when it
        was skipped; the reason is in ``skipped``."""
        target = PatchTarget(module_name, owner_name, attr, factory, requires)
        return self.patch_group(target.label, [target])

    def patch_object(self, owner: Any, attr: str, factory: Callable[[Any], Any],
                     label: str) -> bool:
        """Patch an attribute of a class found at run time (not by import
        path). Idempotent per class; undone by :meth:`uninstall`."""
        with self._lock:
            try:
                original = inspect.getattr_static(owner, attr)
                if getattr(original, WRAPPER_FLAG, False):
                    return True
                if not callable(original):
                    raise TypeError("not callable")
                wrapper = factory(original)
                setattr(wrapper, WRAPPER_FLAG, True)
                own = vars(owner).get(attr, _MISSING)
                setattr(owner, attr, wrapper)
            except Exception as exc:
                self.skipped.setdefault(label, f"could not patch ({type(exc).__name__}: {exc})")
                return False
            self._undos.append((owner, attr, own, wrapper))
            self.patched.append(label)
            return True

    def report_skipped(self, verified: Optional[str] = None) -> None:
        """Log ONE warning naming everything that was skipped."""
        if not self.skipped or self._warned:
            return
        self._warned = True
        details = "; ".join(f"{name} [{reason}]" for name, reason in self.skipped.items())
        message = (
            f"briefcase.auto[{self.provider}]: the installed SDK (version "
            f"{self.sdk_version}; tested with "
            f"{verified or ', '.join(self.tested_versions) or 'unknown'}) does not match "
            "the expected layout, so these call paths are NOT recorded and were left "
            f"untouched: {details}. See briefcase.auto.status()."
        )
        logger.warning(message)
        try:
            # The library's logger is silent unless the application set up
            # logging; a capture gap must be visible regardless.
            import warnings

            warnings.warn(message, RuntimeWarning, stacklevel=4)
        except Exception:
            pass

    def uninstall(self) -> None:
        """Restore every patched attribute to the object it held before.

        An attribute that no longer holds our wrapper (another library
        patched the same method after us) is left alone, with a WARNING:
        restoring it would silently remove that library's patch. Our
        wrapper stays underneath theirs but is switched off."""
        self.active = False
        kept: List[str] = []
        with self._lock:
            undos, self._undos = self._undos, []
            self.patched.clear()
        for owner, attr, original, wrapper in reversed(undos):
            try:
                current = vars(owner).get(attr, _MISSING)
                if current is not wrapper:
                    kept.append(f"{getattr(owner, '__qualname__', owner)}.{attr}")
                    continue
                if original is _MISSING:
                    delattr(owner, attr)
                else:
                    setattr(owner, attr, original)
            except Exception as exc:
                logger.debug("provider undo: ignored error: %s", exc)
        if kept:
            logger.warning(
                "briefcase.auto[%s]: %s was patched again by another library after "
                "briefcase; undo left that patch in place. The briefcase wrapper "
                "beneath it is disabled and records nothing.",
                self.provider, ", ".join(kept),
            )
        _watch_gc(False)

    # -- capture ----------------------------------------------------------

    def begin(
        self,
        decision_type: str,
        function_name: str,
        *,
        model: Any = None,
        params: Optional[Dict[str, Any]] = None,
        inputs: Optional[Callable[[], Any]] = None,
        stream: bool = False,
    ) -> Optional[ProviderCall]:
        """Start a record for a call, or return None when nothing is
        exporting (the call then runs untouched). ``inputs`` is only called
        when content capture is on. Never raises."""
        try:
            if not self.active:
                return None
            exporter = self._resolve_exporter()
            if exporter is None and not _hooks:
                return None
            scope = _correlation.current_llm_run()
            if scope is not None and _correlation.get_dedupe() == "provider":
                # The framework handler records this call; the provider
                # record would count its tokens a second time.
                if not _hooks:
                    return None
                exporter = None  # followed for the call hooks only
            record = _build_record(
                decision_type, self.context_version, (), {}, 0, function_name, "none",
            )
            started_at = datetime.fromisoformat(record["started_at"])
            if started_at.tzinfo is None:  # pragma: no cover - always aware
                started_at = started_at.replace(tzinfo=timezone.utc)
            if scope is not None:
                record["parent_run_id"] = scope.run_id
                record["parent_framework"] = scope.framework
            if inputs is not None and self.captures_content:
                try:
                    content = jsonable(inputs(), self.max_content_chars)
                    record["inputs"] = self.capture_options.content(content)
                except Exception:
                    logger.debug("provider inputs could not be captured", exc_info=True)
            if stream:
                _ensure_reaper()
            return ProviderCall(
                self, record, started_at, exporter,
                str(model) if isinstance(model, str) and model else None,
                dict(params or {}), stream, scope, inputs,
            )
        except Exception:
            logger.debug("provider capture could not start", exc_info=True)
            return None


# -- stream wrapping --------------------------------------------------------

def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Make a capture callback unable to raise into the user's iteration,
    except for a call hook's :class:`CallBlocked`, which is meant to."""
    def guarded(*args: Any) -> Any:
        try:
            return fn(*args)
        except CallBlocked:
            raise
        except Exception:
            logger.debug("provider stream callback failed", exc_info=True)
            return None
    return guarded


def tee(inner: Iterator[Any], on_item: OnItem, on_end: OnEnd) -> Iterator[Any]:
    """Yield from ``inner``, reporting each item, then report the end once:
    ``on_end(True, None)`` on exhaustion, ``on_end(False, exc)`` on error,
    ``on_end(False, None)`` when the consumer stops early. An item for which
    ``on_item`` returns :data:`SKIP` is not passed on. One item is pulled
    per item requested: nothing is buffered."""
    on_item, on_end = _guard(on_item), _guard(on_end)
    completed = False
    error: Optional[BaseException] = None
    try:
        for item in inner:
            if on_item(item) is SKIP:
                continue
            yield item
        completed = True
    except GeneratorExit:
        raise
    except BaseException as exc:
        error = exc
        if isinstance(exc, CallBlocked):
            _close_quietly(inner)
        raise
    finally:
        on_end(completed, error)
        # Do not keep the exception (and through its traceback, this frame
        # and the stream) alive in a reference cycle.
        error = None


async def atee(inner: AsyncIterator[Any], on_item: OnItem, on_end: OnEnd) -> AsyncIterator[Any]:
    """Async counterpart of :func:`tee`."""
    on_item, on_end = _guard(on_item), _guard(on_end)
    completed = False
    error: Optional[BaseException] = None
    try:
        async for item in inner:
            if on_item(item) is SKIP:
                continue
            yield item
        completed = True
    except GeneratorExit:
        raise
    except BaseException as exc:
        error = exc
        if isinstance(exc, CallBlocked):
            aclose = getattr(inner, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    logger.debug("stream could not be closed after a block", exc_info=True)
        raise
    finally:
        on_end(completed, error)
        error = None


def _close_quietly(inner: Any) -> None:
    """Close the SDK's iterator after a hook stopped the stream, so the
    connection is released."""
    close = getattr(inner, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("stream could not be closed after a block", exc_info=True)


class StreamProxy:
    """Iterator wrapper for a stream object that cannot be instrumented in
    place (botocore's ``EventStream``). Everything except iteration and
    closing is delegated, and ``isinstance`` checks against the wrapped
    object's class still pass (``__class__`` reports it)."""

    def __init__(self, inner: Any, on_item: OnItem, on_end: OnEnd) -> None:
        self._bc_inner = inner
        self._bc_on_end = _guard(on_end)
        self._bc_iter: Optional[Iterator[Any]] = None
        self._bc_on_item = on_item

    @property  # type: ignore[misc]
    def __class__(self) -> type:  # noqa: D105 - isinstance() transparency
        return type(self._bc_inner)

    def _bc_start(self) -> Iterator[Any]:
        if self._bc_iter is None:
            self._bc_iter = tee(iter(self._bc_inner), self._bc_on_item, self._bc_on_end)
        return self._bc_iter

    def __iter__(self) -> Any:
        self._bc_start()
        return self

    def __next__(self) -> Any:
        return next(self._bc_start())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bc_inner, name)

    def __enter__(self) -> Any:
        enter = getattr(self._bc_inner, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        self.close()
        return None

    def close(self) -> None:
        try:
            close = getattr(self._bc_inner, "close", None)
            if callable(close):
                close()
        finally:
            self._bc_on_end(False, None)


class AsyncStreamProxy:
    """Async counterpart of :class:`StreamProxy`."""

    def __init__(self, inner: Any, on_item: OnItem, on_end: OnEnd) -> None:
        self._bc_inner = inner
        self._bc_on_end = _guard(on_end)
        self._bc_iter: Optional[AsyncIterator[Any]] = None
        self._bc_on_item = on_item

    @property  # type: ignore[misc]
    def __class__(self) -> type:  # noqa: D105 - isinstance() transparency
        return type(self._bc_inner)

    def _bc_start(self) -> AsyncIterator[Any]:
        if self._bc_iter is None:
            self._bc_iter = atee(self._bc_inner.__aiter__(), self._bc_on_item, self._bc_on_end)
        return self._bc_iter

    def __aiter__(self) -> Any:
        self._bc_start()
        return self

    async def __anext__(self) -> Any:
        return await self._bc_start().__anext__()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bc_inner, name)

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        await self.close()
        return None

    async def close(self) -> None:
        try:
            close = getattr(self._bc_inner, "close", None) or getattr(
                self._bc_inner, "aclose", None
            )
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        finally:
            self._bc_on_end(False, None)

    async def aclose(self) -> None:
        await self.close()


def instrument_stream(stream: Any, on_item: OnItem, on_end: OnEnd, *, is_async: bool,
                      proxy: bool = True) -> Any:
    """Make ``stream`` report its items and its end.

    The Stainless-generated SDKs (openai, anthropic) return a ``Stream`` /
    ``AsyncStream`` object that iterates a private ``_iterator`` generator.
    When that attribute is present the generator is swapped for a reporting
    one and ``close`` is wrapped on the instance, so the caller gets back
    the *same* object (``isinstance`` checks, ``.response`` and context
    manager use keep working).

    Otherwise: with ``proxy=True`` the stream is wrapped in a proxy; with
    ``proxy=False`` ``None`` is returned and the caller hands the stream
    back untouched (recording that it could not be observed), because for
    those SDKs a proxy would change the type the application receives.

    ``on_end`` may be called more than once (exhaustion, then ``close``);
    callers pass an idempotent function.
    """
    try:
        inner = stream._iterator
        if inner is None or not (hasattr(inner, "__anext__") if is_async
                                 else hasattr(inner, "__next__")):
            raise AttributeError("_iterator is not an iterator")
        wrapped = (atee if is_async else tee)(inner, on_item, on_end)
        stream._iterator = wrapped
        if stream._iterator is not wrapped:
            raise AttributeError("_iterator is not assignable")
    except Exception:
        if not proxy:
            return None
        proxy_cls = AsyncStreamProxy if is_async else StreamProxy
        return proxy_cls(stream, on_item, on_end)

    # A stream closed before it was exhausted never resumes the generator,
    # so close() reports the end itself. The closure holds the stream weakly:
    # a strong reference would form another cycle and delay the SDK's own
    # cleanup of a dropped stream until the garbage collector runs.
    guarded_end = _guard(on_end)
    ref = weakref.ref(stream)
    try:
        if is_async:
            async def aclose_and_report() -> None:
                target = ref()
                try:
                    if target is not None:
                        await type(target).close(target)
                finally:
                    guarded_end(False, None)

            stream.close = aclose_and_report
        else:
            def close_and_report() -> None:
                target = ref()
                try:
                    if target is not None:
                        type(target).close(target)
                finally:
                    guarded_end(False, None)

            stream.close = close_and_report
    except Exception:
        logger.debug("could not wrap stream.close", exc_info=True)
    return stream


class StreamState:
    """Counts chunks and finishes a call when its stream ends. Provider
    modules subclass it and override :meth:`observe` and :meth:`info`.

    ``finished`` is set by ``observe`` when the provider's terminal event
    was seen (a finish reason, ``message_stop``): a consumer that stops
    reading right after it has the whole response, so the record is
    ``status="ok"`` even though the iterator was not exhausted."""

    def __init__(self, call: ProviderCall) -> None:
        self.call = call
        self.count = 0
        self.finished = False
        call.mark_stream()
        call.stream_opened()
        # The call's __del__ backstop reports through this state, so an
        # abandoned stream keeps the chunk count and whatever was observed.
        call.on_abandon(self._abandoned)

    def observe(self, chunk: Any) -> Any:  # pragma: no cover - overridden
        """Read usage / finish reason / content out of one chunk."""

    def info(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        return {}

    def on_item(self, chunk: Any) -> Any:
        self.count += 1
        if self.count == 1:
            self.call.first_chunk()
        result = self.observe(chunk)
        if result is SKIP:
            self.count -= 1
        ctx = self.call.ctx
        if ctx is not None and ctx.chunk_check is not None:
            ctx.chunk_check(self.text_so_far())  # may raise CallBlocked
        return result

    def text_so_far(self) -> str:
        """The completion text read so far (subclasses that accumulate
        text expose it as ``get_text``)."""
        get = getattr(self, "get_text", None)
        return get() if callable(get) else ""

    def _abandoned(self) -> None:
        self.on_end(False, None)

    def on_end(self, completed: bool, error: Optional[BaseException]) -> None:
        if self.call.done:
            return
        try:
            info = self.info()
        except Exception:
            logger.debug("provider stream info failed", exc_info=True)
            info = {}
        self.call.finish(
            info, error=error,
            stream={"chunk_count": self.count, "stream_completed": completed},
            finished=self.finished,
        )
        raise_after_stream(self.call, completed, error)


def raise_after_stream(call: ProviderCall, completed: bool,
                       error: Optional[BaseException]) -> None:
    """At the end of a stream read to exhaustion, raise what an ``after``
    hook decided (a blocked completion), from the consumer's last
    ``next()``. Never for a stream that was closed, abandoned or failed."""
    ctx = call.ctx
    if ctx is not None and ctx.pending is not None:
        if completed and error is None and not in_gc():
            ctx.raise_pending()
        ctx.pending = None


def given(value: Any) -> bool:
    """False for None and for the Stainless SDKs' ``Omit`` / ``NotGiven``
    sentinels."""
    if value is None:
        return False
    return type(value).__name__ not in ("Omit", "NotGiven")


def text_accumulator(limit: int) -> Tuple[Callable[[Any], None], Callable[[], str]]:
    """Bounded string accumulator for streamed text: ``(add, get)``."""
    parts: List[str] = []
    size = [0]

    def add(piece: Any) -> None:
        if isinstance(piece, str) and piece and size[0] < limit:
            parts.append(piece[: limit - size[0]])
            size[0] += len(piece)

    def get() -> str:
        return "".join(parts)

    return add, get


Begin = Callable[[tuple, dict], Optional[ProviderCall]]
End = Callable[[ProviderCall, Any, tuple, dict], Any]


def _fail(call: ProviderCall, exc: BaseException) -> None:
    """Record the SDK's exception; whatever happens here, the caller
    re-raises the original."""
    try:
        call.finish(error=exc)
    except BaseException:  # noqa: B902 - the SDK's exception must win
        pass


def sync_method(original: Callable[..., Any], begin: Begin, end: End,
                spec: Any = None) -> Callable[..., Any]:
    """Wrap a synchronous SDK method. ``begin(args, kwargs)`` returns the
    call (or None to pass through); ``end(call, result, args, kwargs)``
    finishes it, or instruments a returned stream, and returns the result.

    The SDK's exception propagates as the same object (a bare ``raise``):
    only this wrapper's frame is added to its traceback.

    ``spec`` (a :class:`~._requests.RequestSpec`) lets the call hooks read
    and change the request; without it no hook runs for this method."""

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call = _safe_begin(begin, args, kwargs)
        if call is None:
            return original(*args, **kwargs)
        ctx = _hooked(call, spec, kwargs, False) if _hooks else None
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            _fail(call, exc)
            raise
        result = _safe_end(end, call, result, args, kwargs)
        if ctx is not None:
            return ctx.after_result(result)
        return result

    return wrapper


def async_method(original: Callable[..., Any], begin: Begin, end: End,
                 aend: Optional[Callable[..., Any]] = None, spec: Any = None) -> Callable[..., Any]:
    """Async counterpart of :func:`sync_method`. ``aend`` is an optional
    coroutine function used instead of ``end`` when finishing the record
    needs an ``await`` (parsing an async raw response)."""

    async def complete(call: ProviderCall, awaitable: Any, args: tuple, kwargs: dict) -> Any:
        try:
            result = await awaitable
        except BaseException as exc:
            _fail(call, exc)
            raise
        if aend is None:
            result = _safe_end(end, call, result, args, kwargs)
        else:
            try:
                result = await aend(call, result, args, kwargs)
            except Exception:
                logger.debug("provider capture could not finish", exc_info=True)
                call.finish(info={"limits": ["capture failed while reading the response"]})
        ctx = call.ctx
        if ctx is not None:
            return ctx.after_result(result)
        return result

    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            call = _safe_begin(begin, args, kwargs)
            if call is None:
                return await original(*args, **kwargs)
            if _hooks:
                _hooked(call, spec, kwargs, True)
            return await complete(call, original(*args, **kwargs), args, kwargs)

        return wrapper

    # The SDK's "async" method is a plain function returning an awaitable
    # (openai and anthropic decorate theirs). Stay one: introspection sees
    # the same kind of callable, and whatever the original raises when it
    # is *called* (argument validation) is still raised at the call, not at
    # the await.
    @functools.wraps(original)
    def plain_wrapper(*args: Any, **kwargs: Any) -> Any:
        call = _safe_begin(begin, args, kwargs)
        if call is None:
            return original(*args, **kwargs)
        if _hooks:
            _hooked(call, spec, kwargs, True)
        try:
            awaitable = original(*args, **kwargs)
        except BaseException as exc:
            _fail(call, exc)
            raise
        return complete(call, awaitable, args, kwargs)

    return plain_wrapper


def sync_generator_method(original: Callable[..., Any], begin: Begin,
                          make_state: Callable[[ProviderCall], StreamState],
                          spec: Any = None) -> Callable[..., Any]:
    """Wrap a method that is a *generator function* (google-genai's
    ``generate_content_stream``). The wrapper is a generator function too,
    so ``inspect.isgeneratorfunction`` still holds and, exactly like the
    original, nothing runs (no request, no record) until the first
    ``next()``."""

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call = _safe_begin(begin, args, kwargs)
        if call is None:
            return (yield from original(*args, **kwargs))
        if _hooks:
            _hooked(call, spec, kwargs, False)
        try:
            state = make_state(call)
            on_item, on_end = state.on_item, state.on_end
        except Exception:
            logger.debug("provider stream capture could not start", exc_info=True)
            call.finish(info={"limits": ["stream capture could not start"]})
            return (yield from original(*args, **kwargs))
        return (yield from tee(original(*args, **kwargs), on_item, on_end))

    return wrapper


def _safe_begin(begin: Begin, args: tuple, kwargs: dict) -> Optional[ProviderCall]:
    try:
        return begin(args, kwargs)
    except Exception:
        logger.debug("provider capture could not start", exc_info=True)
        return None


def _safe_end(end: End, call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
    try:
        return end(call, result, args, kwargs)
    except Exception:
        logger.debug("provider capture could not finish", exc_info=True)
        call.finish(info={"limits": ["capture failed while reading the response"]})
        return result


__all__ = [
    "AsyncStreamProxy",
    "CallBlocked",
    "CallContext",
    "CallOutcome",
    "PatchTarget",
    "ProviderCall",
    "ProviderInstrumentor",
    "SKIP",
    "STATUS_CANCELLED",
    "STATUS_ERROR",
    "STATUS_INCOMPLETE",
    "STATUS_OK",
    "StreamProxy",
    "StreamState",
    "async_method",
    "active_providers",
    "add_call_hook",
    "atee",
    "call_hooks",
    "flush_abandoned",
    "given",
    "http_status_of",
    "in_gc",
    "instrument_stream",
    "jsonable",
    "normalize_capture_content",
    "raise_after_stream",
    "read",
    "remove_call_hook",
    "scalar_params",
    "sync_generator_method",
    "sync_method",
    "tee",
    "text_accumulator",
    "token_usage",
]
