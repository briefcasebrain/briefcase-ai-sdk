"""Auto-instrumentation for the ``openai`` Python SDK.

Verified against ``openai==3.57.0`` by inspecting the installed package.
Patch targets (each is checked at install time; a missing one is skipped
with a WARNING):

==========================================  ==========================  ====================
module                                      class.method                decision_type
==========================================  ==========================  ====================
``openai.resources.chat.completions``       ``Completions.create``      ``llm.openai.chat``
``openai.resources.chat.completions``       ``Completions.parse``       ``llm.openai.chat``
``openai.resources.chat.completions``       ``AsyncCompletions.create`` ``llm.openai.chat``
``openai.resources.chat.completions``       ``AsyncCompletions.parse``  ``llm.openai.chat``
``openai.resources.responses``              ``Responses.create``        ``llm.openai.responses``
``openai.resources.responses``              ``Responses.parse``         ``llm.openai.responses``
``openai.resources.responses``              ``AsyncResponses.create``   ``llm.openai.responses``
``openai.resources.responses``              ``AsyncResponses.parse``    ``llm.openai.responses``
``openai.resources.completions``            ``Completions.create``      ``llm.openai.completions``
``openai.resources.completions``            ``AsyncCompletions.create`` ``llm.openai.completions``
``openai.resources.embeddings``             ``Embeddings.create``       ``llm.openai.embeddings``
``openai.resources.embeddings``             ``AsyncEmbeddings.create``  ``llm.openai.embeddings``
==========================================  ==========================  ====================

Layout facts this module relies on, as found in 3.57.0:

- ``chat.completions.stream()`` and ``responses.stream()`` call
  ``resource.create(..., stream=True)``, so they are covered by the
  ``create`` patches; ``parse()`` posts directly and is patched itself.
- ``create(stream=True)`` returns ``openai.Stream`` / ``openai.AsyncStream``,
  which iterate a private ``_iterator`` generator. The record is emitted
  when that generator is exhausted, when the stream is closed, or when it
  is dropped.
- ``with_raw_response.create`` calls the patched ``create`` with the
  ``X-Stainless-Raw-Response: true`` header and gets a ``LegacyAPIResponse``
  whose ``parse()`` result is cached, so parsing it for the record does not
  change what the caller receives. ``with_streaming_response`` (header value
  ``stream``) leaves the body unread; such a call is recorded without usage.
- Usage fields: ``CompletionUsage`` (``prompt_tokens``, ``completion_tokens``,
  ``total_tokens``, ``prompt_tokens_details.cached_tokens``,
  ``completion_tokens_details.reasoning_tokens``) and ``ResponseUsage``
  (``input_tokens``, ``output_tokens``, ``total_tokens``,
  ``input_tokens_details.cached_tokens``,
  ``output_tokens_details.reasoning_tokens``).
- A chat stream only carries usage when the request sets
  ``stream_options={"include_usage": True}``. That adds a final chunk with
  an empty ``choices`` list to the caller's stream, so it is not injected
  unless ``stream_usage=True`` is passed to the instrumentor.

``AzureOpenAI`` uses the same resource classes and is covered.
"""

from __future__ import annotations

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

PROVIDER = "openai"
VERIFIED_VERSION = "3.57.0"

_CHAT_PARAMS = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "seed",
    "frequency_penalty", "presence_penalty", "reasoning_effort", "service_tier",
)
_RESPONSES_PARAMS = (
    "temperature", "top_p", "max_output_tokens", "service_tier", "truncation",
)
_COMPLETIONS_PARAMS = (
    "temperature", "top_p", "max_tokens", "n", "seed",
    "frequency_penalty", "presence_penalty",
)
_EMBEDDINGS_PARAMS = ("dimensions", "encoding_format")


# -- response readers -------------------------------------------------------

def _completion_usage(usage: Any) -> Optional[Dict[str, int]]:
    return token_usage(
        read(usage, "prompt_tokens"),
        read(usage, "completion_tokens"),
        read(usage, "total_tokens"),
        cached_tokens=read(usage, "prompt_tokens_details", "cached_tokens"),
        reasoning_tokens=read(usage, "completion_tokens_details", "reasoning_tokens"),
    )


def _response_usage(usage: Any) -> Optional[Dict[str, int]]:
    return token_usage(
        read(usage, "input_tokens"),
        read(usage, "output_tokens"),
        read(usage, "total_tokens"),
        cached_tokens=read(usage, "input_tokens_details", "cached_tokens"),
        reasoning_tokens=read(usage, "output_tokens_details", "reasoning_tokens"),
    )


def _first_choice(obj: Any) -> Any:
    choices = read(obj, "choices")
    if isinstance(choices, (list, tuple)) and choices:
        return choices[0]
    return None


