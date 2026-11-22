"""Auto-instrumentation for the ``anthropic`` Python SDK.

Tested with ``anthropic==2.12.0`` (the version this module's test-suite
runs against). Nothing is pinned to a version: every patch target is
resolved and its signature checked at install time, and a surface whose
layout differs is left untouched and reported (``briefcase.auto.status()``).

Surfaces, each patched all-or-nothing (sync and async together), all
recorded as ``decision_type="llm.anthropic.messages"``:

- ``messages``: ``anthropic.resources.messages.(Async)Messages.create`` /
  ``.parse`` / ``.stream``
- ``beta.messages``: the same methods of
  ``anthropic.resources.beta.messages.(Async)Messages``

How ``messages.stream()`` is captured
-------------------------------------

``stream()`` returns a manager; the request is sent when the manager is
entered, and ``__enter__`` / ``__aenter__`` return the event stream. That
is the documented contract (``with client.messages.stream(...) as stream``)
and the only thing this module depends on:

1. The ``stream`` wrapper notes the request on the returned manager and
   patches ``__enter__``/``__exit__`` (``__aenter__``/``__aexit__``) on *the
   manager's class, whatever it is called* (looked up with ``type()`` at
   run time, so a renamed or moved manager class is still covered).
2. Entering starts the record (and records the SDK's error if the request
   fails); the stream it returns is observed through its ``_iterator``
   generator, like every Stainless stream, so ``text_stream``,
   ``get_final_message()`` and ``until_done()`` all feed the record.
3. Fallback, if a future stream object has no ``_iterator``: nothing is
   touched, and the record is built at ``__exit__`` from the public
   ``current_message_snapshot`` property, without chunk timing
   (``capture_limits`` says so).

No private attribute of the manager is read (earlier versions wrapped its
name-mangled ``__api_request``).

Other layout facts, as found in 2.12.0:

- ``create(stream=True)`` returns ``anthropic.Stream`` / ``AsyncStream`` of
  raw events, iterating an ``_iterator`` generator. The record is emitted
  when it is exhausted, closed, or dropped.
- ``parse()`` posts directly and is patched itself.
- Usage fields: ``Message.usage`` (``input_tokens``, ``output_tokens``,
  ``cache_read_input_tokens``, ``cache_creation_input_tokens``). In a
  stream, ``message_start`` carries the input side and a *placeholder*
  ``output_tokens``; each ``message_delta`` carries the cumulative
  ``output_tokens`` and the ``stop_reason``. A stream that ended before its
  first ``message_delta`` therefore has ``completion_tokens: None``.
  ``input_tokens`` excludes cached tokens, so ``total_tokens`` here is
  ``input_tokens + output_tokens`` and the cache counters are reported next
  to it.
- The request id is ``Message._request_id`` (``request-id`` response header
  for streams).
- ``with_raw_response`` / ``with_streaming_response``: see :mod:`._stainless`.

``AnthropicBedrock`` / ``AnthropicVertex`` clients use the same resource
classes and are covered. Not covered: ``messages.count_tokens``, the
Batches API, objects created before ``auto()`` that hold a bound method.
"""

from __future__ import annotations

import functools
import weakref
from typing import Any, Callable, Dict, Optional

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import (
    PatchTarget,
    ProviderCall,
    ProviderInstrumentor,
    StreamState,
    _fail,
    async_method,
    given,
    instrument_stream,
    read,
    scalar_params,
    sync_method,
    text_accumulator,
    token_usage,
)
from briefcase.integrations.providers import _base, _requests
from briefcase.integrations.providers._stainless import Surface, header

logger = get_logger(__name__)

PROVIDER = "anthropic"
TESTED_VERSIONS = ("2.12.0",)
VERIFIED_VERSION = TESTED_VERSIONS[-1]
DECISION_TYPE = "llm.anthropic.messages"
REQUEST_ID_HEADER = "request-id"
#: Attribute set on a manager returned by ``stream()``: the request to record.
PENDING_ATTR = "_briefcase_pending_request"
#: Attribute set on an entered manager: the stream state to end at exit.
STATE_ATTR = "_briefcase_stream_state"

LIMIT_NO_ITERATOR = (
    "message stream has no assignable '_iterator' in this SDK version; record built from "
    "current_message_snapshot at exit, without chunk count or timing"
)

# 2.12.0's create() takes max_tokens / service_tier / speed / inference_geo;
# the sampling parameters are kept for older SDK releases that accept them.
_PARAMS = (
    "max_tokens", "service_tier", "speed", "inference_geo",
    "temperature", "top_p", "top_k",
)
_INPUT_KEYS = ("system", "messages", "tools")


