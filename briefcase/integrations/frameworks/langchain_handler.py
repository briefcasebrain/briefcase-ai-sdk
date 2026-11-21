"""LangChain callback handler for automatic AI decision capture.

Intercepts LLM calls, chain executions, tool invocations, and retriever
operations and records each as a Briefcase decision record. Every root run
(a top-level chain, or a standalone LLM, tool or retriever call) is exported
through the configured exporter when it ends or errors, with its nested runs
attached as child spans.

Usage:
    from briefcase.integrations.frameworks import BriefcaseLangChainHandler

    handler = BriefcaseLangChainHandler(context_version="v2")

    # Pass as a callback to any LangChain component
    llm = ChatOpenAI(callbacks=[handler])
    chain.invoke({"input": "..."}, config={"callbacks": [handler]})

    decisions = handler.get_decisions()
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.integrations.frameworks._capture_state import (
    DEFAULT_INFLIGHT_TTL_SECONDS,
    DEFAULT_MAX_INFLIGHT,
    DEFAULT_MAX_RECORDS,
    CaptureOptions,
    InflightStore,
    bounded_records,
)

logger = get_logger(__name__)

_INSTALL_HINT = (
    "langchain-core is required for BriefcaseLangChainHandler. "
    "Install with: pip install briefcase-ai[langchain] or pip install langchain-core"
)

try:
    import langchain_core  # noqa: F401
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False

try:
    from opentelemetry import trace
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False


@dataclass
class CapturedDecision:
    """A single AI decision captured from a framework callback event."""
    decision_id: str
    decision_type: str  # "llm", "chain", "tool", "retriever", ...
    function_name: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    model_parameters: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    execution_time_ms: Optional[float] = None
    parent_run_id: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    token_usage: Optional[Dict[str, int]] = None
    context_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the decision-record wire shape plus framework extras."""
        result: Dict[str, Any] = {
            "decision_id": self.decision_id,
            "decision_type": self.decision_type,
            "function_name": self.function_name,
            "inputs": self.inputs,
            "outputs": self.outputs,
        }
        if self.started_at:
            result["started_at"] = self.started_at.isoformat()
        if self.ended_at:
            result["ended_at"] = self.ended_at.isoformat()
        if self.execution_time_ms is not None:
            result["execution_time_ms"] = self.execution_time_ms
        if self.error:
            result["error"] = self.error
        if self.context_version is not None:
            result["context_version"] = self.context_version
        if self.model_parameters:
            result["model_parameters"] = self.model_parameters
        if self.tags:
            result["tags"] = self.tags
        if self.parent_run_id:
            result["parent_run_id"] = self.parent_run_id
        if self.token_usage:
            result["token_usage"] = self.token_usage
        return result