def _chat_info(result: Any, want_content: bool) -> Dict[str, Any]:
    choice = _first_choice(result)
    info: Dict[str, Any] = {
        "model": read(result, "model"),
        "response_id": read(result, "id"),
        "request_id": read(result, "_request_id"),
        "finish_reason": read(choice, "finish_reason"),
        "usage": _completion_usage(read(result, "usage")),
        "extra": {"service_tier": read(result, "service_tier")},
    }
    if want_content:
        message = read(choice, "message")
        content: Dict[str, Any] = {"content": read(message, "content")}
        tool_calls = read(message, "tool_calls")
        if tool_calls:
            content["tool_calls"] = tool_calls
        refusal = read(message, "refusal")
        if refusal:
            content["refusal"] = refusal
        info["content"] = content
    return info


def _responses_info(result: Any, want_content: bool) -> Dict[str, Any]:
    status = read(result, "status")
    info: Dict[str, Any] = {
        "model": read(result, "model"),
        "response_id": read(result, "id"),
        "request_id": read(result, "_request_id"),
        "finish_reason": read(result, "incomplete_details", "reason") or status,
        "usage": _response_usage(read(result, "usage")),
        "extra": {
            "service_tier": read(result, "service_tier"),
            "response_error_code": read(result, "error", "code"),
        },
    }
    if want_content:
        try:
            text = result.output_text
        except Exception:
            text = None
        info["content"] = {"content": text}
    return info


def _completions_info(result: Any, want_content: bool) -> Dict[str, Any]:
    choice = _first_choice(result)
    info: Dict[str, Any] = {
        "model": read(result, "model"),
        "response_id": read(result, "id"),
        "request_id": read(result, "_request_id"),
        "finish_reason": read(choice, "finish_reason"),
        "usage": _completion_usage(read(result, "usage")),
    }
    if want_content:
        info["content"] = {"content": read(choice, "text")}
    return info


def _embeddings_info(result: Any, want_content: bool) -> Dict[str, Any]:
    usage = read(result, "usage")
    data = read(result, "data")
    return {
        "model": read(result, "model"),
        "request_id": read(result, "_request_id"),
        "usage": token_usage(read(usage, "prompt_tokens"), 0, read(usage, "total_tokens")),
        "extra": {
            "embedding_count": len(data) if isinstance(data, (list, tuple)) else None,
        },
    }


# -- stream readers ---------------------------------------------------------

class _OpenAIStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = inst.captures_content
        self.add_text, self.get_text = text_accumulator(inst.max_content_chars)
        self.data: Dict[str, Any] = {"request_id": request_id}

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


class _ChatStream(_OpenAIStream):
    """Reads ``ChatCompletionChunk`` (and legacy ``Completion``) chunks."""

    def observe(self, chunk: Any) -> None:
        data = self.data
        model = read(chunk, "model")
        if model:
            data["model"] = model
        chunk_id = read(chunk, "id")
        if chunk_id:
            data["response_id"] = chunk_id
        usage = _completion_usage(read(chunk, "usage"))
        if usage:
            data["usage"] = usage
        choice = _first_choice(chunk)
        if choice is not None:
            finish = read(choice, "finish_reason")
            if finish:
                data["finish_reason"] = finish
            if self.want_content:
                self.add_text(read(choice, "delta", "content") or read(choice, "text"))


class _ResponsesStream(_OpenAIStream):
    """Reads ``ResponseStreamEvent`` events; the terminal event
    (``response.completed`` / ``response.incomplete`` / ``response.failed``)
    carries the full ``Response`` with usage."""

    _TERMINAL = ("response.completed", "response.incomplete", "response.failed")

    def observe(self, event: Any) -> None:
        kind = read(event, "type")
        if kind in self._TERMINAL or kind == "response.created":
            response = read(event, "response")
            if response is not None:
                request_id = self.data.get("request_id")
                self.data = {
                    k: v for k, v in _responses_info(response, False).items() if v is not None
                }
                self.data.setdefault("request_id", request_id)
        elif kind == "response.output_text.delta" and self.want_content:
            self.add_text(read(event, "delta"))


# -- wrappers ---------------------------------------------------------------

