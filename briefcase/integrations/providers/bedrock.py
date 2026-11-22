"""Auto-instrumentation for Amazon Bedrock Runtime through ``boto3`` /
``botocore`` (and, via :mod:`.aiobotocore`, ``aiobotocore``).

Tested with ``botocore==1.44.9`` (``boto3==1.44.9``): the tests drive a
real client through its HTTP layer with canned responses, including
event-stream frames decoded by botocore's own ``EventStream``.

Patch target (resolved and signature-checked at install time; left
untouched and reported if it differs):

- ``botocore.client.BaseClient._make_api_call(self, operation_name,
  api_params)``: the single method every generated client method calls.
  Calls are passed straight through unless the client's
  ``meta.service_model.service_name`` is ``"bedrock-runtime"`` and the
  operation is one of:

  ================================  ===============================
  operation                         decision_type
  ================================  ===============================
  ``Converse``                      ``llm.bedrock.converse``
  ``ConverseStream``                ``llm.bedrock.converse``
  ``InvokeModel``                   ``llm.bedrock.invoke_model``
  ``InvokeModelWithResponseStream`` ``llm.bedrock.invoke_model``
  ================================  ===============================

What is read, and how far each source could be verified:

- ``Converse`` output (botocore service model ``bedrock-runtime``
  2023-09-30, shape ``TokenUsage``): ``usage.inputTokens`` /
  ``outputTokens`` / ``totalTokens`` / ``cacheReadInputTokens`` /
  ``cacheWriteInputTokens``, ``stopReason``, ``metrics.latencyMs``,
  ``output.message.content[].text``.
- ``ConverseStream`` (shape ``ConverseStreamOutput``): ``messageStop``
  carries ``stopReason``, ``metadata`` (which comes *after* it) carries
  ``usage``, ``contentBlockDelta.delta.text`` the text.
  ``response["stream"]`` is replaced by a proxy over the same
  ``EventStream``; attribute access is delegated and ``isinstance(...,
  EventStream)`` still holds.
- ``InvokeModel``. The response shape (``InvokeModelResponse``) has no
  token fields: the model-specific body is the only documented source.
  Two sources are used, and ``usage_source`` on the record says which:

  * ``http_headers``: ``x-amzn-bedrock-input-token-count`` /
    ``x-amzn-bedrock-output-token-count``. These headers are returned by
    the service in practice (for example
    https://repost.aws/questions/QUQfxQ4JDdS06LUNJutd2rhw) but are in
    neither the service model nor the API reference, so they are read
    when present and never assumed: one missing header leaves that
    counter ``None``.
  * ``body``: the body is a ``StreamingBody`` that can be read once, and
    it is the application's. It is **never read here**. When the headers
    did not give both counters (or content capture is on), its ``read`` /
    ``readinto`` / ``close`` are wrapped on the instance so the bytes the
    *application* reads are also copied (up to 1 MiB) and parsed once it
    has read them all; the application receives identical bytes at
    identical times. The record is then emitted when the body has been
    read or closed (or dropped), not when ``invoke_model`` returns. Body
    shapes understood, each from its vendor documentation:
    Anthropic Messages (``usage.input_tokens`` / ``output_tokens``,
    ``stop_reason``; the ``anthropic`` SDK's ``Message`` model), Amazon
    Titan Text (``inputTextTokenCount``, ``results[].tokenCount``;
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-titan-text.html)
    and Meta Llama (``prompt_token_count``, ``generation_token_count``;
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-meta.html).
    Any other body shape: ``token_usage`` stays ``None`` (or what the
    headers gave), with a ``capture_limits`` note.

- ``InvokeModelWithResponseStream`` (shape ``PayloadPart``: opaque
  ``bytes``). Chunks are parsed as JSON only to look for, in order:

  * ``amazon-bedrock-invocationMetrics`` (``inputTokenCount`` /
    ``outputTokenCount``) on the last chunk. Bedrock adds this object in
    practice, but it is **not** in the botocore service model and no AWS
    documentation page describing it could be found when this was written
    (the model-parameter pages above were checked); it is read when
    present, ``usage_source="amazon-bedrock-invocationMetrics"``.
  * Anthropic Messages stream events (``message_start`` /
    ``message_delta`` usage), ``usage_source="body"``.
  * Amazon Titan Text chunks (``inputTextTokenCount`` /
    ``totalOutputTextTokenCount``, documented on the Titan page above).

  A stream of any other shape is recorded with ``token_usage: None``.

- The request id is ``ResponseMetadata.RequestId``.

Not covered: ``InvokeModelWithBidirectionalStream``, ``ApplyGuardrail``,
``CountTokens`` and the async-invoke operations.
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import Any, Callable, Dict, Optional, Tuple

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import (
    AsyncStreamProxy,
    ProviderCall,
    ProviderInstrumentor,
    StreamProxy,
    StreamState,
    _fail,
    read,
    scalar_params,
    text_accumulator,
    token_usage,
)

logger = get_logger(__name__)

PROVIDER = "bedrock"
TESTED_VERSIONS = ("1.44.9",)
VERIFIED_VERSION = TESTED_VERSIONS[-1]
SERVICE_NAME = "bedrock-runtime"
#: Bound on the copy kept of an InvokeModel body the application reads.
MAX_BODY_BYTES = 1024 * 1024

LIMIT_BODY_SHAPE = "InvokeModel body shape not recognised; token usage unknown"
LIMIT_BODY_UNREAD = (
    "InvokeModel body was not read to its end by the application (or exceeds 1 MiB); "
    "only header token counts are available"
)
LIMIT_STREAM_SHAPE = (
    "InvokeModelWithResponseStream chunks carried no recognised usage "
    "(no amazon-bedrock-invocationMetrics, Anthropic or Titan fields)"
)

_OPERATIONS = {
    "Converse": ("llm.bedrock.converse", "bedrock-runtime.converse", False),
    "ConverseStream": ("llm.bedrock.converse", "bedrock-runtime.converse_stream", True),
    "InvokeModel": ("llm.bedrock.invoke_model", "bedrock-runtime.invoke_model", False),
    "InvokeModelWithResponseStream": (
        "llm.bedrock.invoke_model",
        "bedrock-runtime.invoke_model_with_response_stream",
        True,
    ),
}
_INFERENCE_PARAMS = ("maxTokens", "temperature", "topP")
_INPUT_TOKEN_HEADER = "x-amzn-bedrock-input-token-count"
_OUTPUT_TOKEN_HEADER = "x-amzn-bedrock-output-token-count"
_USAGE_MARKERS = (b"invocationMetrics", b"usage", b"TokenCount", b"token_count")


def _usage(usage: Any) -> Optional[Dict[str, Optional[int]]]:
    return token_usage(
        read(usage, "inputTokens"),
        read(usage, "outputTokens"),
        read(usage, "totalTokens"),
        cache_read_input_tokens=read(usage, "cacheReadInputTokens"),
        cache_write_input_tokens=read(usage, "cacheWriteInputTokens"),
    )


def _header_int(headers: Any, name: str) -> Optional[int]:
    try:
        return int(headers[name])
    except Exception:
        return None


def _request_id(response: Any) -> Any:
    return read(response, "ResponseMetadata", "RequestId")


def _converse_info(response: Any, want_content: bool) -> Dict[str, Any]:
    blocks = read(response, "output", "message", "content")
    blocks = blocks if isinstance(blocks, list) else []
    tool_calls = sum(1 for block in blocks if isinstance(block, dict) and "toolUse" in block)
    info: Dict[str, Any] = {
        "request_id": _request_id(response),
        "finish_reason": read(response, "stopReason"),
        "usage": _usage(read(response, "usage")),
        "extra": {
            "provider_latency_ms": read(response, "metrics", "latencyMs"),
            "tool_call_count": tool_calls or None,
        },
    }
    if want_content:
        texts = [
            block["text"] for block in blocks
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        info["content"] = {"content": "".join(texts)}
    return info


def _header_usage(response: Any) -> Tuple[Optional[int], Optional[int]]:
    headers = read(response, "ResponseMetadata", "HTTPHeaders") or {}
    return _header_int(headers, _INPUT_TOKEN_HEADER), _header_int(headers, _OUTPUT_TOKEN_HEADER)


def body_usage(payload: Any) -> Optional[Dict[str, Any]]:
    """``{"usage", "finish_reason", "text"}`` out of a parsed InvokeModel
    response body, for the body shapes named in the module docstring; None
    for any other shape. Recognised by shape, not by model id (a model id
    may be an ARN or an inference profile)."""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if payload.get("type") == "message" and isinstance(usage, dict):  # Anthropic Messages
        content = payload.get("content")
        return {
            "usage": token_usage(
                usage.get("input_tokens"), usage.get("output_tokens"),
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
            ),
            "finish_reason": payload.get("stop_reason"),
            "text": "".join(
                block["text"] for block in (content if isinstance(content, list) else ())
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ),
        }
    results = payload.get("results")
    if "inputTextTokenCount" in payload and isinstance(results, list):  # Amazon Titan Text
        counts = [r.get("tokenCount") for r in results if isinstance(r, dict)]
        known = [c for c in counts if isinstance(c, int) and not isinstance(c, bool)]
        return {
            "usage": token_usage(
                payload.get("inputTextTokenCount"),
                sum(known) if known and len(known) == len(counts) else None,
            ),
            "finish_reason": read(results[0] if results else None, "completionReason"),
            "text": "".join(
                r["outputText"] for r in results
                if isinstance(r, dict) and isinstance(r.get("outputText"), str)
            ),
        }
    if "prompt_token_count" in payload or "generation_token_count" in payload:  # Meta Llama
        generation = payload.get("generation")
        return {
            "usage": token_usage(
                payload.get("prompt_token_count"), payload.get("generation_token_count")),
            "finish_reason": payload.get("stop_reason"),
            "text": generation if isinstance(generation, str) else "",
        }
    return None


class _InvokeBody:
    """Finishes an ``InvokeModel`` record from the bytes the *application*
    reads out of the response body. Nothing is read on its behalf."""

    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, response: Any,
                 header_usage: Tuple[Optional[int], Optional[int]]) -> None:
        self.call = call
        self.inst = inst
        self.request_id = _request_id(response)
        self.header_usage = header_usage
        self.buffer: Optional[bytearray] = bytearray()
        self.complete = False
        call.on_abandon(self.finish)

    def feed(self, data: Any, everything: bool) -> None:
        if self.call.done:
            return
        if isinstance(data, (bytes, bytearray, memoryview)) and len(data):
            if self.buffer is not None:
                if len(self.buffer) + len(data) > MAX_BODY_BYTES:
                    self.buffer = None  # too large to keep a copy of
                else:
                    self.buffer += data
        else:
            everything = True  # b"" (or 0 bytes read into a buffer) is EOF
        if everything:
            self.complete = True
            self.finish()

    def finish(self) -> None:
        if self.call.done:
            return
        prompt, completion = self.header_usage
        info: Dict[str, Any] = {"request_id": self.request_id}
        source = "http_headers" if prompt is not None or completion is not None else None
        parsed = None
        if self.complete and self.buffer is not None:
            try:
                parsed = body_usage(json.loads(bytes(self.buffer)))
            except Exception:
                parsed = None
        limits = []
        if parsed is not None:
            usage = parsed.get("usage") or {}
            if prompt is None and completion is None and usage:
                source = "body"
            elif usage and (prompt is None or completion is None):
                source = "http_headers+body"
            prompt = prompt if prompt is not None else usage.get("prompt_tokens")
            completion = completion if completion is not None else usage.get("completion_tokens")
            extras = {k: v for k, v in usage.items()
                      if k not in ("prompt_tokens", "completion_tokens", "total_tokens")}
            info["usage"] = token_usage(prompt, completion, **extras)
            info["finish_reason"] = parsed.get("finish_reason")
            if self.inst.captures_content:
                info["content"] = {"content": parsed.get("text")}
        else:
            info["usage"] = token_usage(prompt, completion)
            if prompt is None or completion is None or self.inst.captures_content:
                limits.append(LIMIT_BODY_SHAPE if self.complete and self.buffer is not None
                              else LIMIT_BODY_UNREAD)
        if source:
            info["extra"] = {"usage_source": source}
        if limits:
            info["limits"] = limits
        self.buffer = None
        self.call.finish(info)

    # -- instance-level wrappers ------------------------------------------

    def hook(self, body: Any) -> bool:
        """Wrap ``read`` / ``readinto`` / ``close`` on the body instance.
        False when the body is not shaped as expected (nothing is changed
        then)."""
        try:
            original_read = body.read
            original_close = body.close
            original_readinto = getattr(body, "readinto", None)
            feed, finish = self._guarded_feed, self._guarded_finish

            if inspect.iscoroutinefunction(original_read):
                @functools.wraps(original_read)
                async def aread(amt: Any = None, *a: Any, **k: Any) -> Any:
                    data = await original_read(amt, *a, **k)
                    feed(data, amt is None or (isinstance(amt, int) and amt < 0))
                    return data

                body.read = aread
            else:
                @functools.wraps(original_read)
                def read_(amt: Any = None, *a: Any, **k: Any) -> Any:
                    data = original_read(amt, *a, **k)
                    feed(data, amt is None or (isinstance(amt, int) and amt < 0))
                    return data

                body.read = read_

            if callable(original_readinto) and not inspect.iscoroutinefunction(original_readinto):
                @functools.wraps(original_readinto)
                def readinto(buffer: Any, *a: Any, **k: Any) -> Any:
                    count = original_readinto(buffer, *a, **k)
                    try:
                        data = bytes(memoryview(buffer)[:count]) if count else b""
                    except Exception:
                        data = None
                        self.buffer = None
                    if data is not None:
                        feed(data, False)
                    return count

                body.readinto = readinto

            @functools.wraps(original_close)
            def close(*a: Any, **k: Any) -> Any:
                try:
                    return original_close(*a, **k)
                finally:
                    finish()

            body.close = close
            return True
        except Exception:
            logger.debug("bedrock InvokeModel body could not be observed", exc_info=True)
            return False

    def _guarded_feed(self, data: Any, everything: bool) -> None:
        try:
            self.feed(data, everything)
        except Exception:
            logger.debug("bedrock body capture failed", exc_info=True)
            self.buffer = None

    def _guarded_finish(self) -> None:
        try:
            self.finish()
        except Exception:
            logger.debug("bedrock body capture failed", exc_info=True)


class _ConverseStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = inst.captures_content
        self.add_text, self.get_text = text_accumulator(inst.max_content_chars)
        self.data: Dict[str, Any] = {"request_id": request_id}

    def observe(self, event: Any) -> Any:
        if not isinstance(event, dict):
            return None
        if "messageStop" in event:
            self.data["finish_reason"] = read(event, "messageStop", "stopReason")
            self.finished = True
        elif "metadata" in event:
            usage = _usage(read(event, "metadata", "usage"))
            if usage:
                self.data["usage"] = usage
            latency = read(event, "metadata", "metrics", "latencyMs")
            if latency is not None:
                self.data["extra"] = {"provider_latency_ms": latency}
        elif "contentBlockDelta" in event and self.want_content:
            self.add_text(read(event, "contentBlockDelta", "delta", "text"))
        return None

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


class _InvokeStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = inst.captures_content
        self.add_text, self.get_text = text_accumulator(inst.max_content_chars)
        self.data: Dict[str, Any] = {"request_id": request_id}
        self.prompt: Any = None
        self.completion: Any = None
        self.cache: Dict[str, Any] = {}
        self.source: Optional[str] = None

    def observe(self, event: Any) -> Any:
        payload = read(event, "chunk", "bytes")
        if not isinstance(payload, (bytes, bytearray)):
            return None
        if not self.want_content and not any(marker in payload for marker in _USAGE_MARKERS):
            return None
        try:
            chunk = json.loads(payload)
        except Exception:
            return None
        if not isinstance(chunk, dict):
            return None
        metrics = chunk.get("amazon-bedrock-invocationMetrics")
        kind = chunk.get("type")
        if kind == "message_start":  # Anthropic Messages events
            usage = read(chunk, "message", "usage") or {}
            if self.source is None:
                self.prompt = usage.get("input_tokens")
                self.source = "body"
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                if usage.get(key) is not None:
                    self.cache[key] = usage[key]
        elif kind == "message_delta":
            if self.source in (None, "body"):
                self.completion = read(chunk, "usage", "output_tokens")
                self.source = "body"
            stop = read(chunk, "delta", "stop_reason")
            if stop:
                self.data["finish_reason"] = stop
        elif kind == "message_stop":
            self.finished = True
        elif kind == "content_block_delta":
            if self.want_content and read(chunk, "delta", "type") == "text_delta":
                self.add_text(read(chunk, "delta", "text"))
        elif "outputText" in chunk or "totalOutputTextTokenCount" in chunk:  # Titan Text
            if self.source in (None, "body"):
                if chunk.get("inputTextTokenCount") is not None:
                    self.prompt = chunk["inputTextTokenCount"]
                    self.source = "body"
                if chunk.get("totalOutputTextTokenCount") is not None:
                    self.completion = chunk["totalOutputTextTokenCount"]
                    self.source = "body"
            if chunk.get("completionReason"):
                self.data["finish_reason"] = chunk["completionReason"]
                self.finished = True
            if self.want_content:
                self.add_text(chunk.get("outputText"))
        if isinstance(metrics, dict):
            # Bedrock's own metering wins over the model's counters.
            if metrics.get("inputTokenCount") is not None or \
                    metrics.get("outputTokenCount") is not None:
                self.prompt = metrics.get("inputTokenCount")
                self.completion = metrics.get("outputTokenCount")
                self.source = "amazon-bedrock-invocationMetrics"
            self.finished = True
        return None

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        usage = token_usage(self.prompt, self.completion, **self.cache)
        if usage:
            info["usage"] = usage
            info["extra"] = {"usage_source": self.source}
        elif self.count:
            info["limits"] = [LIMIT_STREAM_SHAPE]
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


def _begin(inst: ProviderInstrumentor, operation: str,
           api_params: Any) -> Optional[ProviderCall]:
    decision_type, function_name, stream = _OPERATIONS[operation]
    params = api_params if isinstance(api_params, dict) else {}

    def inputs() -> Dict[str, Any]:
        if operation.startswith("Converse"):
            return {k: params[k] for k in ("system", "messages") if k in params}
        body = params.get("body")
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        return {"body": body}

    return inst.begin(
        decision_type, function_name,
        model=params.get("modelId"),
        params=scalar_params(params.get("inferenceConfig") or {}, _INFERENCE_PARAMS),
        inputs=inputs,
        stream=stream,
    )


def _end(inst: ProviderInstrumentor, call: ProviderCall, operation: str, response: Any,
         is_async: bool = False) -> Any:
    if operation == "Converse":
        call.finish(_converse_info(response, inst.captures_content))
        return response
    if operation == "InvokeModel":
        prompt, completion = _header_usage(response)
        body = response.get("body") if isinstance(response, dict) else None
        observer = _InvokeBody(call, inst, response, (prompt, completion))
        need_body = prompt is None or completion is None or inst.captures_content
        if not need_body or body is None or not observer.hook(body):
            observer.finish()
        return response
    key = "stream" if operation == "ConverseStream" else "body"
    events = response.get(key) if isinstance(response, dict) else None
    if events is None:
        call.finish({"request_id": _request_id(response)})
        return response
    state_cls = _ConverseStream if operation == "ConverseStream" else _InvokeStream
    state = state_cls(call, inst, _request_id(response))
    proxy_cls = AsyncStreamProxy if is_async else StreamProxy
    response[key] = proxy_cls(events, state.on_item, state.on_end)
    return response


def _is_bedrock_runtime(client: Any) -> bool:
    try:
        return client.meta.service_model.service_name == SERVICE_NAME
    except Exception:
        return False


def _start(inst: ProviderInstrumentor, client: Any, operation_name: Any,
           api_params: Any) -> Optional[ProviderCall]:
    try:
        if (not inst.active or not isinstance(operation_name, str)
                or operation_name not in _OPERATIONS or not _is_bedrock_runtime(client)):
            return None
        return _begin(inst, operation_name, api_params)
    except Exception:
        logger.debug("bedrock capture could not start", exc_info=True)
        return None


def _finish(inst: ProviderInstrumentor, call: ProviderCall, operation_name: str,
            response: Any, is_async: bool) -> Any:
    try:
        return _end(inst, call, operation_name, response, is_async)
    except Exception:
        logger.debug("bedrock capture could not finish", exc_info=True)
        call.finish({"limits": ["capture failed while reading the response"]})
        return response


def _factory(inst: ProviderInstrumentor) -> Callable[[Any], Any]:
    def factory(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(self: Any, operation_name: Any, api_params: Any = None,
                    *args: Any, **kwargs: Any) -> Any:
            call = _start(inst, self, operation_name, api_params)
            if call is None:
                return original(self, operation_name, api_params, *args, **kwargs)
            try:
                response = original(self, operation_name, api_params, *args, **kwargs)
            except BaseException as exc:
                _fail(call, exc)
                raise
            return _finish(inst, call, operation_name, response, False)

        return wrapper

    return factory


def _async_factory(inst: ProviderInstrumentor) -> Callable[[Any], Any]:
    """For ``aiobotocore``, whose client overrides ``_make_api_call`` with a
    coroutine function of the same signature."""
    def factory(original: Any) -> Any:
        @functools.wraps(original)
        async def wrapper(self: Any, operation_name: Any, api_params: Any = None,
                          *args: Any, **kwargs: Any) -> Any:
            call = _start(inst, self, operation_name, api_params)
            if call is None:
                return await original(self, operation_name, api_params, *args, **kwargs)
            try:
                response = await original(self, operation_name, api_params, *args, **kwargs)
            except BaseException as exc:
                _fail(call, exc)
                raise
            return _finish(inst, call, operation_name, response, True)

        return wrapper

    return factory


LIMITS = [
    "InvokeModel: token counts from the x-amzn-bedrock-*-token-count headers when present, "
    "else from the body once the application has read it (Anthropic, Titan Text, Llama "
    "shapes only)",
    "InvokeModelWithResponseStream: usage from amazon-bedrock-invocationMetrics, Anthropic "
    "or Titan chunk fields; other model families are recorded with token_usage None",
    "InvokeModelWithBidirectionalStream is not recorded",
]


def instrument(**options: Any) -> ProviderInstrumentor:
    """Patch botocore for Bedrock Runtime calls. Returns the instrumentor;
    call its ``uninstall()`` (or ``briefcase.auto.undo("bedrock")``) to
    restore.

    Args:
        **options: See :class:`ProviderInstrumentor`.

    Raises:
        ImportError: ``botocore`` is not installed.
    """
    try:
        import botocore as sdk
    except ImportError as exc:
        raise ImportError(
            "briefcase.auto: botocore is not installed, so nothing was "
            "instrumented. Install with: pip install boto3"
        ) from exc

    inst = ProviderInstrumentor(PROVIDER, **options)
    inst.sdk_version = getattr(sdk, "__version__", None)
    inst.tested_versions = TESTED_VERSIONS
    inst.limits = list(LIMITS) + [
        "aiobotocore clients are recorded by the separate 'aiobotocore' integration",
    ]
    inst.patch("botocore.client", "BaseClient", "_make_api_call", _factory(inst),
               ("operation_name", "api_params"))
    inst.report_skipped()
    return inst


__all__ = [
    "PROVIDER", "SERVICE_NAME", "TESTED_VERSIONS", "VERIFIED_VERSION", "body_usage", "instrument",
]
