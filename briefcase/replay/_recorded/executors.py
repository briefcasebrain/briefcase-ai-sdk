"""Executors: rebuild a recorded request and run it, or answer from a cassette.

Live executors call the user's installed provider SDK with the user's own
credentials (the SDK's usual environment variables). Request shapes were
read from the installed SDKs (see each class) rather than written from
memory.

Replays are never captured as new decisions: each executor calls the SDK
method *underneath* any ``briefcase.auto`` patch (the original kept in the
wrapper's ``__wrapped__``), and :func:`replaying` is true while a replay
runs, so other code can tell. Replays never run tools: a tool call in the
answer is returned as data and compared structurally.
"""

from __future__ import annotations

import contextvars
import importlib
import inspect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set, Tuple, Union

from briefcase.integrations.providers._base import WRAPPER_FLAG, jsonable, read
from briefcase.replay._recorded.records import (
    RecordedCall,
    fingerprint,
    iter_jsonl,
    normalize_tool_calls,
)

#: Header sent with every live replay request (OpenAI, Anthropic, Gemini),
#: so a replay can be told apart from production traffic downstream.
REPLAY_HEADER = "x-briefcase-replay"

_REPLAY_RUN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "briefcase_replay_run", default=None
)


def replaying() -> Optional[str]:
    """The replay run id while a replay call is executing, else ``None``."""
    return _REPLAY_RUN.get()


@dataclass
class Overrides:
    """What-if changes applied to every replayed request."""

    model: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    drop: Set[str] = field(default_factory=set)
    served_model: bool = False

    @property
    def empty(self) -> bool:
        return self.model is None and not self.params and not self.drop and not self.served_model


@dataclass
class Execution:
    """What a replay returned."""

    output: Dict[str, Any]
    usage: Optional[Dict[str, Any]] = None
    latency_ms: Optional[float] = None
    model: Optional[str] = None
    finish_reason: Optional[str] = None
    cost_usd: Optional[float] = None


class ExecutorError(Exception):
    """A replay call could not be made or failed; ``str()`` says what to do."""


class Executor(Protocol):
    """The small protocol every executor implements."""

    name: str
    #: True when ``execute`` calls a provider (spends the user's money).
    live: bool

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        """Reasons this executor cannot run ``call`` (empty when it can)."""

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        """Run the call; raise :class:`ExecutorError` on failure."""


def resolve_request(call: RecordedCall, overrides: Overrides) -> Tuple[Optional[str], Dict[str, Any]]:
    """The model and parameters a replay sends: every recorded parameter,
    minus ``drop``, plus ``params`` overrides."""
    model = call.model
    if overrides.served_model and call.served_model:
        model = call.served_model
    if overrides.model:
        model = overrides.model
    params = {k: v for k, v in call.params.items() if k not in overrides.drop}
    params.update(overrides.params)
    return model, params


def original(owner: Any, attr: str) -> Callable[..., Any]:
    """``owner.attr`` with every briefcase provider wrapper peeled off."""
    fn = inspect.getattr_static(owner, attr)
    while getattr(fn, WRAPPER_FLAG, False) and hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _accepted(fn: Callable[..., Any]) -> Set[str]:
    try:
        return set(inspect.signature(fn).parameters) - {"self"}
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return set()


def _unaccepted(params: Dict[str, Any], accepted: Set[str], sdk: str) -> List[str]:
    bad = sorted(k for k in params if k not in accepted)
    if not bad:
        return []
    return [
        f"recorded parameter(s) {', '.join(bad)} are not accepted by the installed {sdk}, so "
        "they cannot be pinned; drop them explicitly with --drop-param to replay anyway"
    ]


def _timed(run_id: str, fn: Callable[[], Any]) -> Tuple[Any, float]:
    token = _REPLAY_RUN.set(run_id)
    t0 = time.perf_counter()
    try:
        return fn(), (time.perf_counter() - t0) * 1000
    finally:
        _REPLAY_RUN.reset(token)


def _error(exc: BaseException) -> ExecutorError:
    status = getattr(exc, "status_code", None) or read(
        exc, "response", "ResponseMetadata", "HTTPStatusCode"
    )
    detail = f" (HTTP {status})" if status else ""
    return ExecutorError(f"{type(exc).__name__}{detail}: {str(exc)[:300]}")