def _usage(usage: Any) -> Optional[Dict[str, Optional[int]]]:
    return token_usage(
        read(usage, "input_tokens"),
        read(usage, "output_tokens"),
        cache_read_input_tokens=read(usage, "cache_read_input_tokens"),
        cache_creation_input_tokens=read(usage, "cache_creation_input_tokens"),
    )


def _content_blocks(blocks: Any) -> Dict[str, Any]:
    texts = []
    tool_calls = []
    for block in blocks if isinstance(blocks, (list, tuple)) else ():
        kind = read(block, "type")
        if kind == "text":
            texts.append(read(block, "text") or "")
        elif kind == "tool_use":
            tool_calls.append({
                "id": read(block, "id"),
                "name": read(block, "name"),
                "input": read(block, "input"),
            })
    content: Dict[str, Any] = {"content": "".join(texts)}
    if tool_calls:
        content["tool_calls"] = tool_calls
    return content


def _message_info(message: Any, want_content: bool) -> Dict[str, Any]:
    blocks = read(message, "content")
    tool_calls = sum(
        1 for block in (blocks if isinstance(blocks, (list, tuple)) else ())
        if read(block, "type") == "tool_use"
    )
    info: Dict[str, Any] = {
        "model": read(message, "model"),
        "response_id": read(message, "id"),
        "request_id": read(message, "_request_id"),
        "finish_reason": read(message, "stop_reason"),
        "usage": _usage(read(message, "usage")),
        "extra": {"tool_call_count": tool_calls or None},
    }
    if want_content:
        info["content"] = _content_blocks(blocks)
    return info


class _MessageStream(StreamState):
    """Reads message stream events (raw ones, or the parsed events of the
    ``stream()`` helper, which pass the raw ``message_start`` /
    ``message_delta`` / ``content_block_delta`` through)."""

    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = call.want_content
        self.add_text, self.get_text = text_accumulator(call.text_limit)
        self.data: Dict[str, Any] = {"request_id": request_id}
        self.input_tokens: Any = None
        self.output_tokens: Any = None
        self.cache_read: Any = None
        self.cache_creation: Any = None
        #: message_start carries a placeholder output_tokens; only a
        #: message_delta makes the output count real.
        self.saw_delta = False

    def _read_usage(self, usage: Any, final: bool) -> None:
        for attr, key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("cache_creation", "cache_creation_input_tokens"),
        ):
            if attr == "output_tokens" and not final:
                continue
            value = read(usage, key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, attr, value)

    def observe(self, event: Any) -> Any:
        kind = read(event, "type")
        if kind == "message_start":
            message = read(event, "message")
            self.data["model"] = read(message, "model")
            self.data["response_id"] = read(message, "id")
            self._read_usage(read(message, "usage"), final=False)
        elif kind == "message_delta":
            self.saw_delta = True
            self._read_usage(read(event, "usage"), final=True)
            stop_reason = read(event, "delta", "stop_reason")
            if stop_reason:
                self.data["finish_reason"] = stop_reason
        elif kind == "message_stop":
            self.finished = True
        elif kind == "content_block_delta" and self.want_content:
            if read(event, "delta", "type") == "text_delta":
                self.add_text(read(event, "delta", "text"))
        return None

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        info["usage"] = token_usage(
            self.input_tokens, self.output_tokens,
            cache_read_input_tokens=self.cache_read,
            cache_creation_input_tokens=self.cache_creation,
        )
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


class _SnapshotStream(StreamState):
    """Fallback for a ``stream()`` result that cannot be observed chunk by
    chunk: reads the public ``current_message_snapshot`` when the manager
    exits."""

    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, stream: Any) -> None:
        super().__init__(call)
        self.inst = inst
        self.stream = stream
        self.count = None  # type: ignore[assignment]
        call.limit(LIMIT_NO_ITERATOR)

    def info(self) -> Dict[str, Any]:
        try:
            snapshot = self.stream.current_message_snapshot
        except Exception:
            return {"request_id": header(self.stream, REQUEST_ID_HEADER)}
        info = _message_info(snapshot, self.call.want_content)
        info["request_id"] = info.get("request_id") or header(self.stream, REQUEST_ID_HEADER)
        if read(snapshot, "stop_reason") is None:
            # No message_delta was consumed: the output count is the
            # message_start placeholder.
            usage = info.get("usage")
            if isinstance(usage, dict):
                usage["completion_tokens"] = None
                usage["total_tokens"] = None
        else:
            self.finished = True
        return info

    def on_end(self, completed: bool, error: Optional[BaseException]) -> None:
        if self.call.done:
            return
        try:
            info = self.info()
        except Exception:
            info = {}
        self.call.finish(
            info, error=error,
            stream={"chunk_count": None, "stream_completed": None},
            finished=self.finished,
        )
        _base.raise_after_stream(self.call, False, error)


