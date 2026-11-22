"""Request and response views of each instrumented SDK call, for the call
hooks (:mod:`briefcase.budgets`, :mod:`briefcase.guardrails.content`).

A :class:`RequestSpec` knows, for one operation of one SDK, where the model
id, the output-token cap and the prompt text live in the request, and where
the completion text lives in a (non-streaming) response. Everything here
reads; the only writes are:

- :meth:`RequestSpec.set_model` and :meth:`RequestSpec.rewrite_request`
  replace values in the *wrapper's own* keyword dict. Containers on the way
  to a changed string are copied (dicts and lists, and pydantic models via
  ``model_copy``), so the caller's objects are never mutated and every value
  keeps its type.
- :meth:`RequestSpec.redact_response` assigns new strings to the text
  attributes of the returned SDK object in place (same object, same type).

Texts are classified by ``source``: ``system``, ``user``, ``assistant``,
``tool`` (tool / function results) or ``retrieved`` (a string wrapped with
:class:`RetrievedText`).

Not inspected: images, audio, files and other non-text parts; tool-call
arguments the model produced; any request object of a type not listed here
(counted in ``uninspected``)."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.integrations.providers._base import given, read

Edit = Callable[[str, str], Optional[str]]

#: Keys whose values hold prompt text, at any depth.
TEXT_KEYS = frozenset({
    "content", "text", "output", "system", "instructions", "input", "prompt",
    "contents", "parts", "messages", "toolResult", "system_instruction",
    "inputText", "message",
})
#: Pydantic fields followed on SDK request models (google-genai ``Content`` / ``Part``).
_MODEL_FIELDS = ("parts", "text", "content", "contents")

_ROLE_SOURCE = {
    "system": "system", "developer": "system", "user": "user", "human": "user",
    "assistant": "assistant", "model": "assistant", "ai": "assistant",
    "tool": "tool", "function": "tool", "ipython": "tool",
}
_NON_TEXT_TYPES = frozenset({"image", "image_url", "input_image", "input_audio", "document",
                             "file", "input_file", "audio"})
_TOOL_TYPES = frozenset({"tool_result", "function_call_output", "tool", "function_result"})

#: A non-text part (image, audio, document) counted as this many input
#: tokens when estimating a request.
NON_TEXT_PART_TOKENS = 1600


class RetrievedText(str):
    """A string marked as retrieved content (a RAG chunk, a web page, a
    file): prompt-injection checks treat it as untrusted even when it sits
    in a user message. It is a ``str``; SDKs send it unchanged."""

    __slots__ = ()


class _Walk:
    """One pass over request content: collects ``(source, text)`` or
    applies an edit, copying containers that change."""

    def __init__(self, edit: Optional[Edit], keys: frozenset = TEXT_KEYS) -> None:
        self.edit = edit
        self.keys = keys
        self.texts: List[Tuple[str, str]] = []
        self.non_text = 0
        #: Values that could not be inspected (unknown object types).
        self.uninspected: List[str] = []
        #: A change was needed inside an object that cannot be copied.
        self.blocked = False

    def visit(self, value: Any, source: str) -> Any:
        if isinstance(value, str):
            src = "retrieved" if isinstance(value, RetrievedText) else source
            self.texts.append((src, value))
            if self.edit is None:
                return value
            new = self.edit(value, src)
            if new is None or new == value:
                return value
            return RetrievedText(new) if isinstance(value, RetrievedText) else new
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            self.non_text += 1
            return value
        if isinstance(value, dict):
            return self._dict(value, source)
        if isinstance(value, (list, tuple)):
            items = [self.visit(item, source) for item in value]
            if all(a is b for a, b in zip(items, value)):
                return value
            return type(value)(items) if isinstance(value, tuple) else items
        if not given(value):
            return value
        fields = getattr(type(value), "model_fields", None)
        if isinstance(fields, dict) and hasattr(value, "model_copy"):
            return self._model(value, fields, source)
        self.uninspected.append(type(value).__name__)
        return value

    def _source(self, role: Any, kind: Any, source: str) -> str:
        if isinstance(kind, str) and kind in _TOOL_TYPES:
            return "tool"
        if isinstance(role, str):
            return _ROLE_SOURCE.get(role.lower(), source)
        return source

    def _dict(self, value: Dict[str, Any], source: str) -> Any:
        src = self._source(value.get("role"), value.get("type"), source)
        non_text = value.get("type") in _NON_TEXT_TYPES
        out: Optional[Dict[str, Any]] = None
        for key, item in value.items():
            if key not in self.keys:
                if key in ("image", "image_url", "inline_data", "inlineData", "file_data",
                           "document", "video", "audio"):
                    non_text = True
                continue
            child_source = "tool" if key == "toolResult" else src
            new = self.visit(item, child_source)
            if new is not item:
                if out is None:
                    out = dict(value)
                out[key] = new
        if non_text:
            self.non_text += 1
        return out if out is not None else value

    def _model(self, value: Any, fields: Dict[str, Any], source: str) -> Any:
        src = self._source(getattr(value, "role", None), None, source)
        updates: Dict[str, Any] = {}
        for name in _MODEL_FIELDS:
            if name not in fields:
                continue
            item = getattr(value, name, None)
            if item is None:
                continue
            new = self.visit(item, src)
            if new is not item:
                updates[name] = new
        for name in ("inline_data", "file_data"):
            if name in fields and getattr(value, name, None) is not None:
                self.non_text += 1
        if not updates:
            return value
        try:
            return value.model_copy(update=updates)
        except Exception:
            self.blocked = True
            return value


class RequestSpec:
    """How the hooks see one SDK operation.

    ``fields``: ``(kwarg name, default source)`` pairs holding content.
    ``max_keys``: kwarg names of the output-token cap, first given wins.
    ``default_max``: the cap the API applies when none is given (None:
    unknown, the budget falls back to the model's maximum).
    """

    def __init__(self, provider: str, operation: str, *,
                 fields: Tuple[Tuple[str, str], ...],
                 model_key: str = "model",
                 max_keys: Tuple[str, ...] = (),
                 default_max: Optional[int] = None,
                 stream: Any = "kwarg",
                 response: Optional[str] = None,
                 n_key: Optional[str] = None,
                 output_checks: bool = True) -> None:
        self.provider = provider
        self.operation = operation
        self.fields = fields
        self.model_key = model_key
        self.max_keys = max_keys
        self.default_max = default_max
        self._stream = stream
        self.response = response
        self.n_key = n_key
        #: Whether the completion text reaches the hooks before the result
        #: reaches the application (False: output checks cannot act).
        self.output_checks = output_checks

    def __repr__(self) -> str:
        return f"RequestSpec({self.provider}.{self.operation})"

    # -- request ------------------------------------------------------------

    def is_stream(self, kwargs: Dict[str, Any]) -> bool:
        if self._stream == "kwarg":
            return kwargs.get("stream") is True
        return bool(self._stream)

    def get_model(self, kwargs: Dict[str, Any]) -> Optional[str]:
        model = kwargs.get(self.model_key)
        return model if isinstance(model, str) and model else None

    def set_model(self, kwargs: Dict[str, Any], model: str) -> None:
        kwargs[self.model_key] = model

    def max_output_tokens(self, kwargs: Dict[str, Any]) -> Optional[int]:
        for key in self.max_keys:
            value = kwargs.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value * self._choices(kwargs)
        if self.default_max is not None:
            return self.default_max * self._choices(kwargs)
        return None

    def _choices(self, kwargs: Dict[str, Any]) -> int:
        n = kwargs.get(self.n_key) if self.n_key else None
        return n if isinstance(n, int) and not isinstance(n, bool) and n > 1 else 1

    def _walk(self, kwargs: Dict[str, Any], edit: Optional[Edit]) -> _Walk:
        walk = _Walk(edit)
        for key, source in self.fields:
            value = kwargs.get(key)
            if value is None or not given(value):
                continue
            new = walk.visit(value, source)
            if new is not value and edit is not None:
                kwargs[key] = new
        return walk

    def request_texts(self, kwargs: Dict[str, Any]) -> List[Tuple[str, str]]:
        return self._walk(kwargs, None).texts

    def inspect(self, kwargs: Dict[str, Any]) -> _Walk:
        """The collected texts plus counts of non-text and uninspected parts."""
        return self._walk(kwargs, None)

    def rewrite_request(self, kwargs: Dict[str, Any], edit: Edit) -> bool:
        trial = dict(kwargs)
        walk = self._walk(trial, edit)
        if walk.blocked or walk.uninspected:
            return False
        kwargs.update(trial)
        return True

    # -- response -----------------------------------------------------------

    def response_redactable(self, kwargs: Dict[str, Any]) -> bool:
        if self.response is None or self.is_stream(kwargs):
            return False
        headers = kwargs.get("extra_headers")
        try:
            if headers is not None and headers.get("X-Stainless-Raw-Response"):
                return False  # with_raw_response / with_streaming_response
        except Exception:
            return False
        return True

    def redact_response(self, result: Any, fn: Callable[[str], str]) -> int:
        """Replace completion texts of ``result`` in place; returns how many
        strings were replaced."""
        redact = _RESPONSE_REDACTORS.get(self.response or "")
        return redact(result, fn) if redact is not None else 0


# -- response redactors (in place; the object keeps its type) ---------------

def _set_attr(obj: Any, name: str, fn: Callable[[str], str]) -> int:
    value = getattr(obj, name, None)
    if not isinstance(value, str):
        return 0
    new = fn(value)
    if new == value:
        return 0
    setattr(obj, name, new)
    return 1


def _items(value: Any) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _openai_chat(result: Any, fn: Callable[[str], str]) -> int:
    return sum(_set_attr(read(choice, "message"), "content", fn)
               for choice in _items(read(result, "choices")))


def _openai_completions(result: Any, fn: Callable[[str], str]) -> int:
    return sum(_set_attr(choice, "text", fn) for choice in _items(read(result, "choices")))


def _openai_responses(result: Any, fn: Callable[[str], str]) -> int:
    count = 0
    for item in _items(read(result, "output")):
        if read(item, "type") != "message":
            continue
        for part in _items(read(item, "content")):
            if read(part, "type") == "output_text":
                count += _set_attr(part, "text", fn)
    return count


def _anthropic(result: Any, fn: Callable[[str], str]) -> int:
    return sum(_set_attr(block, "text", fn) for block in _items(read(result, "content"))
               if read(block, "type") == "text")


def _google(result: Any, fn: Callable[[str], str]) -> int:
    count = 0
    for candidate in _items(read(result, "candidates")):
        for part in _items(read(candidate, "content", "parts")):
            count += _set_attr(part, "text", fn)
    return count


def _bedrock_converse(result: Any, fn: Callable[[str], str]) -> int:
    count = 0
    for block in _items(read(result, "output", "message", "content")):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            new = fn(block["text"])
            if new != block["text"]:
                block["text"] = new
                count += 1
    return count


_RESPONSE_REDACTORS: Dict[str, Callable[[Any, Callable[[str], str]], int]] = {
    "openai.chat": _openai_chat,
    "openai.completions": _openai_completions,
    "openai.responses": _openai_responses,
    "anthropic.messages": _anthropic,
    "google.generate": _google,
    "bedrock.converse": _bedrock_converse,
}


# -- google-genai: the system instruction lives inside ``config`` ------------

class _GoogleSpec(RequestSpec):
    def _walk(self, kwargs: Dict[str, Any], edit: Optional[Edit]) -> _Walk:
        walk = super()._walk(kwargs, edit)
        config = kwargs.get("config")
        system = read(config, "system_instruction")
        if system is None:
            return walk
        new = walk.visit(system, "system")
        if new is not system and edit is not None:
            if isinstance(config, dict):
                kwargs["config"] = dict(config, system_instruction=new)
            elif config is not None and hasattr(config, "model_copy"):
                kwargs["config"] = config.model_copy(update={"system_instruction": new})
            else:
                walk.blocked = True
        return walk

    def max_output_tokens(self, kwargs: Dict[str, Any]) -> Optional[int]:
        config = kwargs.get("config")
        value = read(config, "max_output_tokens")
        count = read(config, "candidate_count")
        n = count if isinstance(count, int) and count > 1 else 1
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value * n
        return None


# -- bedrock: botocore passes ``api_params`` as one dict --------------------

_BODY_MAX_KEYS = ("max_tokens", "max_tokens_to_sample", "max_gen_len", "max_new_tokens")


class _BedrockSpec(RequestSpec):
    def max_output_tokens(self, kwargs: Dict[str, Any]) -> Optional[int]:
        if self.operation.startswith("converse"):
            value = read(kwargs.get("inferenceConfig"), "maxTokens")
        else:
            body = _body_json(kwargs.get("body"))
            value = None
            for key in _BODY_MAX_KEYS:
                if isinstance(read(body, key), int):
                    value = read(body, key)
                    break
            if value is None:
                value = read(body, "textGenerationConfig", "maxTokenCount")
                if value is None:
                    value = read(body, "inferenceConfig", "maxTokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def _walk(self, kwargs: Dict[str, Any], edit: Optional[Edit]) -> _Walk:
        if self.operation.startswith("converse"):
            return super()._walk(kwargs, edit)
        walk = _Walk(edit)
        raw = kwargs.get("body")
        body = _body_json(raw)
        if body is None:
            if raw is not None:
                walk.uninspected.append(type(raw).__name__)
            return walk
        new = walk.visit(body, "user")
        if new is not body and edit is not None:
            text = json.dumps(new)
            kwargs["body"] = text.encode("utf-8") if isinstance(raw, (bytes, bytearray)) else text
        return walk


def _body_json(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except Exception:
            return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


# -- the specs ----------------------------------------------------------------

OPENAI_CHAT = RequestSpec(
    "openai", "chat", fields=(("messages", "user"),),
    max_keys=("max_completion_tokens", "max_tokens"), response="openai.chat", n_key="n")
OPENAI_RESPONSES = RequestSpec(
    "openai", "responses", fields=(("instructions", "system"), ("input", "user")),
    max_keys=("max_output_tokens",), response="openai.responses")
OPENAI_COMPLETIONS = RequestSpec(
    "openai", "completions", fields=(("prompt", "user"),),
    max_keys=("max_tokens",), default_max=16, response="openai.completions", n_key="n")
OPENAI_EMBEDDINGS = RequestSpec(
    "openai", "embeddings", fields=(("input", "user"),), default_max=0, stream=False)
ANTHROPIC_MESSAGES = RequestSpec(
    "anthropic", "messages", fields=(("system", "system"), ("messages", "user")),
    max_keys=("max_tokens",), response="anthropic.messages")
ANTHROPIC_STREAM = RequestSpec(
    "anthropic", "messages.stream", fields=(("system", "system"), ("messages", "user")),
    max_keys=("max_tokens",), stream=True)
GOOGLE_GENERATE = _GoogleSpec(
    "google-genai", "generate_content", fields=(("contents", "user"),), stream=False,
    response="google.generate")
GOOGLE_GENERATE_STREAM = _GoogleSpec(
    "google-genai", "generate_content_stream", fields=(("contents", "user"),), stream=True)
BEDROCK = {
    "Converse": _BedrockSpec("bedrock", "converse", fields=(("system", "system"), ("messages", "user")),
                             model_key="modelId", stream=False, response="bedrock.converse"),
    "ConverseStream": _BedrockSpec("bedrock", "converse_stream",
                                   fields=(("system", "system"), ("messages", "user")),
                                   model_key="modelId", stream=True),
    "InvokeModel": _BedrockSpec("bedrock", "invoke_model", fields=(), model_key="modelId",
                                stream=False, output_checks=False),
    "InvokeModelWithResponseStream": _BedrockSpec(
        "bedrock", "invoke_model_stream", fields=(), model_key="modelId", stream=True),
}


def estimate_input_tokens(spec: RequestSpec, kwargs: Dict[str, Any]) -> int:
    """A deliberately high estimate of a request's input tokens, for
    reserving budget before the call: UTF-8 bytes / 3 of every text (real
    tokenizers average about 4 characters per token in English), plus 4 per
    text for message framing and :data:`NON_TEXT_PART_TOKENS` per image,
    audio or file part. The reservation is settled to the provider's
    reported usage afterwards."""
    walk = spec.inspect(kwargs)
    total = sum(-(-len(text.encode("utf-8")) // 3) + 4 for _, text in walk.texts)
    return total + walk.non_text * NON_TEXT_PART_TOKENS + len(walk.uninspected) * NON_TEXT_PART_TOKENS


__all__ = [
    "ANTHROPIC_MESSAGES", "ANTHROPIC_STREAM", "BEDROCK", "GOOGLE_GENERATE",
    "GOOGLE_GENERATE_STREAM", "NON_TEXT_PART_TOKENS", "OPENAI_CHAT", "OPENAI_COMPLETIONS",
    "OPENAI_EMBEDDINGS", "OPENAI_RESPONSES", "RequestSpec", "RetrievedText", "TEXT_KEYS",
    "estimate_input_tokens",
]