def _output(text: Any, tool_calls: Any = None, refusal: Any = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "text": text if isinstance(text, str) else None,
        "tool_calls": normalize_tool_calls(jsonable(tool_calls) if tool_calls else []),
    }
    if isinstance(refusal, str) and refusal:
        out["refusal"] = refusal
    return out


# -- OpenAI -----------------------------------------------------------------

class OpenAIExecutor:
    """Replays ``openai`` chat completions, Responses and legacy completions.

    Shapes from ``openai==3.57.0``: ``Completions.create(messages, model,
    ..., seed, temperature, top_p, tools, response_format, extra_headers)``,
    ``Responses.create(input, instructions, model, ..., tools, text)``.
    Retries on 408/409/429/5xx are the SDK's own (``max_retries``).
    """

    name = "openai"
    live = True

    def __init__(self, client: Any = None, *, max_retries: int = 3, run_id: str = "replay") -> None:
        self._client = client
        self.max_retries = max_retries
        self.run_id = run_id

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import openai
            except ImportError as exc:
                raise ExecutorError(
                    "openai is not installed; install it with: pip install openai") from exc
            try:
                self._client = openai.OpenAI(max_retries=self.max_retries)
            except Exception as exc:
                raise ExecutorError(
                    f"could not create an OpenAI client ({exc}); set OPENAI_API_KEY") from exc
        return self._client

    def _target(self, surface: str) -> Tuple[Any, Callable[..., Any]]:
        from openai.resources.chat.completions import Completions as Chat
        from openai.resources.completions import Completions as Legacy
        from openai.resources.responses import Responses

        if surface == "openai.chat":
            return self.client.chat.completions, original(Chat, "create")
        if surface == "openai.responses":
            return self.client.responses, original(Responses, "create")
        return self.client.completions, original(Legacy, "create")

    def build(self, call: RecordedCall, overrides: Overrides) -> Dict[str, Any]:
        model, params = resolve_request(call, overrides)
        kwargs: Dict[str, Any] = {"model": model}
        kwargs.update(call.request)
        kwargs.update(params)
        return kwargs

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        try:
            from openai.resources.chat.completions import Completions as Chat
            from openai.resources.completions import Completions as Legacy
            from openai.resources.responses import Responses
            import openai
        except ImportError:
            return ["openai is not installed; install it with: pip install openai"]
        owner = {"openai.chat": Chat, "openai.responses": Responses}.get(call.surface or "", Legacy)
        _, params = resolve_request(call, overrides)
        return _unaccepted(params, _accepted(original(owner, "create")),
                           f"openai {getattr(openai, '__version__', '?')}")

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        from briefcase.integrations.providers import openai as prov

        resource, fn = self._target(call.surface or "")
        kwargs = self.build(call, overrides)
        kwargs["extra_headers"] = {REPLAY_HEADER: self.run_id}
        try:
            result, ms = _timed(self.run_id, lambda: fn(resource, **kwargs))
        except Exception as exc:
            raise _error(exc) from exc
        if call.surface == "openai.chat":
            info = prov._chat_info(result, False)
            message = read(prov._first_choice(result), "message")
            output = _output(read(message, "content"), read(message, "tool_calls"),
                             read(message, "refusal"))
        elif call.surface == "openai.responses":
            info = prov._responses_info(result, False)
            try:
                text = result.output_text
            except Exception:
                text = None
            calls = [item for item in (read(result, "output") or [])
                     if read(item, "type") == "function_call"]
            output = _output(text, calls)
        else:
            info = prov._completions_info(result, False)
            output = _output(read(prov._first_choice(result), "text"))
        return Execution(output, info.get("usage"), ms, info.get("model"), info.get("finish_reason"))


# -- Anthropic --------------------------------------------------------------

