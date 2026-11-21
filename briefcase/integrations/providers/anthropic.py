"""Auto-instrumentation for the ``anthropic`` Python SDK.

Verified against ``anthropic==2.12.0`` by inspecting the installed package.
Patch targets (each is checked at install time; a missing one is skipped
with a WARNING), all recorded as ``decision_type="llm.anthropic.messages"``:

- ``anthropic.resources.messages.Messages.create`` / ``.parse`` / ``.stream``
- ``anthropic.resources.messages.AsyncMessages.create`` / ``.parse`` / ``.stream``
- ``anthropic.resources.beta.messages.Messages.create`` / ``.parse`` / ``.stream``
- ``anthropic.resources.beta.messages.AsyncMessages.create`` / ``.parse`` / ``.stream``

Layout facts this module relies on, as found in 2.12.0:

- ``create(stream=True)`` returns ``anthropic.Stream`` / ``AsyncStream`` of
  raw events, iterating a ``_iterator`` generator (a cached property). The
  record is emitted when it is exhausted, closed, or dropped.
- ``stream()`` does not go through ``create``: it returns a
  ``MessageStreamManager`` (``AsyncMessageStreamManager``, and the ``Beta``
  variants) holding the pending request in a private ``__api_request``
  attribute: a callable for the sync manager, an awaitable for the async
  one. That request is wrapped so the raw stream it produces is
  instrumented. If a future SDK renames the attribute, ``stream()`` calls
  are passed through unrecorded and a WARNING is logged once.
- ``parse()`` posts directly and is patched itself.
- Usage fields: ``Message.usage`` (``input_tokens``, ``output_tokens``,
  ``cache_read_input_tokens``, ``cache_creation_input_tokens``). In a
  stream, ``message_start`` carries the input side and each
  ``message_delta`` the cumulative ``output_tokens`` and the
  ``stop_reason``. ``input_tokens`` excludes cached tokens, so
  ``total_tokens`` here is ``input_tokens + output_tokens`` and the cache
  counters are reported next to it.
- The request id is ``Message._request_id`` (``request-id`` response header
  for streams).

``AnthropicBedrock`` / ``AnthropicVertex`` clients use the same resource
classes and are covered.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, Optional

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import (
    ProviderCall,
    ProviderInstrumentor,
    StreamState,
    async_method,
    given,
    instrument_stream,
    read,
    scalar_params,
    sync_method,
    text_accumulator,
    token_usage,
    unwrap_raw,
)

logger = get_logger(__name__)

PROVIDER = "anthropic"
VERIFIED_VERSION = "2.12.0"
DECISION_TYPE = "llm.anthropic.messages"

# 2.12.0's create() takes max_tokens / service_tier / speed / inference_geo;
# the sampling parameters are kept for older SDK releases that accept them.
_PARAMS = (
    "max_tokens", "service_tier", "speed", "inference_geo",
    "temperature", "top_p", "top_k",
)
_INPUT_KEYS = ("system", "messages", "tools")

_warned_manager_layout = False


def _usage(usage: Any) -> Optional[Dict[str, int]]:
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
    info: Dict[str, Any] = {
        "model": read(message, "model"),
        "response_id": read(message, "id"),
        "request_id": read(message, "_request_id"),
        "finish_reason": read(message, "stop_reason"),
        "usage": _usage(read(message, "usage")),
    }
    if want_content:
        info["content"] = _content_blocks(read(message, "content"))
    return info


class _MessageStream(StreamState):
    """Reads raw message stream events."""

    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = inst.captures_content
        self.add_text, self.get_text = text_accumulator(inst.max_content_chars)
        self.data: Dict[str, Any] = {"request_id": request_id}
        self.input_tokens: Any = None
        self.output_tokens: Any = None
        self.cache_read: Any = None
        self.cache_creation: Any = None

    def _read_usage(self, usage: Any) -> None:
        for attr, key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("cache_creation", "cache_creation_input_tokens"),
        ):
            value = read(usage, key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, attr, value)

    def observe(self, event: Any) -> None:
        kind = read(event, "type")
        if kind == "message_start":
            message = read(event, "message")
            self.data["model"] = read(message, "model")
            self.data["response_id"] = read(message, "id")
            self._read_usage(read(message, "usage"))
        elif kind == "message_delta":
            self._read_usage(read(event, "usage"))
            stop_reason = read(event, "delta", "stop_reason")
            if stop_reason:
                self.data["finish_reason"] = stop_reason
        elif kind == "content_block_delta" and self.want_content:
            if read(event, "delta", "type") == "text_delta":
                self.add_text(read(event, "delta", "text"))

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


def _attach(inst: ProviderInstrumentor, call: ProviderCall, stream: Any, is_async: bool) -> Any:
    headers = read(stream, "response", "headers")
    request_id = headers.get("request-id") if headers is not None else None
    state = _MessageStream(call, inst, request_id)
    return instrument_stream(stream, state.on_item, state.on_end, is_async=is_async)


def _call_factory(inst: ProviderInstrumentor, function_name: str,
                  is_async: bool) -> Callable[[Any], Any]:
    """Wrapper for ``create`` and ``parse``."""

    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        return _begin(inst, function_name, kwargs, kwargs.get("stream") is True)

    def end(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        raw, parsed = unwrap_raw(result, kwargs)
        if parsed is None:
            headers = read(result, "headers")
            call.finish({"request_id": headers.get("request-id") if headers is not None else None})
            return result
        if kwargs.get("stream") is True:
            instrumented = _attach(inst, call, parsed, is_async)
            if raw is not None:
                if instrumented is not parsed:
                    call.finish()
                return result
            return instrumented
        call.finish(_message_info(parsed, inst.captures_content))
        return result

    def factory(original: Any) -> Any:
        return (async_method if is_async else sync_method)(original, begin, end)

    return factory


class _LazyRequest:
    """Awaitable standing in for the async manager's pending request. The
    wrapped coroutine is only created when the manager is entered, so a
    manager that is never entered leaves nothing un-awaited behind."""

    def __init__(self, run: Callable[[], Any]) -> None:
        self._run = run

    def __await__(self) -> Any:
        return self._run().__await__()


def _hook_manager(inst: ProviderInstrumentor, function_name: str, manager: Any,
                  kwargs: dict, is_async: bool) -> None:
    """Wrap the pending request of a ``*MessageStreamManager``."""
    global _warned_manager_layout
    attr = next(
        (name for name in getattr(manager, "__dict__", {}) if name.endswith("__api_request")),
        None,
    )
    if attr is None:
        if not _warned_manager_layout:
            _warned_manager_layout = True
            logger.warning(
                "briefcase.auto[anthropic]: %s has no '__api_request' attribute "
                "in the installed SDK (verified against %s); messages.stream() "
                "calls are not recorded.",
                type(manager).__name__, VERIFIED_VERSION,
            )
        return
    pending = getattr(manager, attr)

    if is_async:
        async def run() -> Any:
            call = _begin(inst, function_name, kwargs, True)
            if call is None:
                return await pending
            try:
                raw_stream = await pending
            except BaseException as exc:
                call.finish(error=exc)
                raise
            return _attach(inst, call, raw_stream, True)

        setattr(manager, attr, _LazyRequest(run))
        return

    def request() -> Any:
        call = _begin(inst, function_name, kwargs, True)
        if call is None:
            return pending()
        try:
            raw_stream = pending()
        except BaseException as exc:
            call.finish(error=exc)
            raise
        return _attach(inst, call, raw_stream, False)

    setattr(manager, attr, request)


def _stream_factory(inst: ProviderInstrumentor, function_name: str,
                    is_async: bool) -> Callable[[Any], Any]:
    """Wrapper for ``stream``: sync in both clients, returning a manager."""

    def factory(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            manager = original(*args, **kwargs)
            try:
                if inst._resolve_exporter() is not None:
                    _hook_manager(inst, function_name, manager, kwargs, is_async)
            except Exception:
                logger.debug("anthropic stream manager could not be hooked", exc_info=True)
            return manager

        return wrapper

    return factory


_TARGETS = (
    ("anthropic.resources.messages", "anthropic.messages"),
    ("anthropic.resources.beta.messages", "anthropic.beta.messages"),
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
    for module, prefix in _TARGETS:
        for class_name in ("Messages", "AsyncMessages"):
            is_async = class_name.startswith("Async")
            for method in ("create", "parse"):
                inst.patch(module, class_name, method,
                           _call_factory(inst, f"{prefix}.{method}", is_async))
            inst.patch(module, class_name, "stream",
                       _stream_factory(inst, f"{prefix}.stream", is_async))
    return inst


__all__ = ["DECISION_TYPE", "PROVIDER", "VERIFIED_VERSION", "instrument"]