def _begin(inst: ProviderInstrumentor, function_name: str, kwargs: dict,
           stream: bool) -> Optional[ProviderCall]:
    params = scalar_params(kwargs, _PARAMS)
    effort = read(kwargs.get("output_config"), "effort")
    if isinstance(effort, str):
        params["effort"] = effort
    return inst.begin(
        DECISION_TYPE, function_name,
        model=kwargs.get("model"),
        params=params,
        inputs=lambda: {k: kwargs[k] for k in _INPUT_KEYS if given(kwargs.get(k))},
        stream=stream,
    )


def _call_factory(inst: ProviderInstrumentor, function_name: str,
                  is_async: bool) -> Callable[[Any], Any]:
    """Wrapper for ``create`` and ``parse``."""
    surface = Surface(
        inst, is_async=is_async, info_fn=_message_info,
        state_fn=lambda call, request_id: _MessageStream(call, inst, request_id),
        request_id_header=REQUEST_ID_HEADER,
    )

    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        return _begin(inst, function_name, kwargs, kwargs.get("stream") is True)

    def factory(original: Any) -> Any:
        if is_async:
            return async_method(original, begin, surface.end, surface.aend,
                                spec=_requests.ANTHROPIC_MESSAGES)
        return sync_method(original, begin, surface.end, spec=_requests.ANTHROPIC_MESSAGES)

    return factory


# -- messages.stream() ------------------------------------------------------

def _attach(inst: ProviderInstrumentor, call: ProviderCall, manager: Any, stream: Any,
            is_async: bool) -> None:
    """Observe the event stream an entered manager returned."""
    state: StreamState = _MessageStream(call, inst, header(stream, REQUEST_ID_HEADER))
    if instrument_stream(stream, state.on_item, state.on_end,
                         is_async=is_async, proxy=False) is None:
        state = _SnapshotStream(call, inst, stream)
    try:
        setattr(manager, STATE_ATTR, state)
    except Exception:
        logger.debug("could not keep the stream state on the manager", exc_info=True)


def _take(manager: Any, name: str) -> Any:
    try:
        return vars(manager).pop(name, None)
    except Exception:
        return None


def _exited(manager: Any) -> None:
    """The manager's ``with`` block ended: a stream not read to its end is
    finished here (as incomplete), never raising."""
    state = _take(manager, STATE_ATTR)
    if state is not None:
        try:
            state.on_end(False, None)
        except Exception:
            logger.debug("anthropic stream could not be finished at exit", exc_info=True)


def _enter_factory(inst: ProviderInstrumentor, is_async: bool) -> Callable[[Any], Any]:
    def start(manager: Any) -> Optional[ProviderCall]:
        pending = _take(manager, PENDING_ATTR)
        if pending is None:
            return None
        try:
            function_name, kwargs, ctx = pending
            call = _begin(inst, function_name, kwargs, True)
            finalizer = ctx.state.pop("_never_entered", None) if ctx is not None else None
            if finalizer is not None:
                finalizer.detach()
            if call is not None and ctx is not None:
                call.attach(ctx)
                call.request_changed(ctx)
                ctx.sent = True
            elif ctx is not None:
                _never_entered(ctx)
            return call
        except Exception:
            logger.debug("anthropic stream capture could not start", exc_info=True)
            return None

    def attach(call: ProviderCall, manager: Any, stream: Any) -> None:
        try:
            _attach(inst, call, manager, stream, is_async)
        except Exception:
            logger.debug("anthropic stream could not be observed", exc_info=True)
            call.finish({"limits": ["stream capture failed"]})

    def factory(original: Any) -> Any:
        if is_async:
            @functools.wraps(original)
            async def aenter(self: Any, *args: Any, **kwargs: Any) -> Any:
                call = start(self)
                if call is None:
                    return await original(self, *args, **kwargs)
                try:
                    stream = await original(self, *args, **kwargs)
                except BaseException as exc:
                    _fail(call, exc)
                    raise
                attach(call, self, stream)
                return stream

            return aenter

        @functools.wraps(original)
        def enter(self: Any, *args: Any, **kwargs: Any) -> Any:
            call = start(self)
            if call is None:
                return original(self, *args, **kwargs)
            try:
                stream = original(self, *args, **kwargs)
            except BaseException as exc:
                _fail(call, exc)
                raise
            attach(call, self, stream)
            return stream

        return enter

    return factory


