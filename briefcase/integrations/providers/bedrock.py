"""Auto-instrumentation for Amazon Bedrock Runtime through ``boto3`` /
``botocore``.

Verified against ``botocore==1.44.9`` (``boto3==1.44.9``) by inspecting the
installed package and its ``bedrock-runtime`` service model.

Patch target (checked at install time; skipped with a WARNING if missing):

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

What is read, per the 1.44.9 service model:

- ``Converse`` output: ``usage`` (``inputTokens``, ``outputTokens``,
  ``totalTokens``, ``cacheReadInputTokens``, ``cacheWriteInputTokens``),
  ``stopReason``, ``output.message.content[].text``.
- ``ConverseStream``: ``response["stream"]`` is an ``EventStream``; it is
  replaced by a proxy that iterates the same events. ``messageStop`` carries
  ``stopReason``, ``metadata`` carries ``usage``, and
  ``contentBlockDelta.delta.text`` the text.
- ``InvokeModel``: the body is a provider-specific ``StreamingBody`` that
  can be read only once, so it is never touched. Token counts come from the
  ``x-amzn-bedrock-input-token-count`` / ``x-amzn-bedrock-output-token-count``
  response headers. Completion content is therefore not captured for
  ``InvokeModel`` even with ``capture_content=True`` (the request body is).
- ``InvokeModelWithResponseStream``: chunks are opaque provider JSON bytes.
  The final chunk's ``amazon-bedrock-invocationMetrics`` object
  (``inputTokenCount`` / ``outputTokenCount``) is read when present. That
  field is not described by the botocore service model (the payload is
  bytes), so it is parsed defensively and could not be verified offline.
- The request id is ``ResponseMetadata.RequestId``.

Not covered: ``aiobotocore`` (its client overrides ``_make_api_call`` with a
coroutine of its own) and ``InvokeModelWithBidirectionalStream``.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, Dict, Optional

from briefcase._logging import get_logger
from briefcase.integrations.providers._base import (
    ProviderCall,
    ProviderInstrumentor,
    StreamProxy,
    StreamState,
    read,
    scalar_params,
    text_accumulator,
    token_usage,
)

logger = get_logger(__name__)

PROVIDER = "bedrock"
VERIFIED_VERSION = "1.44.9"
SERVICE_NAME = "bedrock-runtime"

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


def _usage(usage: Any) -> Optional[Dict[str, int]]:
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
    info: Dict[str, Any] = {
        "request_id": _request_id(response),
        "finish_reason": read(response, "stopReason"),
        "usage": _usage(read(response, "usage")),
    }
    if want_content:
        blocks = read(response, "output", "message", "content")
        texts = [
            block["text"] for block in (blocks if isinstance(blocks, list) else ())
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        info["content"] = {"content": "".join(texts)}
    return info


def _invoke_info(response: Any) -> Dict[str, Any]:
    headers = read(response, "ResponseMetadata", "HTTPHeaders") or {}
    return {
        "request_id": _request_id(response),
        "usage": token_usage(
            _header_int(headers, _INPUT_TOKEN_HEADER),
            _header_int(headers, _OUTPUT_TOKEN_HEADER),
        ),
    }


class _ConverseStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.want_content = inst.captures_content
        self.add_text, self.get_text = text_accumulator(inst.max_content_chars)
        self.data: Dict[str, Any] = {"request_id": request_id}

    def observe(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        if "messageStop" in event:
            self.data["finish_reason"] = read(event, "messageStop", "stopReason")
        elif "metadata" in event:
            usage = _usage(read(event, "metadata", "usage"))
            if usage:
                self.data["usage"] = usage
        elif "contentBlockDelta" in event and self.want_content:
            self.add_text(read(event, "contentBlockDelta", "delta", "text"))

    def info(self) -> Dict[str, Any]:
        info = dict(self.data)
        if self.want_content:
            info["content"] = {"content": self.get_text()}
        return info


class _InvokeStream(StreamState):
    def __init__(self, call: ProviderCall, inst: ProviderInstrumentor, request_id: Any) -> None:
        super().__init__(call)
        self.data: Dict[str, Any] = {"request_id": request_id}

    def observe(self, event: Any) -> None:
        payload = read(event, "chunk", "bytes")
        if not isinstance(payload, (bytes, bytearray)) or b"invocationMetrics" not in payload:
            return
        metrics = json.loads(payload).get("amazon-bedrock-invocationMetrics") or {}
        usage = token_usage(metrics.get("inputTokenCount"), metrics.get("outputTokenCount"))
        if usage:
            self.data["usage"] = usage

    def info(self) -> Dict[str, Any]:
        return dict(self.data)


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


def _end(inst: ProviderInstrumentor, call: ProviderCall, operation: str, response: Any) -> Any:
    if operation == "Converse":
        call.finish(_converse_info(response, inst.captures_content))
        return response
    if operation == "InvokeModel":
        call.finish(_invoke_info(response))
        return response
    key = "stream" if operation == "ConverseStream" else "body"
    events = response.get(key) if isinstance(response, dict) else None
    if events is None:
        call.finish({"request_id": _request_id(response)})
        return response
    state_cls = _ConverseStream if operation == "ConverseStream" else _InvokeStream
    state = state_cls(call, inst, _request_id(response))
    response[key] = StreamProxy(events, state.on_item, state.on_end)
    return response


def _is_bedrock_runtime(client: Any) -> bool:
    try:
        return client.meta.service_model.service_name == SERVICE_NAME
    except Exception:
        return False


def _factory(inst: ProviderInstrumentor) -> Callable[[Any], Any]:
    def factory(original: Any) -> Any:
        @functools.wraps(original)
        def wrapper(self: Any, operation_name: Any, api_params: Any = None,
                    *args: Any, **kwargs: Any) -> Any:
            if operation_name not in _OPERATIONS or not _is_bedrock_runtime(self):
                return original(self, operation_name, api_params, *args, **kwargs)
            try:
                call = _begin(inst, operation_name, api_params)
            except Exception:
                logger.debug("bedrock capture could not start", exc_info=True)
                call = None
            if call is None:
                return original(self, operation_name, api_params, *args, **kwargs)
            try:
                response = original(self, operation_name, api_params, *args, **kwargs)
            except BaseException as exc:
                call.finish(error=exc)
                raise
            try:
                return _end(inst, call, operation_name, response)
            except Exception:
                logger.debug("bedrock capture could not finish", exc_info=True)
                call.finish()
                return response

        return wrapper

    return factory


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
    inst.patch("botocore.client", "BaseClient", "_make_api_call", _factory(inst))
    return inst


__all__ = ["PROVIDER", "SERVICE_NAME", "VERIFIED_VERSION", "instrument"]