class AnthropicExecutor:
    """Replays ``anthropic`` Messages calls.

    Shape from ``anthropic==2.12.0``: ``Messages.create(max_tokens, messages,
    model, ..., output_config, stop_sequences, system, tools, extra_headers)``.
    That release no longer accepts ``temperature`` / ``top_p`` / ``top_k``; a
    record that carries them (from an older SDK) is reported as not
    replayable unless they are dropped with ``--drop-param``. The recorded
    ``effort`` is sent back as ``output_config={"effort": ...}``.
    """

    name = "anthropic"
    live = True

    def __init__(self, client: Any = None, *, max_retries: int = 3, run_id: str = "replay") -> None:
        self._client = client
        self.max_retries = max_retries
        self.run_id = run_id

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ExecutorError(
                    "anthropic is not installed; install it with: pip install anthropic") from exc
            try:
                self._client = anthropic.Anthropic(max_retries=self.max_retries)
            except Exception as exc:
                raise ExecutorError(
                    f"could not create an Anthropic client ({exc}); set ANTHROPIC_API_KEY") from exc
        return self._client

    @staticmethod
    def _beta(call: RecordedCall) -> bool:
        return ".beta." in str(call.record.get("function_name", ""))

    @staticmethod
    def _owner(beta: bool) -> Any:
        module = "anthropic.resources.beta.messages" if beta else "anthropic.resources.messages"
        return importlib.import_module(module).Messages

    def build(self, call: RecordedCall, overrides: Overrides) -> Dict[str, Any]:
        model, params = resolve_request(call, overrides)
        kwargs: Dict[str, Any] = {"model": model}
        kwargs.update(call.request)
        effort = params.pop("effort", None)
        if effort is not None:
            config = dict(params.pop("output_config", None) or {})
            config.setdefault("effort", effort)
            params["output_config"] = config
        kwargs.update(params)
        return kwargs

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        try:
            import anthropic
            owner = self._owner(self._beta(call))
        except ImportError:
            return ["anthropic is not installed; install it with: pip install anthropic"]
        params = {k: v for k, v in self.build(call, overrides).items()
                  if k not in ("model", *call.request)}
        return _unaccepted(params, _accepted(original(owner, "create")),
                           f"anthropic {getattr(anthropic, '__version__', '?')}")

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        from briefcase.integrations.providers import anthropic as prov

        beta = self._beta(call)
        resource = self.client.beta.messages if beta else self.client.messages
        fn = original(self._owner(beta), "create")
        kwargs = self.build(call, overrides)
        kwargs["extra_headers"] = {REPLAY_HEADER: self.run_id}
        try:
            result, ms = _timed(self.run_id, lambda: fn(resource, **kwargs))
        except Exception as exc:
            raise _error(exc) from exc
        info = prov._message_info(result, False)
        content = prov._content_blocks(jsonable(read(result, "content")))
        output = _output(content.get("content"), content.get("tool_calls"))
        return Execution(output, info.get("usage"), ms, info.get("model"), info.get("finish_reason"))


# -- Google GenAI -----------------------------------------------------------

class GoogleGenAIExecutor:
    """Replays ``google-genai`` ``models.generate_content`` calls.

    Shape from ``google-genai==3.6.1``: ``generate_content(model, contents,
    config=GenerateContentConfig(system_instruction, temperature, top_p,
    top_k, max_output_tokens, candidate_count, seed, ..., http_options))``.
    Retries use ``HttpOptions(retry_options=HttpRetryOptions(attempts=...))``
    (the SDK retries 408/429/5xx by default).
    """

    name = "google-genai"
    live = True

    def __init__(self, client: Any = None, *, max_retries: int = 3, run_id: str = "replay") -> None:
        self._client = client
        self.max_retries = max_retries
        self.run_id = run_id

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise ExecutorError(
                    "google-genai is not installed; install it with: pip install google-genai"
                ) from exc
            try:
                self._client = genai.Client(http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=self.max_retries + 1)))
            except Exception as exc:
                raise ExecutorError(
                    f"could not create a google-genai client ({exc}); set GOOGLE_API_KEY "
                    "(or GEMINI_API_KEY), or the Vertex AI environment variables") from exc
        return self._client

    def build(self, call: RecordedCall, overrides: Overrides) -> Dict[str, Any]:
        model, params = resolve_request(call, overrides)
        config = dict(params)
        if "system_instruction" in call.request:
            config["system_instruction"] = call.request["system_instruction"]
        return {"model": model, "contents": call.request.get("contents"), "config": config}

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        try:
            from google.genai import types
            from google import genai
        except ImportError:
            return ["google-genai is not installed; install it with: pip install google-genai"]
        _, params = resolve_request(call, overrides)
        return _unaccepted(params, set(types.GenerateContentConfig.model_fields),
                           f"google-genai {getattr(genai, '__version__', '?')}")

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        from google.genai import types
        from google.genai.models import Models

        from briefcase.integrations.providers import google_genai as prov

        kwargs = self.build(call, overrides)
        config = dict(kwargs.pop("config"))
        config["http_options"] = types.HttpOptions(headers={REPLAY_HEADER: self.run_id})
        fn = original(Models, "generate_content")
        resource = self.client.models
        try:
            result, ms = _timed(self.run_id, lambda: fn(
                resource, model=kwargs["model"], contents=kwargs["contents"],
                config=types.GenerateContentConfig(**config)))
        except Exception as exc:
            raise _error(exc) from exc
        info = prov._info(result, False)
        parts = read(prov._first_candidate(result), "content", "parts") or []
        calls = [read(part, "function_call") for part in parts if read(part, "function_call")]
        return Execution(_output(prov._text(result), calls), info.get("usage"), ms,
                         info.get("model"), info.get("finish_reason"))


