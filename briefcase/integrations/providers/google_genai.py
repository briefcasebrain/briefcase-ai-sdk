"""Auto-instrumentation for the ``google-genai`` Python SDK (``google.genai``).

Tested with ``google-genai==3.6.1`` (the version this module's test-suite
runs against). Every patch target is resolved and its signature checked at
install time; if any differs, none is patched and the gap is reported
(``briefcase.auto.status()``). Targets, one all-or-nothing group, all
recorded as ``decision_type="llm.google_genai.generate_content"``:

- ``google.genai.models.Models.generate_content`` (plain function)
- ``google.genai.models.Models.generate_content_stream`` (generator
  function; the wrapper is a generator function too, so like the original
  it sends nothing, and records nothing, until the first ``next()``)
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
of the *final* response only (earlier round-trips are not counted: a known
under-count). Errors are ``google.genai.errors.APIError`` with the HTTP
status in ``code``. Not covered: ``embed_content``, ``count_tokens``,
image / video generation, the Live API.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from briefcase._logging import get_logger
from briefcase.integrations.providers import _requests
from briefcase.integrations.providers._base import (
    PatchTarget,
    ProviderCall,
    ProviderInstrumentor,
    StreamState,
    async_method,
    atee,
    read,
    scalar_params,
    sync_generator_method,
    sync_method,
    text_accumulator,
    token_usage,
)

logger = get_logger(__name__)

PROVIDER = "google-genai"
TESTED_VERSIONS = ("3.6.1",)
VERIFIED_VERSION = TESTED_VERSIONS[-1]
DECISION_TYPE = "llm.google_genai.generate_content"

_MODULE = "google.genai.models"
_PARAMS = (
    "temperature", "top_p", "top_k", "max_output_tokens", "candidate_count", "seed",
    "frequency_penalty", "presence_penalty",
)


def _usage(metadata: Any) -> Optional[Dict[str, Optional[int]]]:
    return token_usage(
        read(metadata, "prompt_token_count"),
        read(metadata, "candidates_token_count"),
        read(metadata, "total_token_count"),
        cached_content_token_count=read(metadata, "cached_content_token_count"),
        thoughts_token_count=read(metadata, "thoughts_token_count"),
        tool_use_prompt_token_count=read(metadata, "tool_use_prompt_token_count"),
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
        self.want_content = call.want_content
        self.add_text, self.get_text = text_accumulator(call.text_limit)
        self.data: Dict[str, Any] = {}

    def observe(self, chunk: Any) -> Any:
        for key, value in _info(chunk, False).items():
            if value is not None:
                self.data[key] = value
        if self.data.get("finish_reason"):
            self.finished = True
        if self.want_content:
            self.add_text(_text(chunk))
        return None

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
        call.finish(_info(result, call.want_content))
        return result

    def factory(original: Any) -> Any:
        if is_async:
            return async_method(original, begin, end, spec=_requests.GOOGLE_GENERATE)
        return sync_method(original, begin, end, spec=_requests.GOOGLE_GENERATE)

    return factory


def _stream_factory(inst: ProviderInstrumentor, function_name: str,
                    is_async: bool) -> Callable[[Any], Any]:
    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        return _begin(inst, function_name, kwargs, True)

    def end(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        state = _GenerateStream(call, inst)
        return atee(result.__aiter__(), state.on_item, state.on_end)

    def factory(original: Any) -> Any:
        if is_async:
            # A coroutine function returning an async generator: the
            # request is sent when it is awaited, and so is the record
            # started.
            return async_method(original, begin, end, spec=_requests.GOOGLE_GENERATE_STREAM)
        return sync_generator_method(original, begin, lambda call: _GenerateStream(call, inst),
                                     spec=_requests.GOOGLE_GENERATE_STREAM)

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
    inst.tested_versions = TESTED_VERSIONS
    inst.limits = [
        "automatic function calling: usage of the final response only",
        "no request id is exposed by the SDK",
        "embed_content, count_tokens and the Live API are not recorded",
    ]
    targets = []
    for class_name, prefix in (("Models", "google.genai.models"),
                               ("AsyncModels", "google.genai.aio.models")):
        is_async = class_name.startswith("Async")
        targets.append(PatchTarget(
            _MODULE, class_name, "generate_content",
            _call_factory(inst, f"{prefix}.generate_content", is_async),
            ("model", "contents", "config")))
        targets.append(PatchTarget(
            _MODULE, class_name, "generate_content_stream",
            _stream_factory(inst, f"{prefix}.generate_content_stream", is_async),
            ("model", "contents", "config")))
    inst.patch_group("generate_content", targets)
    inst.report_skipped()
    return inst


__all__ = ["DECISION_TYPE", "PROVIDER", "TESTED_VERSIONS", "VERIFIED_VERSION", "instrument"]
