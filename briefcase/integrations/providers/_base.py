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

Provider fields added to the ``@capture`` record layout:

- ``model_parameters``: ``{"provider", "model_name", ...scalar request
  parameters}`` (the layout the framework handlers and ``OTelExporter``
  already read).
- ``token_usage``: ``{"prompt_tokens", "completion_tokens",
  "total_tokens"}`` plus provider-specific counters when reported.
- ``status``: ``"ok"``, ``"error"`` or ``"cancelled"``; ``error`` and
  ``error_type`` on failure, ``http_status`` when the SDK error carries one.
- ``finish_reason``, ``response_id``, ``request_id`` when the SDK exposes
  them.
- ``stream`` (bool); for streams ``outputs`` also carries ``chunk_count``
  and ``stream_completed``, and the record ``time_to_first_chunk_ms``.

Prompt and completion content is not recorded unless ``capture_content`` is
turned on.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import time
import weakref
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Tuple, Union

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.decorators import _build_record, _finalize_record
from briefcase.integrations.frameworks._capture_state import (
    CAPTURE_CONTENT_MODES,
    CaptureOptions,
)

logger = get_logger(__name__)

# Marker set on every wrapper this package installs, so a target is never
# wrapped twice.
WRAPPER_FLAG = "__briefcase_provider_wrapper__"

DEFAULT_MAX_CONTENT_CHARS = 8000
_MAX_DEPTH = 8
_MAX_ITEMS = 200

_MISSING = object()

OnItem = Callable[[Any], None]
OnEnd = Callable[[bool, Optional[BaseException]], None]


def normalize_capture_content(value: Union[bool, str, None]) -> str:
    """Map the provider-facing ``capture_content`` value onto the content
    modes the framework handlers use: ``False``/``None`` is ``"none"``,
    ``True`` is ``"full"``, and the mode strings pass through."""
    if value is None or value is False:
        return "none"
    if value is True:
        return "full"
    if value in CAPTURE_CONTENT_MODES:
        return str(value)
    raise ValueError(
        "capture_content must be a bool or one of "
        f"{CAPTURE_CONTENT_MODES}, got {value!r}"
    )


def jsonable(value: Any, limit: int = DEFAULT_MAX_CONTENT_CHARS, _depth: int = 0) -> Any:
    """Bounded, JSON-safe copy of request or response content.

    Strings are cut at ``limit`` characters, bytes are replaced by their
    length, pydantic models are dumped, and anything else is named by its
    type rather than repr'd. Iterators are never consumed. Never raises.
    """
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + "..."
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<{len(value)} bytes>"
        if _depth >= _MAX_DEPTH:
            return f"<{type(value).__name__}>"
        if isinstance(value, dict):
            return {
                str(k): jsonable(v, limit, _depth + 1)
                for k, v in list(value.items())[:_MAX_ITEMS]
            }
        if isinstance(value, (list, tuple)):
            return [jsonable(v, limit, _depth + 1) for v in value[:_MAX_ITEMS]]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return jsonable(dump(mode="json", exclude_none=True), limit, _depth + 1)
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, (str, int)):
            return enum_value
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
    return int(value)


def token_usage(
    prompt: Any = None,
    completion: Any = None,
    total: Any = None,
    **extras: Any,
) -> Optional[Dict[str, int]]:
    """Normalize provider token counters onto ``prompt_tokens`` /
    ``completion_tokens`` / ``total_tokens`` (the keys the framework
    handlers emit), keeping provider-specific counters in ``extras`` when
    they are numbers. ``None`` when the provider reported nothing."""
    prompt_i, completion_i, total_i = _int(prompt), _int(completion), _int(total)
    if prompt_i is None and completion_i is None and total_i is None:
        return None
    usage = {
        "prompt_tokens": prompt_i or 0,
        "completion_tokens": completion_i or 0,
    }
    usage["total_tokens"] = (
        total_i if total_i is not None else usage["prompt_tokens"] + usage["completion_tokens"]
    )
    for key, value in extras.items():
        extra = _int(value)
        if extra is not None:
            usage[key] = extra
    return usage