# -- Bedrock ----------------------------------------------------------------

class BedrockExecutor:
    """Replays Amazon Bedrock ``Converse`` calls through ``boto3``.

    Shape from the ``botocore==1.44.9`` service model: ``Converse(modelId,
    messages, system, inferenceConfig{maxTokens, temperature, topP,
    stopSequences}, toolConfig, ...)``. Recorded parameters go into
    ``inferenceConfig``; a ``--param`` naming a top-level Converse member
    (e.g. ``toolConfig``) is sent top-level. Retries use botocore's
    ``Config(retries={"max_attempts": N, "mode": "standard"})``. No replay
    header is added (requests are SigV4-signed; that path was not verified).
    """

    name = "bedrock"
    live = True

    def __init__(self, client: Any = None, *, max_retries: int = 3, region: Optional[str] = None,
                 run_id: str = "replay") -> None:
        self._client = client
        self.max_retries = max_retries
        self.region = region
        self.run_id = run_id

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise ExecutorError("boto3 is not installed; install it with: pip install boto3") from exc
            try:
                self._client = boto3.client(
                    "bedrock-runtime", region_name=self.region,
                    config=Config(retries={"max_attempts": self.max_retries + 1, "mode": "standard"}),
                )
            except Exception as exc:
                raise ExecutorError(
                    f"could not create a bedrock-runtime client ({exc}); set AWS_REGION and AWS "
                    "credentials (or pass --region)") from exc
        return self._client

    @staticmethod
    def _members() -> Tuple[Set[str], Set[str]]:
        import botocore.session

        service = botocore.session.get_session().get_service_model("bedrock-runtime")
        shape = service.operation_model("Converse").input_shape
        return set(shape.members), set(shape.members["inferenceConfig"].members)

    def build(self, call: RecordedCall, overrides: Overrides,
              members: Optional[Tuple[Set[str], Set[str]]] = None) -> Dict[str, Any]:
        top, inference = members or self._members()
        model, params = resolve_request(call, overrides)
        api: Dict[str, Any] = {"modelId": model, "messages": call.request.get("messages")}
        if call.request.get("system"):
            api["system"] = call.request["system"]
        config: Dict[str, Any] = {}
        for key, value in params.items():
            if key in inference:
                config[key] = value
            elif key in top:
                api[key] = value
            else:
                config[key] = value  # reported by check()
        if config:
            api["inferenceConfig"] = config
        return api

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        try:
            import botocore
            top, inference = self._members()
        except ImportError:
            return ["boto3 is not installed; install it with: pip install boto3"]
        except Exception as exc:
            return [f"could not read the bedrock-runtime service model ({exc})"]
        _, params = resolve_request(call, overrides)
        return _unaccepted(params, top | inference, f"botocore {botocore.__version__}")

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        from botocore.client import BaseClient

        from briefcase.integrations.providers import bedrock as prov

        api = self.build(call, overrides)
        fn = original(BaseClient, "_make_api_call")
        client = self.client
        try:
            result, ms = _timed(self.run_id, lambda: fn(client, "Converse", api))
        except Exception as exc:
            raise _error(exc) from exc
        info = prov._converse_info(result, False)
        blocks = read(result, "output", "message", "content") or []
        texts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
        calls = [b for b in blocks if isinstance(b, dict) and isinstance(b.get("toolUse"), dict)]
        return Execution(_output("".join(texts) if texts else None, calls), info.get("usage"), ms,
                         call.model, info.get("finish_reason"))


# -- cassette ---------------------------------------------------------------

