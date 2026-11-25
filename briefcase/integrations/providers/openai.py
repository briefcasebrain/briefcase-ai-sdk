"""Auto-instrumentation for the ``openai`` Python SDK.

Tested with ``openai==3.57.0`` (the version this module's test-suite runs
against); reported working with 3.3.0 in another environment. Nothing here
is pinned to a version: every patch target is resolved and its signature
checked at install time, and a surface whose layout differs is left
untouched and reported (``briefcase.auto.status()``), never half-patched.

Surfaces (each is patched all-or-nothing, sync and async together):

=============  ======================================  ==========================
surface        targets                                 decision_type
=============  ======================================  ==========================
chat           ``resources.chat.completions``          ``llm.openai.chat``
               ``(Async)Completions.create`` / ``.parse``
responses      ``resources.responses``                 ``llm.openai.responses``
               ``(Async)Responses.create`` / ``.parse``
completions    ``resources.completions``               ``llm.openai.completions``
               ``(Async)Completions.create``
embeddings     ``resources.embeddings``                ``llm.openai.embeddings``
               ``(Async)Embeddings.create``
=============  ======================================  ==========================

Layout facts this module relies on, as found in 3.57.0:

- ``chat.completions.stream()`` and ``responses.stream()`` call
  ``resource.create(..., stream=True)``, so they are covered by the
  ``create`` patches; ``parse()`` posts directly and is patched itself.
- ``create(stream=True)`` returns ``openai.Stream`` / ``openai.AsyncStream``,
  which iterate a private ``_iterator`` generator. It is swapped for a
  reporting generator on the instance, so the caller keeps the SDK's own
  object. If a future SDK has no such attribute the stream is handed back
  untouched and the record says it was not observed (``capture_limits``).
- ``with_raw_response`` and ``with_streaming_response``: see
  :mod:`._stainless`.
- Usage fields: ``CompletionUsage`` (``prompt_tokens``, ``completion_tokens``,
  ``total_tokens``, ``prompt_tokens_details.cached_tokens`` /
  ``.cache_write_tokens`` / ``.audio_tokens``, ``completion_tokens_details.reasoning_tokens`` /
  ``.audio_tokens`` / ``.accepted_prediction_tokens`` /
  ``.rejected_prediction_tokens``) and ``ResponseUsage`` (``input_tokens``,
  ``output_tokens``, ``total_tokens``, ``input_tokens_details.cached_tokens`` /
  ``.cache_write_tokens``, ``output_tokens_details.reasoning_tokens``).
  ``cached_tokens`` and ``cache_write_tokens`` are parts of the prompt /
  input count, recorded next to it (checked against the published OpenAPI
  spec, ``tests/contracts``).

Streaming usage (chat and legacy completions)
---------------------------------------------

A chat stream only carries usage when the request sets
``stream_options={"include_usage": True}``. By default this module does not
set it: the record of such a stream has ``token_usage: None`` and a
``capture_limits`` note, which is the truth.

Setting it for the application is not free of side effects: the API then
sends one more chunk, with an empty ``choices`` list (code doing
``chunk.choices[0]`` breaks), and adds ``"usage": null`` to every other
chunk (code that re-serialises chunks forwards a field it did not before);
and an OpenAI-compatible or older Azure endpoint may reject the parameter
with a 400. So ``stream_usage=True`` (or ``BRIEFCASE_OPENAI_STREAM_USAGE=1``)
injects the option only when all of these hold, and hides both effects:

- the request has no ``stream_options`` of its own;
- the client's ``base_url`` host is in ``stream_usage_hosts`` (default
  ``api.openai.com`` only), so no other endpoint is ever sent a parameter
  it did not ask for;
- the call is a plain ``create(stream=True)`` (this includes the
  ``.stream()`` helper), not a raw-response mode where the application
  reads the bytes itself.

The usage-only chunk is then consumed by the wrapper and never yielded, and
the ``usage`` marker is removed from the other chunks' ``model_fields_set``,
so the application iterates and serialises exactly what it would have
without the option. Injection is still opt-in because it relies on that
clean-up matching the SDK's chunk model.

Not covered: bound methods or ``with_raw_response`` objects an application
created *before* ``auto()`` ran (they hold the unpatched function), the
Assistants / Realtime / Batch / images / audio APIs.

``AzureOpenAI`` uses the same resource classes and is covered.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Iterable, List, Optional

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import (
    SKIP,
    PatchTarget,
    ProviderCall,
    ProviderInstrumentor,
    StreamState,
    async_method,
    given,
    read,
    scalar_params,
    sync_method,
    text_accumulator,
    token_usage,
)
from briefcase.integrations.providers import _requests
from briefcase.integrations.providers._stainless import Surface, raw_mode

logger = get_logger(__name__)

PROVIDER = "openai"
#: Versions the test-suite of this module was run against.
TESTED_VERSIONS = ("3.57.0",)
#: Versions reported working outside the test-suite (not re-verified here).
REPORTED_VERSIONS = ("3.3.0",)
VERIFIED_VERSION = TESTED_VERSIONS[-1]
ENV_STREAM_USAGE = "BRIEFCASE_OPENAI_STREAM_USAGE"
DEFAULT_STREAM_USAGE_HOSTS = ("api.openai.com",)
REQUEST_ID_HEADER = "x-request-id"

LIMIT_NO_STREAM_USAGE = (
    "stream carried no usage: the request did not set "
    "stream_options={'include_usage': True} (see the stream_usage option)"
)
LIMIT_FIRST_CHOICE = "content of the first choice only (n > 1)"

# The dict injected as stream_options; its identity marks an injected request.
_INJECTED_STREAM_OPTIONS: Dict[str, Any] = {"include_usage": True}

_CHAT_PARAMS = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "seed",
    "frequency_penalty", "presence_penalty", "reasoning_effort", "service_tier", "verbosity",
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

def _completion_usage(usage: Any) -> Optional[Dict[str, Optional[int]]]:
    prompt_details = read(usage, "prompt_tokens_details")
    completion_details = read(usage, "completion_tokens_details")
    return token_usage(
        read(usage, "prompt_tokens"),
        read(usage, "completion_tokens"),
        read(usage, "total_tokens"),
        cached_tokens=read(prompt_details, "cached_tokens"),
        cache_write_tokens=read(prompt_details, "cache_write_tokens"),
        audio_prompt_tokens=read(prompt_details, "audio_tokens"),
        reasoning_tokens=read(completion_details, "reasoning_tokens"),
        audio_completion_tokens=read(completion_details, "audio_tokens"),
        accepted_prediction_tokens=read(completion_details, "accepted_prediction_tokens"),
        rejected_prediction_tokens=read(completion_details, "rejected_prediction_tokens"),
    )


def _response_usage(usage: Any) -> Optional[Dict[str, Optional[int]]]:
    return token_usage(
        read(usage, "input_tokens"),
        read(usage, "output_tokens"),
        read(usage, "total_tokens"),
        cached_tokens=read(usage, "input_tokens_details", "cached_tokens"),
        cache_write_tokens=read(usage, "input_tokens_details", "cache_write_tokens"),
        reasoning_tokens=read(usage, "output_tokens_details", "reasoning_tokens"),
    )


def _choices(obj: Any) -> List[Any]:
    choices = read(obj, "choices")
    return list(choices) if isinstance(choices, (list, tuple)) else []


def _first_choice(obj: Any) -> Any:
    choices = _choices(obj)
    return choices[0] if choices else None


def _choice_extra(choices: List[Any]) -> Dict[str, Any]:
    """Counts for a multi-choice (``n > 1``) response; nothing for one."""
    if len(choices) <= 1:
        return {}
    return {
        "choice_count": len(choices),
        "finish_reasons": [
            str(read(c, "finish_reason")) if read(c, "finish_reason") is not None else None
            for c in choices[:50]
        ],
    }


def _chat_info(result: Any, want_content: bool) -> Dict[str, Any]:
    choices = _choices(result)
    choice = choices[0] if choices else None
    message = read(choice, "message")
    tool_calls = read(message, "tool_calls")
    extra: Dict[str, Any] = {"service_tier": read(result, "service_tier")}
    extra.update(_choice_extra(choices))
    if isinstance(tool_calls, (list, tuple)) and tool_calls:
        extra["tool_call_count"] = len(tool_calls)
    if read(message, "refusal"):
        extra["refusal"] = True
    info: Dict[str, Any] = {
        "model": read(result, "model"),
        "response_id": read(result, "id"),
        "request_id": read(result, "_request_id"),
        "finish_reason": read(choice, "finish_reason"),
        "usage": _completion_usage(read(result, "usage")),
        "extra": extra,
    }
    if want_content:
        content: Dict[str, Any] = {"content": read(message, "content")}
        if tool_calls:
            content["tool_calls"] = tool_calls
        refusal = read(message, "refusal")
        if refusal:
            content["refusal"] = refusal
        info["content"] = content
        if len(choices) > 1:
            info["limits"] = [LIMIT_FIRST_CHOICE]
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
    choices = _choices(result)
    choice = choices[0] if choices else None
    info: Dict[str, Any] = {
        "model": read(result, "model"),
        "response_id": read(result, "id"),
        "request_id": read(result, "_request_id"),
        "finish_reason": read(choice, "finish_reason"),
        "usage": _completion_usage(read(result, "usage")),
        "extra": _choice_extra(choices),
    }
    if want_content:
        info["content"] = {"content": read(choice, "text")}
        if len(choices) > 1:
            info["limits"] = [LIMIT_FIRST_CHOICE]
    return info


def _embeddings_info(result: Any, want_content: bool) -> Dict[str, Any]:
    usage = read(result, "usage")
    data = read(result, "data")
    return {
        "model": read(result, "model"),
        "request_id": read(result, "_request_id"),
        # An embedding has no completion side: 0 is a fact here, not a guess.
        "usage": token_usage(
            read(usage, "prompt_tokens"),
            0 if read(usage, "prompt_tokens") is not None else None,
            read(usage, "total_tokens"),
        ),
        "extra": {
            "embedding_count": len(data) if isinstance(data, (list, tuple)) else None,
        },
    }


# -- stream readers ---------------------------------------------------------

class _OpenAIStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = call.want_content
        self.add_text, self.get_text = text_accumulator(call.text_limit)
        self.data: Dict[str, Any] = {"request_id": request_id}

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


class _ChatStream(_OpenAIStream):
    """Reads ``ChatCompletionChunk`` (and legacy ``Completion``) chunks.

    With ``hide_usage`` (the request's ``stream_options`` were injected by
    this module) the usage-only chunk is consumed here and the ``usage``
    marker removed from the others, so the consumer sees the stream it
    would have seen without the option."""

    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any,
                 hide_usage: bool = False) -> None:
        super().__init__(call, inst, request_id)
        self.hide_usage = hide_usage
        self.open_choices: set = set()
        self.closed_choices: set = set()

    def observe(self, chunk: Any) -> Any:
        data = self.data
        model = read(chunk, "model")
        if model:
            data["model"] = model
        chunk_id = read(chunk, "id")
        if chunk_id:
            data["response_id"] = chunk_id
        raw_usage = read(chunk, "usage")
        usage = _completion_usage(raw_usage)
        if usage:
            data["usage"] = usage
        choices = _choices(chunk)
        for position, choice in enumerate(choices):
            index = read(choice, "index")
            index = index if isinstance(index, int) else position
            self.open_choices.add(index)
            finish = read(choice, "finish_reason")
            if finish:
                self.closed_choices.add(index)
                if index == 0 or "finish_reason" not in data:
                    data["finish_reason"] = finish
            if index == 0 and self.want_content:
                self.add_text(read(choice, "delta", "content") or read(choice, "text"))
        if self.open_choices and self.open_choices <= self.closed_choices:
            self.finished = True
        if self.hide_usage:
            if not choices and raw_usage is not None:
                return SKIP
            if raw_usage is None:
                # "usage": null on a content chunk exists only because of
                # the injected option; un-mark it so model_dump()/to_json()
                # (exclude_unset) are unchanged.
                fields_set = getattr(chunk, "__pydantic_fields_set__", None)
                if isinstance(fields_set, set):
                    fields_set.discard("usage")
        return None

    def info(self) -> Dict[str, Any]:
        info = super().info()
        if len(self.open_choices) > 1:
            info["extra"] = {"choice_count": len(self.open_choices)}
        if "usage" not in info and (self.finished or self.count):
            info["limits"] = [LIMIT_NO_STREAM_USAGE]
        return info


class _ResponsesStream(_OpenAIStream):
    """Reads ``ResponseStreamEvent`` events; the terminal event
    (``response.completed`` / ``response.incomplete`` / ``response.failed``)
    carries the full ``Response`` with usage."""

    _TERMINAL = ("response.completed", "response.incomplete", "response.failed")

    def observe(self, event: Any) -> Any:
        kind = read(event, "type")
        if kind in self._TERMINAL or kind == "response.created":
            response = read(event, "response")
            if response is not None:
                request_id = self.data.get("request_id")
                self.data = {
                    k: v for k, v in _responses_info(response, False).items() if v is not None
                }
                self.data.setdefault("request_id", request_id)
            if kind in self._TERMINAL:
                self.finished = True
        elif kind == "response.output_text.delta" and self.want_content:
            self.add_text(read(event, "delta"))
        return None


# -- wrappers ---------------------------------------------------------------

def _host_of(resource: Any) -> Optional[str]:
    """Host of the client a resource object belongs to."""
    host = read(resource, "_client", "base_url", "host")
    return host.lower() if isinstance(host, str) else None


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
    stream_usage_hosts: frozenset = frozenset(),
    spec: Optional[_requests.RequestSpec] = None,
) -> Callable[[Any], Any]:

    def begin(args: tuple, kwargs: dict) -> Optional[ProviderCall]:
        streaming = kwargs.get("stream") is True
        call = inst.begin(
            decision_type, function_name,
            model=kwargs.get("model"),
            params=scalar_params(kwargs, params),
            inputs=lambda: {k: kwargs[k] for k in input_keys if given(kwargs.get(k))},
            stream=streaming,
        )
        if (
            call is not None and streaming and stream_usage_hosts
            and not given(kwargs.get("stream_options"))
            and raw_mode(kwargs) is None
            and args and _host_of(args[0]) in stream_usage_hosts
        ):
            # kwargs is this wrapper's own dict; the caller's is untouched.
            kwargs["stream_options"] = _INJECTED_STREAM_OPTIONS
        return call

    def state_for(kwargs: dict) -> Optional[Callable[[ProviderCall, Any], StreamState]]:
        if stream_cls is None:
            return None
        if stream_cls is _ChatStream:
            hide = kwargs.get("stream_options") is _INJECTED_STREAM_OPTIONS
            return lambda call, request_id: _ChatStream(call, inst, request_id, hide_usage=hide)
        return lambda call, request_id: stream_cls(call, inst, request_id)

    def surface(kwargs: dict) -> Surface:
        return Surface(inst, is_async=is_async, info_fn=info_fn, state_fn=state_for(kwargs),
                       request_id_header=REQUEST_ID_HEADER)

    def end(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        return surface(kwargs).end(call, result, args, kwargs)

    async def aend(call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
        return await surface(kwargs).aend(call, result, args, kwargs)

    def factory(original: Any) -> Any:
        if is_async:
            return async_method(original, begin, end, aend, spec=spec)
        return sync_method(original, begin, end, spec=spec)

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
        "spec": _requests.OPENAI_CHAT,
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
        "spec": _requests.OPENAI_RESPONSES,
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
        "spec": _requests.OPENAI_COMPLETIONS,
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
        "spec": _requests.OPENAI_EMBEDDINGS,
    },
}


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def instrument(
    *,
    stream_usage: Optional[bool] = None,
    stream_usage_hosts: Iterable[str] = DEFAULT_STREAM_USAGE_HOSTS,
    **options: Any,
) -> ProviderInstrumentor:
    """Patch the openai SDK. Returns the instrumentor; call its
    ``uninstall()`` (or ``briefcase.auto.undo("openai")``) to restore.

    Args:
        stream_usage: When True, plain chat / completions streams that set
            no ``stream_options`` are sent ``{"include_usage": True}`` so
            token usage is recorded, and the extra chunk that produces is
            hidden from the application (see the module docstring). Only
            for clients whose ``base_url`` host is in ``stream_usage_hosts``.
            ``None`` (default) reads ``BRIEFCASE_OPENAI_STREAM_USAGE``; off
            when that is unset, and streams are then recorded with
            ``token_usage: None``.
        stream_usage_hosts: Hosts that accept ``stream_options``. Default
            ``("api.openai.com",)``.
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

    if isinstance(stream_usage_hosts, str):
        stream_usage_hosts = (stream_usage_hosts,)
    if stream_usage is None:
        stream_usage = _env_flag(ENV_STREAM_USAGE)
    hosts = frozenset(h.lower() for h in stream_usage_hosts) if stream_usage else frozenset()

    inst = ProviderInstrumentor(PROVIDER, **options)
    inst.sdk_version = getattr(sdk, "__version__", None)
    inst.tested_versions = TESTED_VERSIONS
    inst.reported_versions = REPORTED_VERSIONS
    inst.options = {"stream_usage": bool(stream_usage), "stream_usage_hosts": sorted(hosts)}
    inst.limits = [
        "with_streaming_response: usage only when the caller uses .parse()",
        "objects created before auto() that hold a bound method "
        "(e.g. client.chat.completions.with_raw_response) are not recorded",
    ]
    if not stream_usage:
        inst.limits.insert(0, "chat/completions streams: token_usage is None unless the "
                              "request sets stream_options.include_usage (stream_usage=False)")
    for name, surface in _SURFACES.items():
        targets = []
        for class_name in surface["classes"]:
            for method in surface["methods"]:
                streams = surface["stream_cls"] is not None and method == "create"
                targets.append(PatchTarget(
                    surface["module"], class_name, method,
                    _factory(
                        inst,
                        decision_type=surface["decision_type"],
                        function_name=f"{surface['function_name']}.{method}",
                        is_async=class_name.startswith("Async"),
                        params=surface["params"],
                        input_keys=surface["input_keys"],
                        info_fn=surface["info_fn"],
                        stream_cls=surface["stream_cls"] if streams else None,
                        stream_usage_hosts=(
                            hosts if streams and name in ("chat", "completions") else frozenset()
                        ),
                        spec=surface["spec"],
                    ),
                    requires=("model", "stream") if streams else ("model",),
                ))
        inst.patch_group(name, targets)
    inst.report_skipped()
    return inst


__all__ = ["PROVIDER", "REPORTED_VERSIONS", "TESTED_VERSIONS", "VERIFIED_VERSION", "instrument"]