class ProviderCall:
    """One provider call in flight. ``finish`` builds the rest of the record
    and exports it exactly once; it never raises."""

    __slots__ = (
        "_inst", "_record", "_started_at", "_exporter", "_model", "_params",
        "_stream", "_done", "_t0", "_first_chunk_ms", "__weakref__",
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
    ) -> None:
        self._inst = inst
        self._record = record
        self._started_at = started_at
        self._exporter = exporter
        self._model = model
        self._params = params
        self._stream = stream
        self._done = False
        self._t0 = time.perf_counter()
        self._first_chunk_ms: Optional[float] = None

    @property
    def done(self) -> bool:
        return self._done

    def mark_stream(self) -> None:
        """Flag the call as streaming when that is only known from the
        result (not from the request)."""
        self._stream = True

    def first_chunk(self) -> None:
        if self._first_chunk_ms is None:
            self._first_chunk_ms = (time.perf_counter() - self._t0) * 1000

    def finish(
        self,
        info: Optional[Dict[str, Any]] = None,
        *,
        error: Optional[BaseException] = None,
        stream: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Complete and export the record.

        ``info`` is what the provider module extracted from the response:
        any of ``model``, ``response_id``, ``request_id``, ``finish_reason``,
        ``usage`` (a :func:`token_usage` dict), ``content`` (outputs, kept
        only when content capture is on) and ``extra`` (top-level fields).
        ``stream`` is ``{"chunk_count", "stream_completed"}`` for streams.
        """
        if self._done:
            return
        self._done = True
        try:
            self._finish(info or {}, error, stream)
        except Exception:
            logger.debug("provider record could not be completed", exc_info=True)

    def _finish(
        self,
        info: Dict[str, Any],
        error: Optional[BaseException],
        stream: Optional[Dict[str, Any]],
    ) -> None:
        inst = self._inst
        record = self._record
        options = inst.capture_options
        mode = options.capture_content

        if error is not None:
            _finalize_record(
                record, self._started_at, error=error,
                max_output_chars=inst.max_content_chars,
                capture_content=mode, redact=options.redact,
            )
            record.setdefault("status", "error")
            record["error_type"] = type(error).__name__
            http_status = _int(getattr(error, "status_code", None))
            if http_status is None:
                http_status = _int(
                    read(error, "response", "ResponseMetadata", "HTTPStatusCode")
                )
            if http_status is not None:
                record["http_status"] = http_status
            if "request_id" not in info:
                request_id = getattr(error, "request_id", None) or read(
                    error, "response", "ResponseMetadata", "RequestId"
                )
                if isinstance(request_id, str):
                    info = dict(info, request_id=request_id)
        else:
            _finalize_record(record, self._started_at, capture_content="none")
            record["status"] = "ok"

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
            parameters["model_name"] = str(model)
        if self._model and model and str(self._model) != str(model):
            parameters["requested_model"] = str(self._model)
        parameters.update(self._params)
        record["model_parameters"] = parameters

        usage = info.get("usage")
        if usage:
            record["token_usage"] = usage
        for key in ("finish_reason", "response_id", "request_id"):
            value = info.get(key)
            if value is not None:
                record[key] = str(value)
        record["stream"] = bool(self._stream)
        if self._first_chunk_ms is not None:
            record["time_to_first_chunk_ms"] = self._first_chunk_ms
        extra = info.get("extra")
        if isinstance(extra, dict):
            for key, value in extra.items():
                if value is not None:
                    record.setdefault(key, value)

        inst._trigger_export(record, exporter=self._exporter)

    def __del__(self) -> None:
        # Backstop for a stream that was dropped without being read or
        # closed: the record still goes out, marked incomplete.
        try:
            if not self._done and self._stream:
                self.finish(stream={"chunk_count": 0, "stream_completed": False})
        except Exception:
            pass


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
        max_content_chars: Bound on each captured string.
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
        self._undos: List[Tuple[Any, str, Any]] = []
        self.patched: List[str] = []
        self.skipped: List[str] = []

    @property
    def captures_content(self) -> bool:
        return self.capture_options.capture_content != "none"

    # -- patching ---------------------------------------------------------

    def patch(
        self,
        module_name: str,
        owner_name: Optional[str],
        attr: str,
        factory: Callable[[Any], Any],
    ) -> bool:
        """Replace ``module.owner.attr`` with ``factory(original)``.

        Returns False, after a WARNING, when the target does not exist in
        the installed SDK (its layout differs from the verified version) or
        when it is already wrapped. Never raises.
        """
        label = ".".join(p for p in (module_name, owner_name, attr) if p)
        try:
            owner: Any = importlib.import_module(module_name)
            if owner_name:
                owner = getattr(owner, owner_name)
            original = inspect.getattr_static(owner, attr)
        except Exception as exc:
            logger.warning(
                "briefcase.auto[%s]: patch target %s not found in the installed "
                "SDK (%s); skipping it. Calls through it are not recorded.",
                self.provider, label, type(exc).__name__,
            )
            self.skipped.append(label)
            return False
        if getattr(original, WRAPPER_FLAG, False):
            logger.warning(
                "briefcase.auto[%s]: %s is already instrumented; not wrapping it again.",
                self.provider, label,
            )
            self.skipped.append(label)
            return False
        if not callable(original):
            logger.warning(
                "briefcase.auto[%s]: patch target %s is not callable; skipping it.",
                self.provider, label,
            )
            self.skipped.append(label)
            return False
        try:
            wrapper = factory(original)
            setattr(wrapper, WRAPPER_FLAG, True)
            own = vars(owner).get(attr, _MISSING)
            setattr(owner, attr, wrapper)
        except Exception as exc:
            logger.warning(
                "briefcase.auto[%s]: could not patch %s (%s); skipping it.",
                self.provider, label, exc,
            )
            self.skipped.append(label)
            return False
        self._undos.append((owner, attr, own))
        self.patched.append(label)
        return True

    def uninstall(self) -> None:
        """Restore every patched attribute to the object it held before."""
        while self._undos:
            owner, attr, original = self._undos.pop()
            try:
                if original is _MISSING:
                    delattr(owner, attr)
                else:
                    setattr(owner, attr, original)
            except Exception as exc:
                logger.debug("provider undo: ignored error: %s", exc)
        self.patched.clear()

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
            exporter = self._resolve_exporter()
            if exporter is None:
                return None
            record = _build_record(
                decision_type, self.context_version, (), {}, 0, function_name, "none",
            )
            started_at = datetime.fromisoformat(record["started_at"])
            if started_at.tzinfo is None:  # pragma: no cover - always aware
                started_at = started_at.replace(tzinfo=timezone.utc)
            if inputs is not None and self.captures_content:
                try:
                    content = jsonable(inputs(), self.max_content_chars)
                    record["inputs"] = self.capture_options.content(content)
                except Exception:
                    logger.debug("provider inputs could not be captured", exc_info=True)
            return ProviderCall(
                self, record, started_at, exporter,
                str(model) if isinstance(model, str) and model else None,
                dict(params or {}), stream,
            )
        except Exception:
            logger.debug("provider capture could not start", exc_info=True)
            return None


# -- stream wrapping --------------------------------------------------------

def _guard(fn: Callable[..., None]) -> Callable[..., None]:
    """Make a capture callback unable to raise into the user's iteration."""
    def guarded(*args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            logger.debug("provider stream callback failed", exc_info=True)
    return guarded


def tee(inner: Iterator[Any], on_item: OnItem, on_end: OnEnd) -> Iterator[Any]:
    """Yield from ``inner``, reporting each item, then report the end once:
    ``on_end(True, None)`` on exhaustion, ``on_end(False, exc)`` on error,
    ``on_end(False, None)`` when the consumer stops early."""
    on_item, on_end = _guard(on_item), _guard(on_end)
    completed = False
    error: Optional[BaseException] = None
    try:
        for item in inner:
            on_item(item)
            yield item
        completed = True
    except GeneratorExit:
        raise
    except BaseException as exc:
        error = exc
        raise
    finally:
        on_end(completed, error)


async def atee(inner: AsyncIterator[Any], on_item: OnItem, on_end: OnEnd) -> AsyncIterator[Any]:
    """Async counterpart of :func:`tee`."""
    on_item, on_end = _guard(on_item), _guard(on_end)
    completed = False
    error: Optional[BaseException] = None
    try:
        async for item in inner:
            on_item(item)
            yield item
        completed = True
    except GeneratorExit:
        raise
    except BaseException as exc:
        error = exc
        raise
    finally:
        on_end(completed, error)


class StreamProxy:
    """Iterator wrapper used when a stream object cannot be instrumented in
    place. Everything except iteration and closing is delegated."""

    def __init__(self, inner: Any, on_item: OnItem, on_end: OnEnd) -> None:
        self._bc_inner = inner
        self._bc_on_end = _guard(on_end)
        self._bc_iter = tee(iter(inner), on_item, on_end)

    def __iter__(self) -> "StreamProxy":
        return self

    def __next__(self) -> Any:
        return next(self._bc_iter)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bc_inner, name)

    def __enter__(self) -> "StreamProxy":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

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
        self._bc_iter = atee(inner.__aiter__(), on_item, on_end)

    def __aiter__(self) -> "AsyncStreamProxy":
        return self

    async def __anext__(self) -> Any:
        return await self._bc_iter.__anext__()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bc_inner, name)

    async def __aenter__(self) -> "AsyncStreamProxy":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

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