def _exit_factory(is_async: bool) -> Callable[[Any], Any]:
    def factory(original: Any) -> Any:
        if is_async:
            @functools.wraps(original)
            async def aexit(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    return await original(self, *args, **kwargs)
                finally:
                    _exited(self)

            return aexit

        @functools.wraps(original)
        def exit_(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return original(self, *args, **kwargs)
            finally:
                _exited(self)

        return exit_

    return factory


def _never_entered(ctx: Any) -> None:
    """A ``stream()`` manager dropped without entering it: the request was
    never sent, so the hooks release what they reserved."""
    try:
        ctx.complete({}, None, False, None, True)
    except Exception:
        logger.debug("anthropic stream hooks could not be released", exc_info=True)


def _stream_hooks(inst: ProviderInstrumentor, function_name: str, kwargs: dict,
                  is_async: bool) -> Optional[Any]:
    """Run the call hooks' ``before`` when ``stream()`` is called: the SDK
    binds the request then, and sends it at ``__enter__``."""
    hooks = _base.call_hooks()
    if not hooks or not inst.active:
        return None
    ctx = _base.CallContext(_requests.ANTHROPIC_STREAM, kwargs, is_async, hooks)
    try:
        ctx.run_before()
    except _base.CallBlocked as exc:
        call = _begin(inst, function_name, kwargs, True)
        if call is not None:
            call.attach(ctx)
            _base._fail(call, exc)
        else:
            ctx.complete({}, exc, False, None, True)
        raise
    return ctx


def _stream_factory(inst: ProviderInstrumentor, function_name: str,
                    is_async: bool) -> Callable[[Any], Any]:
    """Wrapper for ``stream``: sync in both clients, returning a manager."""
    enter, exit_ = ("__aenter__", "__aexit__") if is_async else ("__enter__", "__exit__")

    def hook(manager: Any, kwargs: dict, ctx: Any) -> None:
        cls = type(manager)
        label = f"{cls.__module__}.{cls.__qualname__}"
        if not (inst.patch_object(cls, enter, _enter_factory(inst, is_async), f"{label}.{enter}")
                and inst.patch_object(cls, exit_, _exit_factory(is_async), f"{label}.{exit_}")):
            inst.report_skipped()
            if ctx is not None:
                _never_entered(ctx)
            return
        setattr(manager, PENDING_ATTR, (function_name, kwargs, ctx))
        if ctx is not None:
            ctx.state["_never_entered"] = weakref.finalize(manager, _never_entered, ctx)

    def factory(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _stream_hooks(inst, function_name, kwargs, is_async) if _base._hooks else None
            manager = original(*args, **kwargs)
            try:
                if inst.active and (ctx is not None or inst._resolve_exporter() is not None):
                    hook(manager, kwargs, ctx)
            except Exception:
                logger.debug("anthropic stream manager could not be hooked", exc_info=True)
            return manager

        return wrapper

    return factory


_TARGETS = (
    ("messages", "anthropic.resources.messages", "anthropic.messages"),
    ("beta.messages", "anthropic.resources.beta.messages", "anthropic.beta.messages"),
)


def instrument(**options: Any) -> ProviderInstrumentor:
    """Patch the anthropic SDK. Returns the instrumentor; call its
    ``uninstall()`` (or ``briefcase.auto.undo("anthropic")``) to restore.

    Args:
        **options: See :class:`ProviderInstrumentor`.

    Raises:
        ImportError: ``anthropic`` is not installed.
    """
    try:
        import anthropic as sdk
    except ImportError as exc:
        raise ImportError(
            "briefcase.auto: anthropic is not installed, so nothing was "
            "instrumented. Install with: pip install anthropic"
        ) from exc

    inst = ProviderInstrumentor(PROVIDER, **options)
    inst.sdk_version = getattr(sdk, "__version__", None)
    inst.tested_versions = TESTED_VERSIONS
    inst.limits = [
        "with_streaming_response: usage only when the caller uses .parse()",
        "a stream abandoned before its first message_delta has completion_tokens None",
        "messages.count_tokens and the Batches API are not recorded",
    ]
    for name, module, prefix in _TARGETS:
        targets = []
        for class_name in ("Messages", "AsyncMessages"):
            is_async = class_name.startswith("Async")
            targets.append(PatchTarget(
                module, class_name, "create",
                _call_factory(inst, f"{prefix}.create", is_async), ("model", "stream")))
            targets.append(PatchTarget(
                module, class_name, "parse",
                _call_factory(inst, f"{prefix}.parse", is_async), ("model",)))
            targets.append(PatchTarget(
                module, class_name, "stream",
                _stream_factory(inst, f"{prefix}.stream", is_async), ("model",)))
        inst.patch_group(name, targets)
    inst.report_skipped()
    return inst


__all__ = ["DECISION_TYPE", "PROVIDER", "TESTED_VERSIONS", "VERIFIED_VERSION", "instrument"]
