"""Auto-instrumentation for the ``google-genai`` Python SDK (``google.genai``).

Verified against ``google-genai==3.6.1`` by inspecting the installed
package. Patch targets (each is checked at install time; a missing one is
skipped with a WARNING), all recorded as
``decision_type="llm.google_genai.generate_content"``:

- ``google.genai.models.Models.generate_content`` (plain function)
- ``google.genai.models.Models.generate_content_stream`` (generator
  function; the wrapper returns an iterator over the same chunks)
- ``google.genai.models.AsyncModels.generate_content`` (coroutine function)
- ``google.genai.models.AsyncModels.generate_content_stream`` (coroutine
  function that returns an async iterator)

Layout facts this module relies on, as found in 3.6.1:

- All four take keyword-only ``model``, ``contents`` and ``config``.
  ``client.chats`` sends through the same ``Models`` methods, so chat
  sessions are covered.
- ``GenerateContentResponse`` carries ``usage_metadata``
  (``prompt_token_count``, ``candidates_token_count``, ``total_token_count``,
  ``cached_content_token_count``, ``thoughts_token_count``),
  ``model_version``, ``response_id`` and ``candidates[0].finish_reason``
  (an enum). Every stream chunk is a ``GenerateContentResponse``; usage is
  taken from the last chunk that reports it.
- The SDK exposes no request id on the response, so ``request_id`` is not
  recorded; ``response_id`` is.

Automatic function calling makes several HTTP requests inside one
``generate_content`` call; that is recorded as one decision, with the usage
of the final response.
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
    atee,
    read,
    scalar_params,
    sync_method,
    tee,
    text_accumulator,
    token_usage,
)

logger = get_logger(__name__)

PROVIDER = "google-genai"
VERIFIED_VERSION = "3.6.1"
DECISION_TYPE = "llm.google_genai.generate_content"

_MODULE = "google.genai.models"
_PARAMS = (
    "temperature", "top_p", "top_k", "max_output_tokens", "candidate_count", "seed",
)


def _usage(metadata: Any) -> Optional[Dict[str, int]]:
    return token_usage(
        read(metadata, "prompt_token_count"),
        read(metadata, "candidates_token_count"),
        read(metadata, "total_token_count"),
        cached_content_token_count=read(metadata, "cached_content_token_count"),
        thoughts_token_count=read(metadata, "thoughts_token_count"),
    )


def _first_candidate(response: Any) -> Any:
    candidates = read(response, "candidates")
    if isinstance(candidates, (list, tuple)) and candidates:
        return candidates[0]
    return None


def _finish_reason(response: Any) -> Optional[str]:
    reason = read(_first_candidate(response), "finish_reason")
    if reason is None:
        return None
    value = getattr(reason, "value", reason)
    return str(value)


def _text(response: Any) -> str:
    """Concatenated text parts of the first candidate. Reads the parts
    directly: the ``.text`` property logs a warning on non-text parts."""
    parts = read(_first_candidate(response), "content", "parts")
    pieces = []
    for part in parts if isinstance(parts, (list, tuple)) else ():
        text = read(part, "text")
        if isinstance(text, str) and not read(part, "thought"):
            pieces.append(text)
    return "".join(pieces)


def _info(response: Any, want_content: bool) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "model": read(response, "model_version"),
        "response_id": read(response, "response_id"),
        "finish_reason": _finish_reason(response),
        "usage": _usage(read(response, "usage_metadata")),
    }
    if want_content:
        info["content"] = {"content": _text(response)}
    return info


class _GenerateStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor) -> None:
        super().__init__(call)
        self.want_content = inst.captures_content
        self.add_text, self.get_text = text_accumulator(inst.max_content_chars)
        self.data: Dict[str, Any] = {}

    def observe(self, chunk: Any) -> None:
        for key, value in _info(chunk, False).items():
            if value is not None:
                self.data[key] = value
        if self.want_content:
            self.add_text(_text(chunk))

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


def _begin(inst: ProviderInstrumentor, function_name: str, kwargs: dict,
           stream: bool) -> Optional[ProviderCall]:
    config = kwargs.get("config")

    def inputs() -> Dict[str, Any]:
        content: Dict[str, Any] = {"contents": kwargs.get("contents")}
        system = read(config, "system_instruction")
        if system is not None:
            content["system_instruction"] = system
        return content

    return inst.begin(
        DECISION_TYPE, function_name,
        model=kwargs.get("model"),
        params=scalar_params(config, _PARAMS) if config is not None else {},
        inputs=inputs,
        stream=stream,
    )


def _call_factory(inst: ProviderInstrumentor, function_name: str,
                  is_async: bool) -> Callable[[Any], Any]:
    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        return _begin(inst, function_name, kwargs, False)

    def end(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        call.finish(_info(result, inst.captures_content))
        return result

    def factory(original: Any) -> Any:
        return (async_method if is_async else sync_method)(original, begin, end)

    return factory


def _stream_factory(inst: ProviderInstrumentor, function_name: str,
                    is_async: bool) -> Callable[[Any], Any]:
    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        return _begin(inst, function_name, kwargs, True)

    def end(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        state = _GenerateStream(call, inst)
        if is_async:
            return atee(result.__aiter__(), state.on_item, state.on_end)
        return tee(iter(result), state.on_item, state.on_end)

    def factory(original: Any) -> Any:
        if is_async:
            return async_method(original, begin, end)

        # The sync original is a generator function: calling it does no
        # work until the first next(). sync_method keeps that laziness (it
        # only creates the generator) and an error raised while iterating
        # is reported by tee().
        return functools.wraps(original)(sync_method(original, begin, end))

    return factory


def instrument(**options: Any) -> ProviderInstrumentor:
    """Patch the google-genai SDK. Returns the instrumentor; call its
    ``uninstall()`` (or ``briefcase.auto.undo("google-genai")``) to restore.

    Args:
        **options: See :class:`ProviderInstrumentor`.

    Raises:
        ImportError: ``google-genai`` is not installed.
    """
    try:
        from google import genai as sdk
    except ImportError as exc:
        raise ImportError(
            "briefcase.auto: google-genai is not installed, so nothing was "
            "instrumented. Install with: pip install google-genai"
        ) from exc

    inst = ProviderInstrumentor(PROVIDER, **options)
    inst.sdk_version = getattr(sdk, "__version__", None)
    for class_name, prefix in (("Models", "google.genai.models"),
                               ("AsyncModels", "google.genai.aio.models")):
        is_async = class_name.startswith("Async")
        inst.patch(_MODULE, class_name, "generate_content",
                   _call_factory(inst, f"{prefix}.generate_content", is_async))
        inst.patch(_MODULE, class_name, "generate_content_stream",
                   _stream_factory(inst, f"{prefix}.generate_content_stream", is_async))
    return inst


__all__ = ["DECISION_TYPE", "PROVIDER", "VERIFIED_VERSION", "instrument"]