def instrument_stream(stream: Any, on_item: OnItem, on_end: OnEnd, *, is_async: bool) -> Any:
    """Make ``stream`` report its items and its end.

    The Stainless-generated SDKs (openai, anthropic) return a ``Stream`` /
    ``AsyncStream`` object that iterates a private ``_iterator`` generator.
    When that attribute is present the generator is swapped for a reporting
    one and ``close`` is wrapped on the instance, so the caller gets back
    the *same* object (``isinstance`` checks, ``.response`` and context
    manager use keep working). Otherwise the stream is wrapped in a proxy.

    ``on_end`` may be called more than once (exhaustion, then ``close``);
    callers pass an idempotent function.
    """
    try:
        inner = stream._iterator
        wrapped = (atee if is_async else tee)(inner, on_item, on_end)
        stream._iterator = wrapped
        if stream._iterator is not wrapped:
            raise AttributeError("_iterator is not assignable")
    except Exception:
        proxy_cls = AsyncStreamProxy if is_async else StreamProxy
        return proxy_cls(stream, on_item, on_end)

    # A stream closed before it was exhausted never resumes the generator,
    # so close() reports the end itself. The closure holds the stream weakly:
    # a strong reference would form a cycle and delay the SDK's own cleanup
    # of a dropped stream until the garbage collector runs.
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
    modules subclass it and override :meth:`observe` and :meth:`info`."""

    def __init__(self, call: ProviderCall) -> None:
        self.call = call
        self.count = 0

    def observe(self, chunk: Any) -> None:  # pragma: no cover - overridden
        """Read usage / finish reason / content out of one chunk."""

    def info(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        return {}

    def on_item(self, chunk: Any) -> None:
        self.count += 1
        if self.count == 1:
            self.call.first_chunk()
        self.observe(chunk)

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
        )


RAW_RESPONSE_HEADER = "X-Stainless-Raw-Response"


def given(value: Any) -> bool:
    """False for None and for the Stainless SDKs' ``Omit`` / ``NotGiven``
    sentinels."""
    if value is None:
        return False
    return type(value).__name__ not in ("Omit", "NotGiven")


def unwrap_raw(result: Any, kwargs: dict) -> Tuple[Optional[str], Any]:
    """Handle the Stainless SDKs' raw-response wrappers (openai, anthropic).

    ``with_raw_response.<method>`` and ``with_streaming_response.<method>``
    call the patched method with an ``X-Stainless-Raw-Response`` header and
    get a response wrapper back instead of the parsed model. Returns
    ``(mode, parsed)``:

    - ``(None, result)`` for a plain call.
    - ``("stream", None)`` for ``with_streaming_response``: the body is
      unread and belongs to the caller, so it is not touched.
    - ``(mode, parsed)`` for ``with_raw_response``: ``parse()`` is cached by
      the wrapper, so the caller later receives this same object.
      ``parsed`` is None when it cannot be parsed synchronously.
    """
    headers = kwargs.get("extra_headers")
    mode = headers.get(RAW_RESPONSE_HEADER) if isinstance(headers, dict) else None
    if not isinstance(mode, str):
        return None, result
    if mode == "stream":
        return mode, None
    try:
        parsed = result.parse()
    except Exception:
        return mode, None
    if inspect.isawaitable(parsed):
        close = getattr(parsed, "close", None)
        if callable(close):
            close()
        return mode, None
    return mode, parsed


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


def sync_method(
    original: Callable[..., Any],
    begin: Callable[[tuple, dict], Optional[ProviderCall]],
    end: Callable[[ProviderCall, Any, tuple, dict], Any],
) -> Callable[..., Any]:
    """Wrap a synchronous SDK method. ``begin(args, kwargs)`` returns the
    call (or None to pass through); ``end(call, result, args, kwargs)``
    finishes it, or instruments a returned stream, and returns the result."""

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call = _safe_begin(begin, args, kwargs)
        if call is None:
            return original(*args, **kwargs)
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            call.finish(error=exc)
            raise
        return _safe_end(end, call, result, args, kwargs)

    return wrapper


def async_method(
    original: Callable[..., Any],
    begin: Callable[[tuple, dict], Optional[ProviderCall]],
    end: Callable[[ProviderCall, Any, tuple, dict], Any],
) -> Callable[..., Any]:
    """Async counterpart of :func:`sync_method`."""

    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        call = _safe_begin(begin, args, kwargs)
        if call is None:
            return await original(*args, **kwargs)
        try:
            result = await original(*args, **kwargs)
        except BaseException as exc:
            call.finish(error=exc)
            raise
        return _safe_end(end, call, result, args, kwargs)

    return wrapper


def _safe_begin(begin: Callable[[tuple, dict], Optional[ProviderCall]],
                args: tuple, kwargs: dict) -> Optional[ProviderCall]:
    try:
        return begin(args, kwargs)
    except Exception:
        logger.debug("provider capture could not start", exc_info=True)
        return None


def _safe_end(end: Callable[[ProviderCall, Any, tuple, dict], Any],
              call: ProviderCall, result: Any, args: tuple, kwargs: dict) -> Any:
    try:
        return end(call, result, args, kwargs)
    except Exception:
        logger.debug("provider capture could not finish", exc_info=True)
        call.finish()
        return result


__all__ = [
    "AsyncStreamProxy",
    "ProviderCall",
    "ProviderInstrumentor",
    "StreamProxy",
    "StreamState",
    "async_method",
    "atee",
    "given",
    "instrument_stream",
    "jsonable",
    "normalize_capture_content",
    "read",
    "scalar_params",
    "sync_method",
    "tee",
    "text_accumulator",
    "token_usage",
    "unwrap_raw",
]