class BriefcaseLangChainHandler(ExportMixin):
    """LangChain callback handler that captures AI decisions for Briefcase.

    Works as a drop-in callback for any LangChain component. Captures:
    - LLM calls (model, prompts, completions, token usage, streamed tokens)
    - Chain executions (inputs, outputs, chain type)
    - Tool invocations (tool name, input, output)
    - Retriever queries (query, retrieved documents)

    Export: every *root* run is exported once, when it ends or errors, with
    all of its nested runs attached as ``child_spans``. A root is a run with
    no ``parent_run_id``, or one whose parent this handler is not tracking
    (for example an LLM call under a chain when ``capture_chains=False``),
    so a standalone LLM, tool or retriever call is exported too.

    Memory: completed decisions are kept in a ring buffer of ``max_records``.
    A run whose end callback never arrives is dropped from the in-flight
    table after ``inflight_ttl_seconds`` (or when ``max_inflight`` runs are
    open) and recorded with an ``abandoned`` error.

    Implements the LangChain BaseCallbackHandler interface via duck typing
    rather than inheritance, so langchain-core is needed only to have
    something to observe, never to import this module.
    """

    def __init__(
        self,
        capture_llm: bool = True,
        capture_chains: bool = True,
        capture_tools: bool = True,
        capture_retrievers: bool = True,
        max_input_chars: int = 10000,
        max_output_chars: int = 10000,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        exporter: Any = None,
        capture_content: str = "full",
        redact: Optional[Callable[[str], str]] = None,
        max_records: Optional[int] = DEFAULT_MAX_RECORDS,
        max_inflight: Optional[int] = DEFAULT_MAX_INFLIGHT,
        inflight_ttl_seconds: Optional[float] = DEFAULT_INFLIGHT_TTL_SECONDS,
    ):
        """
        Args:
            capture_llm / capture_chains / capture_tools / capture_retrievers:
                Which run types to capture.
            max_input_chars / max_output_chars: Bound on each captured string.
            context_version: Optional version tag added to all decisions.
            async_capture: If True (default), export runs in the background.
            exporter: Briefcase exporter; falls back to the global config.
            capture_content: ``"full"`` (default), ``"hash"`` or ``"none"``,
                as for ``@briefcase.capture``.
            redact: Optional ``str -> str`` hook applied to captured content
                in ``"full"`` mode. Fails closed.
            max_records: Completed decisions kept for ``get_decisions()``
                (``None`` = unbounded).
            max_inflight: Open runs tracked at once (``None`` = unbounded).
            inflight_ttl_seconds: Age after which an open run is treated as
                abandoned (``None`` = never).
        """
        if not _LANGCHAIN_AVAILABLE:
            raise ImportError(_INSTALL_HINT)

        self.capture_llm = capture_llm
        self.capture_chains = capture_chains
        self.capture_tools = capture_tools
        self.capture_retrievers = capture_retrievers
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self.context_version = context_version
        self.async_capture = async_capture
        self._exporter = exporter
        self._capture_options = CaptureOptions(capture_content, redact)
        self.max_records = max_records

        self._lock = threading.RLock()

        # Completed decisions (ring buffer)
        self._decisions: Deque[CapturedDecision] = bounded_records(max_records)

        # In-flight tracking (run_id to partial decision)
        self._inflight = InflightStore(
            max_size=max_inflight,
            ttl_seconds=inflight_ttl_seconds,
            on_evict=self._on_inflight_evicted,
        )
        # run_id -> run_id of the root it will be exported with
        self._root_of: Dict[str, str] = {}
        # root run_id -> completed descendants awaiting the root's export
        self._children: Dict[str, Deque[CapturedDecision]] = {}
        # run_id -> streamed tokens seen so far
        self._stream_buffers: Dict[str, List[str]] = {}

        # LangChain BaseCallbackHandler compatibility flags
        self.raise_error = False
        self.run_inline = True
        self.ignore_llm = not capture_llm
        self.ignore_chain = not capture_chains
        self.ignore_agent = False
        self.ignore_retriever = not capture_retrievers

    # Public API

    def get_decisions(self) -> List[CapturedDecision]:
        """Return the captured decisions (the most recent ``max_records``)."""
        with self._lock:
            return list(self._decisions)

    def get_decisions_as_dicts(self) -> List[Dict[str, Any]]:
        """Return the captured decisions as serializable dicts."""
        return [d.to_dict() for d in self.get_decisions()]

    def clear(self) -> None:
        """Clear all captured decisions and in-flight state."""
        with self._lock:
            self._decisions.clear()
            self._inflight.clear()
            self._root_of.clear()
            self._children.clear()
            self._stream_buffers.clear()

    @property
    def decision_count(self) -> int:
        """Number of completed decisions held."""
        return len(self._decisions)

    # Run lifecycle

    def _start_run(
        self,
        decision_type: str,
        function_name: str,
        inputs: Dict[str, Any],
        run_id: Any,
        parent_run_id: Any,
        tags: Optional[List[str]],
        metadata: Optional[Dict[str, Any]],
        model_parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        run_key = str(run_id) if run_id else str(uuid.uuid4())
        parent_key = str(parent_run_id) if parent_run_id else None
        decision = CapturedDecision(
            decision_id=run_key,
            decision_type=decision_type,
            function_name=function_name,
            inputs=inputs,
            model_parameters=model_parameters or {},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_key,
            tags=_merge_tags(tags, metadata),
            context_version=self.context_version,
        )
        with self._lock:
            # A run is exported with its root. It is its own root when it has
            # no parent, or when the parent is not a run this handler tracks.
            if parent_key is not None and parent_key in self._root_of:
                self._root_of[run_key] = self._root_of[parent_key]
            else:
                self._root_of[run_key] = run_key
            self._inflight[run_key] = decision

    def _finish_run(
        self,
        run_id: Any,
        *,
        outputs: Optional[Dict[str, Any]] = None,
        error: Optional[BaseException] = None,
        token_usage: Optional[Dict[str, int]] = None,
    ) -> None:
        run_key = str(run_id) if run_id else None
        if run_key is None:
            return
        with self._lock:
            decision = self._inflight.pop(run_key, None)
            if decision is None:
                return
            if outputs is not None:
                decision.outputs = outputs
            if error is not None:
                decision.error = str(error) or type(error).__name__
            if token_usage:
                decision.token_usage = token_usage
            record = self._complete(run_key, decision)
        if record is not None:
            self._trigger_export(record)

    def _complete(
        self, run_key: str, decision: CapturedDecision
    ) -> Optional[Dict[str, Any]]:
        """Finalize a decision. Returns the export record when it is a root.

        Caller holds ``self._lock``.
        """
        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        self._stream_buffers.pop(run_key, None)
        options = self._capture_options
        decision.inputs = options.content(decision.inputs)
        decision.outputs = options.content(decision.outputs)
        decision.error = options.error(decision.error)
        self._decisions.append(decision)

        root_key = self._root_of.pop(run_key, run_key)
        if root_key != run_key and root_key in self._inflight:
            # Nested run: held until its root finishes.
            children = self._children.get(root_key)
            if children is None:
                children = self._children[root_key] = bounded_records(self.max_records)
            children.append(decision)
            return None

        # Root run (or a nested run whose root is gone): export it now.
        record = decision.to_dict()
        record["child_spans"] = [
            child.to_dict() for child in self._children.pop(run_key, ())
        ]
        return record

    def _on_inflight_evicted(self, run_key: Any, decision: Any, reason: str) -> None:
        """Record a run whose end callback never arrived as abandoned."""
        with self._lock:
            decision.error = f"abandoned: no end callback ({reason})"
            record = self._complete(str(run_key), decision)
        if record is not None:
            self._trigger_export(record)

    # LLM callbacks

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM starts generating."""
        if not self.capture_llm:
            return

        try:
            serialized = serialized or {}
            model_name = _extract_model_name(serialized, kwargs)
            self._start_run(
                "llm",
                model_name,
                {"prompts": [str(p)[:self.max_input_chars] for p in prompts]},
                run_id, parent_run_id, tags, metadata,
                model_parameters=_extract_model_params(serialized, kwargs),
            )

            if HAS_OTEL:
                _emit_otel_event("llm_start", {
                    "model": model_name,
                    "prompt_count": len(prompts),
                })
        except Exception as exc:
            logger.debug("on_llm_start capture failed: %s", exc)

    def on_llm_new_token(
        self,
        token: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Streaming token: accumulated as the output when no final text arrives."""
        if not self.capture_llm or not run_id or not token:
            return
        try:
            run_key = str(run_id)
            with self._lock:
                if run_key not in self._inflight:
                    return
                buffer = self._stream_buffers.setdefault(run_key, [])
                held = sum(len(t) for t in buffer)
                room = self.max_output_chars - held
                if room > 0:
                    buffer.append(str(token)[:room])
        except Exception as exc:
            logger.debug("on_llm_new_token capture failed: %s", exc)

    def _streamed_text(self, run_id: Any) -> str:
        with self._lock:
            return "".join(self._stream_buffers.get(str(run_id), ()))

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM finishes generating."""
        try:
            output_text, token_usage = _extract_llm_output(response)
            outputs: Dict[str, Any] = {}
            if not output_text:
                streamed = self._streamed_text(run_id)
                if streamed:
                    output_text = streamed
                    outputs["streamed"] = True
            outputs["text"] = (
                str(output_text)[:self.max_output_chars] if output_text else None
            )
            self._finish_run(run_id, outputs=outputs, token_usage=token_usage)
        except Exception as exc:
            logger.debug("on_llm_end capture failed: %s", exc)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM errors. Keeps any text streamed before the error."""
        try:
            outputs = None
            streamed = self._streamed_text(run_id)
            if streamed:
                outputs = {"text": streamed, "streamed": True, "partial": True}
            self._finish_run(run_id, outputs=outputs, error=error)
        except Exception as exc:
            logger.debug("on_llm_error capture failed: %s", exc)

    # Chat model callbacks

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[Any],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chat model starts."""
        if not self.capture_llm:
            return

        try:
            serialized = serialized or {}
            self._start_run(
                "llm",
                _extract_model_name(serialized, kwargs),
                {"messages": _serialize_messages(messages, self.max_input_chars)},
                run_id, parent_run_id, tags, metadata,
                model_parameters=_extract_model_params(serialized, kwargs),
            )
        except Exception as exc:
            logger.debug("on_chat_model_start capture failed: %s", exc)

    # Chain callbacks

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain starts."""
        if not self.capture_chains:
            return

        try:
            self._start_run(
                "chain",
                _extract_chain_name(serialized or {}),
                _truncate_dict(inputs, self.max_input_chars),
                run_id, parent_run_id, tags, metadata,
            )
        except Exception as exc:
            logger.debug("on_chain_start capture failed: %s", exc)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain finishes."""
        try:
            self._finish_run(
                run_id, outputs=_truncate_dict(outputs, self.max_output_chars)
            )
        except Exception as exc:
            logger.debug("on_chain_end capture failed: %s", exc)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain errors."""
        try:
            self._finish_run(run_id, error=error)
        except Exception as exc:
            logger.debug("on_chain_error capture failed: %s", exc)

    # Tool callbacks

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool starts."""
        if not self.capture_tools:
            return

        try:
            serialized = serialized or {}
            tool_name = serialized.get("name", serialized.get("id", ["unknown"])[-1]
                                       if isinstance(serialized.get("id"), list)
                                       else "unknown_tool")
            self._start_run(
                "tool",
                tool_name,
                {"input": str(input_str)[:self.max_input_chars]},
                run_id, parent_run_id, tags, metadata,
            )
        except Exception as exc:
            logger.debug("on_tool_start capture failed: %s", exc)

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool finishes."""
        try:
            self._finish_run(
                run_id, outputs={"output": str(output)[:self.max_output_chars]}
            )
        except Exception as exc:
            logger.debug("on_tool_end capture failed: %s", exc)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool errors."""
        try:
            self._finish_run(run_id, error=error)
        except Exception as exc:
            logger.debug("on_tool_error capture failed: %s", exc)

    # Retriever callbacks

    def on_retriever_start(
        self,
        serialized: Dict[str, Any],
        query: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Called when a retriever starts."""
        if not self.capture_retrievers:
            return

        try:
            self._start_run(
                "retriever",
                (serialized or {}).get("name", "retriever"),
                {"query": str(query)[:self.max_input_chars]},
                run_id, parent_run_id, tags, metadata,
            )
        except Exception as exc:
            logger.debug("on_retriever_start capture failed: %s", exc)

    def on_retriever_end(
        self,
        documents: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a retriever finishes."""
        try:
            doc_summaries = _serialize_documents(documents, self.max_output_chars)
            self._finish_run(run_id, outputs={
                "document_count": len(doc_summaries),
                "documents": doc_summaries,
            })
        except Exception as exc:
            logger.debug("on_retriever_end capture failed: %s", exc)

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a retriever errors."""
        try:
            self._finish_run(run_id, error=error)
        except Exception as exc:
            logger.debug("on_retriever_error capture failed: %s", exc)

    # Export helpers

    def _assemble_decision_record(self, decision: CapturedDecision) -> Dict[str, Any]:
        """Build the exportable dict for a completed decision.

        ``child_spans`` holds the completed decisions still in the ring
        buffer that descend from it.
        """
        with self._lock:
            completed = list(self._decisions)
        family = {decision.decision_id}
        children: List[Dict[str, Any]] = []
        # Children complete before their parents, so walk newest-first to
        # resolve each generation before the one below it.
        for candidate in reversed(completed):
            if candidate.parent_run_id in family and candidate is not decision:
                family.add(candidate.decision_id)
                children.append(candidate.to_dict())
        children.reverse()
        record = decision.to_dict()
        record["child_spans"] = children
        return record

    # No-op callbacks required by the LangChain interface

    def on_text(self, text: str, **kwargs: Any) -> None:
        """Generic text event; not captured."""
        pass

    def on_agent_action(self, action: Any, **kwargs: Any) -> None:
        """Agent action; captured via tool callbacks."""
        pass

    def on_agent_finish(self, finish: Any, **kwargs: Any) -> None:
        """Agent finish; captured via chain callbacks."""
        pass


# Private helpers

def _extract_model_name(
    serialized: Dict[str, Any], kwargs: Dict[str, Any]
) -> str:
    """Extract the model name from serialized config or kwargs."""
    for key in ("model_name", "model", "model_id"):
        if key in kwargs:
            return str(kwargs[key])

    ser_kwargs = serialized.get("kwargs", {})
    for key in ("model_name", "model", "model_id"):
        if key in ser_kwargs:
            return str(ser_kwargs[key])

    id_path = serialized.get("id", [])
    if isinstance(id_path, list) and id_path:
        return id_path[-1]

    return serialized.get("name", "unknown_model")


def _extract_model_params(
    serialized: Dict[str, Any], kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Extract model parameters from serialized config."""
    params = {}
    ser_kwargs = serialized.get("kwargs", {})

    for key in ("temperature", "max_tokens", "top_p", "frequency_penalty",
                "presence_penalty", "stop", "model_name", "model"):
        if key in ser_kwargs:
            params[key] = ser_kwargs[key]
        elif key in kwargs:
            params[key] = kwargs[key]

    return params


def _extract_chain_name(serialized: Dict[str, Any]) -> str:
    """Extract the chain class name from serialized config."""
    id_path = serialized.get("id", [])
    if isinstance(id_path, list) and id_path:
        return id_path[-1]
    return serialized.get("name", "unknown_chain")


def _extract_llm_output(response: Any) -> tuple:
    """Extract text and token usage from an LLM response.

    Returns (output_text, token_usage_dict_or_None).
    """
    output_text = ""
    token_usage = None

    if response is None:
        return output_text, token_usage

    # LangChain LLMResult exposes .generations and .llm_output
    if hasattr(response, "generations"):
        gens = response.generations
        if gens and len(gens) > 0 and len(gens[0]) > 0:
            first_gen = gens[0][0]
            text = getattr(first_gen, "text", None)
            if isinstance(text, str) and text:
                output_text = text
            elif hasattr(first_gen, "message") and hasattr(first_gen.message, "content"):
                content = first_gen.message.content
                output_text = content if isinstance(content, str) else (
                    str(content) if content else ""
                )
            elif text is not None:
                output_text = text

    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        token_usage = _normalize_usage(
            llm_output.get("token_usage") or llm_output.get("usage")
        )

    # Current chat models report usage on the message rather than in
    # llm_output: AIMessage.usage_metadata, or response_metadata["token_usage"].
    if token_usage is None:
        token_usage = _usage_from_generations(getattr(response, "generations", None))

    return output_text, token_usage


def _normalize_usage(usage: Any) -> Optional[Dict[str, int]]:
    """Map a provider usage mapping/object onto prompt/completion/total tokens.

    Accepts both the legacy ``prompt_tokens`` / ``completion_tokens`` keys and
    the ``usage_metadata`` keys ``input_tokens`` / ``output_tokens``.
    """
    if not usage:
        return None

    def read(*names: str) -> Optional[int]:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return None

    prompt = read("prompt_tokens", "input_tokens")
    completion = read("completion_tokens", "output_tokens")
    total = read("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    prompt = prompt or 0
    completion = completion or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total is not None else prompt + completion,
    }


def _usage_from_generations(generations: Any) -> Optional[Dict[str, int]]:
    """Sum token usage reported on each generation's message."""
    totals: Optional[Dict[str, int]] = None
    try:
        batches = list(generations or [])
    except TypeError:
        return None
    for batch in batches:
        if not isinstance(batch, (list, tuple)):
            batch = [batch]
        for generation in batch:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            usage = _normalize_usage(getattr(message, "usage_metadata", None))
            if usage is None:
                response_metadata = getattr(message, "response_metadata", None)
                if isinstance(response_metadata, dict):
                    usage = _normalize_usage(
                        response_metadata.get("token_usage")
                        or response_metadata.get("usage")
                    )
            if usage is None:
                continue
            if totals is None:
                totals = dict(usage)
            else:
                for key, value in usage.items():
                    totals[key] += value
    return totals


def _serialize_messages(
    messages: List[Any], max_chars: int
) -> List[Dict[str, str]]:
    """Serialize LangChain message lists to dicts."""
    result = []
    for msg_list in messages:
        if not isinstance(msg_list, (list, tuple)):
            msg_list = [msg_list]
        for msg in msg_list:
            if hasattr(msg, "type") and hasattr(msg, "content"):
                result.append({
                    "role": getattr(msg, "type", "unknown"),
                    "content": str(getattr(msg, "content", ""))[:max_chars],
                })
            elif isinstance(msg, dict):
                result.append({
                    "role": msg.get("role", "unknown"),
                    "content": str(msg.get("content", ""))[:max_chars],
                })
            else:
                result.append({"role": "unknown", "content": str(msg)[:max_chars]})
    return result


def _serialize_documents(
    documents: Any, max_chars: int
) -> List[Dict[str, Any]]:
    """Serialize retrieved documents to summaries."""
    if documents is None:
        return []

    result = []
    for doc in documents:
        if hasattr(doc, "page_content") and hasattr(doc, "metadata"):
            result.append({
                "content_preview": doc.page_content[:200],
                "metadata": doc.metadata if isinstance(doc.metadata, dict) else {},
            })
        elif isinstance(doc, dict):
            content = doc.get("page_content", doc.get("content", ""))
            result.append({
                "content_preview": str(content)[:200],
                "metadata": doc.get("metadata", {}),
            })
        else:
            result.append({"content_preview": str(doc)[:200]})

    return result


def _truncate_dict(d: Any, max_chars: int) -> Dict[str, Any]:
    """Truncate string values in a dict."""
    if not isinstance(d, dict):
        return {"value": str(d)[:max_chars]}

    result: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = v[:max_chars]
        elif isinstance(v, dict):
            result[k] = _truncate_dict(v, max_chars)
        else:
            result[k] = v
    return result


def _merge_tags(
    tags: Optional[List[str]], metadata: Optional[Dict[str, Any]]
) -> Dict[str, str]:
    """Merge a tags list and a metadata dict into one flat dict."""
    result = {}
    if tags:
        for i, tag in enumerate(tags):
            result[f"tag_{i}"] = tag
    if metadata:
        for k, v in metadata.items():
            result[str(k)] = str(v)
    return result


def _emit_otel_event(name: str, attributes: Dict[str, Any]) -> None:
    """Emit an OTel span event when tracing is active."""
    if not HAS_OTEL:
        return
    try:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.add_event(f"briefcase.langchain.{name}", attributes=attributes)
    except Exception as exc:
        logger.debug("Failed to emit OTel event %s: %s", name, exc)


def require_langchain() -> None:
    """Raise ImportError with an install hint when langchain-core is absent."""
    try:
        import langchain_core  # noqa: F401,F811
    except ImportError as exc:
        raise ImportError(
            "langchain-core is required for BriefcaseLangChainHandler. "
            "Install it with: pip install langchain-core"
        ) from exc