def _factory(
    inst: ProviderInstrumentor,
    *,
    decision_type: str,
    function_name: str,
    is_async: bool,
    params: tuple,
    input_keys: tuple,
    info_fn: Callable[[Any, bool], Dict[str, Any]],
    stream_cls: Optional[type],
    stream_usage: bool = False,
) -> Callable[[Any], Any]:

    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        streaming = kwargs.get("stream") is True
        if streaming and stream_usage and not isinstance(kwargs.get("stream_options"), dict):
            kwargs["stream_options"] = {"include_usage": True}
        return inst.begin(
            decision_type, function_name,
            model=kwargs.get("model"),
            params=scalar_params(kwargs, params),
            inputs=lambda: {k: kwargs[k] for k in input_keys if given(kwargs.get(k))},
            stream=streaming,
        )

    def end(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        raw, parsed = unwrap_raw(result, kwargs)
        if parsed is None:
            # with_streaming_response (the body is unread and belongs to the
            # caller) or an unparsable raw response: headers only.
            call.finish({"request_id": read(result, "request_id")})
            return result
        if kwargs.get("stream") is True and stream_cls is not None:
            headers = read(parsed, "response", "headers")
            request_id = headers.get("x-request-id") if headers is not None else None
            state = stream_cls(call, inst, request_id)
            instrumented = instrument_stream(
                parsed, state.on_item, state.on_end, is_async=is_async,
            )
            if raw is not None:
                if instrumented is not parsed:
                    # The stream could not be instrumented in place and the
                    # caller holds the raw response, not our proxy.
                    call.finish({"request_id": request_id})
                return result
            return instrumented
        call.finish(info_fn(parsed, inst.captures_content))
        return result

    def factory(original: Any) -> Any:
        make = async_method if is_async else sync_method
        return make(original, begin, end)

    return factory


_SURFACES: Dict[str, Dict[str, Any]] = {
    "chat": {
        "module": "openai.resources.chat.completions",
        "classes": ("Completions", "AsyncCompletions"),
        "methods": ("create", "parse"),
        "decision_type": "llm.openai.chat",
        "function_name": "openai.chat.completions",
        "params": _CHAT_PARAMS,
        "input_keys": ("messages", "tools"),
        "info_fn": _chat_info,
        "stream_cls": _ChatStream,
    },
    "responses": {
        "module": "openai.resources.responses",
        "classes": ("Responses", "AsyncResponses"),
        "methods": ("create", "parse"),
        "decision_type": "llm.openai.responses",
        "function_name": "openai.responses",
        "params": _RESPONSES_PARAMS,
        "input_keys": ("instructions", "input", "tools"),
        "info_fn": _responses_info,
        "stream_cls": _ResponsesStream,
    },
    "completions": {
        "module": "openai.resources.completions",
        "classes": ("Completions", "AsyncCompletions"),
        "methods": ("create",),
        "decision_type": "llm.openai.completions",
        "function_name": "openai.completions",
        "params": _COMPLETIONS_PARAMS,
        "input_keys": ("prompt",),
        "info_fn": _completions_info,
        "stream_cls": _ChatStream,
    },
    "embeddings": {
        "module": "openai.resources.embeddings",
        "classes": ("Embeddings", "AsyncEmbeddings"),
        "methods": ("create",),
        "decision_type": "llm.openai.embeddings",
        "function_name": "openai.embeddings",
        "params": _EMBEDDINGS_PARAMS,
        "input_keys": ("input",),
        "info_fn": _embeddings_info,
        "stream_cls": None,
    },
}


def instrument(*, stream_usage: bool = False, **options: Any) -> ProviderInstrumentor:
    """Patch the openai SDK. Returns the instrumentor; call its
    ``uninstall()`` (or ``briefcase.auto.undo("openai")``) to restore.

    Args:
        stream_usage: When True, chat streams that set no ``stream_options``
            are sent ``{"include_usage": True}`` so token usage is recorded.
            Off by default because it adds a final usage-only chunk (empty
            ``choices``) to the stream the application reads.
        **options: See :class:`ProviderInstrumentor`.

    Raises:
        ImportError: ``openai`` is not installed.
    """
    try:
        import openai as sdk
    except ImportError as exc:
        raise ImportError(
            "briefcase.auto: openai is not installed, so nothing was "
            "instrumented. Install with: pip install openai"
        ) from exc

    inst = ProviderInstrumentor(PROVIDER, **options)
    inst.sdk_version = getattr(sdk, "__version__", None)
    for name, surface in _SURFACES.items():
        for class_name in surface["classes"]:
            for method in surface["methods"]:
                inst.patch(
                    surface["module"], class_name, method,
                    _factory(
                        inst,
                        decision_type=surface["decision_type"],
                        function_name=f"{surface['function_name']}.{method}",
                        is_async=class_name.startswith("Async"),
                        params=surface["params"],
                        input_keys=surface["input_keys"],
                        info_fn=surface["info_fn"],
                        stream_cls=surface["stream_cls"],
                        stream_usage=stream_usage and name == "chat",
                    ),
                )
    return inst


__all__ = ["PROVIDER", "VERIFIED_VERSION", "instrument"]