class CassetteExecutor:
    """Answers from recorded responses; never touches the network.

    Without ``path`` it returns each record's own recorded output. That
    checks the pipeline (replayability, report format) and is deterministic,
    but it is not evidence that a model still answers the same way; with
    overrides it refuses, since it holds no answer for a changed request.

    With ``path`` (a cassette written by ``replay run --save-cassette``) it
    looks the request up by its fingerprint (surface, model, parameters,
    request content) and returns the stored response, so a CI job can check
    that the requests an application builds still match the ones recorded.
    """

    name = "cassette"
    live = False

    def __init__(self, path: Union[str, Path, None] = None,
                 entries: Optional[Iterable[Dict[str, Any]]] = None) -> None:
        self.path = Path(path) if path else None
        self._entries: Optional[Dict[str, Dict[str, Any]]] = None
        if entries is not None:
            self._entries = {e["fingerprint"]: e for e in entries if "fingerprint" in e}
        elif self.path is not None:
            self._entries = {
                e["fingerprint"]: e for e in iter_jsonl(self.path) if "fingerprint" in e
            }

    def key(self, call: RecordedCall, overrides: Overrides) -> str:
        model, params = resolve_request(call, overrides)
        return fingerprint(call.surface, model, params, call.request)

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        if self._entries is None and not overrides.empty:
            return [
                "the built-in cassette only holds the recorded answer to the recorded request; "
                "--model/--param need a live executor (--executor auto) or a --cassette file "
                "with entries for the changed request"
            ]
        return []

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        if self._entries is None:
            if call.output is None:
                raise ExecutorError("the record holds no output to answer with")
            return Execution(dict(call.output), call.usage, call.latency_ms,
                             call.served_model or call.model, call.finish_reason, call.cost_usd)
        entry = self._entries.get(self.key(call, overrides))
        if entry is None:
            raise ExecutorError(
                "no cassette entry for this request (it differs from every recorded one); "
                "record one with: briefcase-ai replay run FILE --save-cassette CASSETTE --yes"
            )
        return Execution(dict(entry.get("output") or {}), entry.get("usage"),
                         entry.get("latency_ms"), entry.get("model"), entry.get("finish_reason"),
                         entry.get("cost_usd"))


def cassette_entry(call: RecordedCall, overrides: Overrides, execution: Execution) -> Dict[str, Any]:
    """The cassette line for one executed replay."""
    model, params = resolve_request(call, overrides)
    return {
        "fingerprint": fingerprint(call.surface, model, params, call.request),
        "surface": call.surface,
        "model": execution.model or model,
        "output": execution.output,
        "usage": execution.usage,
        "latency_ms": execution.latency_ms,
        "finish_reason": execution.finish_reason,
        "cost_usd": execution.cost_usd,
    }


class AutoExecutor:
    """Picks the live executor for each record's provider; built lazily, so
    only the SDKs the file needs are imported."""

    name = "auto"
    live = True

    _CLASSES = {
        "openai": OpenAIExecutor,
        "anthropic": AnthropicExecutor,
        "google-genai": GoogleGenAIExecutor,
        "bedrock": BedrockExecutor,
    }

    def __init__(self, *, max_retries: int = 3, run_id: str = "replay",
                 executors: Optional[Dict[str, Any]] = None) -> None:
        self.max_retries = max_retries
        self.run_id = run_id
        self._executors: Dict[str, Any] = dict(executors or {})

    def for_call(self, call: RecordedCall) -> Any:
        provider = call.provider or ""
        if provider not in self._executors:
            cls = self._CLASSES.get(provider)
            if cls is None:
                raise ExecutorError(f"no executor for provider {provider!r}")
            self._executors[provider] = cls(max_retries=self.max_retries, run_id=self.run_id)
        return self._executors[provider]

    def check(self, call: RecordedCall, overrides: Overrides) -> List[str]:
        try:
            return self.for_call(call).check(call, overrides)
        except ExecutorError as exc:
            return [str(exc)]

    def execute(self, call: RecordedCall, overrides: Overrides) -> Execution:
        return self.for_call(call).execute(call, overrides)


def dumps_line(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


__all__ = [
    "AnthropicExecutor",
    "AutoExecutor",
    "BedrockExecutor",
    "CassetteExecutor",
    "Execution",
    "Executor",
    "ExecutorError",
    "GoogleGenAIExecutor",
    "OpenAIExecutor",
    "Overrides",
    "REPLAY_HEADER",
    "cassette_entry",
    "original",
    "replaying",
    "resolve_request",
]
