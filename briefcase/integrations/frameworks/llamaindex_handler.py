"""LlamaIndex callback handler for automatic AI decision capture.

Intercepts LLM calls, query engine operations, retriever events, and
embedding operations and records each as a Briefcase decision record. Every
root event is exported once, when it ends or fails, with its nested events
attached as ``child_spans``; a standalone LLM, retriever or embedding call
(one made outside any query) is its own root and is exported too.

Payload keys and token-usage locations were verified against
llama-index-core 0.14.26 (``llama_index.core.callbacks.schema.EventPayload``,
``llama_index.core.llms.callbacks`` and
``llama_index.core.callbacks.token_counting.get_tokens_from_response``). The
older key names this handler first shipped with are still read.

Usage:
    from briefcase.integrations.frameworks import BriefcaseLlamaIndexHandler

    handler = BriefcaseLlamaIndexHandler(context_version="v2")

    # As a global callback
    # from llama_index.core import Settings
    # Settings.callback_manager.add_handler(handler)

    decisions = handler.get_decisions()
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Union

from briefcase._export_mixin import ExportMixin
from briefcase._logging import get_logger
from briefcase.integrations.frameworks._capture_state import (
    DEFAULT_INFLIGHT_TTL_SECONDS,
    DEFAULT_MAX_INFLIGHT,
    DEFAULT_MAX_RECORDS,
    CaptureOptions,
    InflightStore,
    LLMRunScopes,
    bounded_records,
)
from briefcase.integrations.frameworks.langchain_handler import CapturedDecision

logger = get_logger(__name__)

_INSTALL_HINT = (
    "llama-index-core is required for BriefcaseLlamaIndexHandler. "
    "Install with: pip install briefcase-ai[llamaindex] or pip install llama-index-core"
)

try:
    import llama_index.core  # noqa: F401
    _LLAMAINDEX_AVAILABLE = True
except ImportError:
    _LLAMAINDEX_AVAILABLE = False


# parent_id the LlamaIndex CallbackManager gives a top-level event
# (llama_index.core.callbacks.schema.BASE_TRACE_EVENT).
_BASE_TRACE_EVENT = "root"


class EventType:
    """LlamaIndex event type constants, mirrored to avoid importing llama_index."""
    LLM = "llm"
    EMBEDDING = "embedding"
    RETRIEVE = "retrieve"
    QUERY = "query"
    SYNTHESIZE = "synthesize"
    CHUNKING = "chunking"
    RERANKING = "reranking"
    EXCEPTION = "exception"
    TEMPLATING = "templating"
    SUB_QUESTION = "sub_question"
    TREE = "tree"
    AGENT_STEP = "agent_step"


class BriefcaseLlamaIndexHandler(ExportMixin):
    """LlamaIndex callback handler that captures AI decisions for Briefcase.

    Implements the LlamaIndex BaseCallbackHandler interface via duck typing
    (no llama_index import required at call time). Captures:
    - LLM calls (model, prompts, completions, token counts)
    - Embedding operations (model, text count, dimensions)
    - Retrieval events (query, document count, scores)
    - Query and synthesis events (full pipeline tracking)

    Uses the on_event_start/on_event_end pairing from LlamaIndex.

    Export: every *root* event is exported once, when it ends or fails, with
    all of its nested events attached as ``child_spans``. A root is an event
    with no parent, the callback manager's ``"root"`` parent, or a parent
    this handler is not tracking (for example an LLM call under a query when
    ``capture_queries=False``), so a standalone LLM, retriever or embedding
    call is exported too. A failure reaches the handler either as the event's
    own end with an ``exception`` payload (what LlamaIndex emits) or as a
    separate ``exception`` event.

    Completed decisions are kept in a ring buffer of ``max_records``. An
    event whose end never arrives is dropped from the in-flight table after
    ``inflight_ttl_seconds`` (or when ``max_inflight`` events are open) and
    recorded with an ``abandoned`` error. ``capture_content`` (default
    ``"none"``: no content) and ``redact`` take the same values as the
    provider integrations.
    """

    def __init__(
        self,
        capture_llm: bool = True,
        capture_embeddings: bool = True,
        capture_retrievals: bool = True,
        capture_queries: bool = True,
        max_input_chars: int = 10000,
        max_output_chars: int = 10000,
        event_starts_to_ignore: Optional[List[str]] = None,
        event_ends_to_ignore: Optional[List[str]] = None,
        context_version: Optional[str] = None,
        exporter: Any = None,
        async_capture: bool = True,
        capture_content: Union[bool, str, None] = "none",
        redact: Optional[Callable[[str], str]] = None,
        max_records: Optional[int] = DEFAULT_MAX_RECORDS,
        max_inflight: Optional[int] = DEFAULT_MAX_INFLIGHT,
        inflight_ttl_seconds: Optional[float] = DEFAULT_INFLIGHT_TTL_SECONDS,
    ):
        if not _LLAMAINDEX_AVAILABLE:
            raise ImportError(_INSTALL_HINT)

        self.capture_llm = capture_llm
        self.capture_embeddings = capture_embeddings
        self.capture_retrievals = capture_retrievals
        self.capture_queries = capture_queries
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self.context_version = context_version
        self._exporter = exporter
        self.async_capture = async_capture

        # LlamaIndex BaseCallbackHandler interface
        self.event_starts_to_ignore = event_starts_to_ignore or []
        self.event_ends_to_ignore = event_ends_to_ignore or []

        self._capture_options = CaptureOptions(capture_content, redact)
        self.max_records = max_records
        self._lock = threading.RLock()

        # Completed decisions (ring buffer)
        self._decisions: Deque[CapturedDecision] = bounded_records(max_records)

        # In-flight tracking (event_id to partial decision)
        # Publishes open LLM events to provider auto-instrumentation.
        self._llm_scopes = LLMRunScopes("llamaindex")
        self._inflight = InflightStore(
            max_size=max_inflight,
            ttl_seconds=inflight_ttl_seconds,
            on_evict=self._on_inflight_evicted,
        )

        # event_id -> event_id of the root it will be exported with
        self._root_of: Dict[str, str] = {}
        # root event_id -> completed descendants awaiting the root's export
        self._children: Dict[str, Deque[CapturedDecision]] = {}

        # Trace map for hierarchical event tracking
        self._trace_map: Dict[str, List[str]] = {}
        self._current_trace_id: Optional[str] = None

    # Public API

    def get_decisions(self) -> List[CapturedDecision]:
        """Return the captured decisions (the most recent ``max_records``)."""
        with self._lock:
            return list(self._decisions)

    def get_decisions_as_dicts(self) -> List[Dict[str, Any]]:
        """Return all captured decisions as serializable dicts."""
        return [d.to_dict() for d in self.get_decisions()]

    def clear(self) -> None:
        """Clear all captured decisions and in-flight state."""
        with self._lock:
            self._decisions.clear()
            self._inflight.clear()
            self._root_of.clear()
            self._children.clear()
            self._trace_map.clear()

    @property
    def decision_count(self) -> int:
        """Number of completed decisions captured."""
        return len(self._decisions)

    # Event lifecycle

    def _begin(self, event_id: str, parent_id: str, decision: CapturedDecision) -> None:
        """Track an event from its start, and settle which root exports it."""
        with self._lock:
            # An event is exported with its root. It is its own root when it
            # has no parent, or when the parent is not an event this handler
            # tracks (the callback manager's "root", or a filtered type).
            if parent_id and parent_id in self._root_of:
                self._root_of[event_id] = self._root_of[parent_id]
            else:
                self._root_of[event_id] = event_id
            self._inflight[event_id] = decision

    def _finish(
        self,
        event_id: str,
        payload: Dict[str, Any],
        fill: Callable[[CapturedDecision, Dict[str, Any]], None],
    ) -> None:
        """Complete an event from its end payload and export it if a root."""
        with self._lock:
            decision = self._inflight.pop(event_id, None)
            if decision is None:
                return
            # LlamaIndex reports a failure as the event's own end, carrying
            # the exception instead of a response.
            failure = payload.get("exception")
            if failure is not None:
                decision.error = str(failure) or type(failure).__name__
            else:
                fill(decision, payload)
            record = self._complete(event_id, decision)
        if record is not None:
            self._trigger_export(record)

    def _complete(
        self, event_id: str, decision: CapturedDecision
    ) -> Optional[Dict[str, Any]]:
        """Finalize a decision. Returns the export record when it is a root.

        Caller holds ``self._lock``.
        """
        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        options = self._capture_options
        decision.inputs = options.content(decision.inputs)
        decision.outputs = options.content(decision.outputs)
        decision.error = options.error(decision.error)
        self._decisions.append(decision)

        root_key = self._root_of.pop(event_id, event_id)
        if root_key != event_id and root_key in self._inflight:
            # Nested event: held until its root finishes.
            children = self._children.get(root_key)
            if children is None:
                children = self._children[root_key] = bounded_records(self.max_records)
            children.append(decision)
            return None

        # Root event (or a nested event whose root is gone): export it now.
        record = decision.to_dict()
        record["child_spans"] = [
            child.to_dict() for child in self._children.pop(event_id, ())
        ]
        return record

    def _on_inflight_evicted(self, event_id: Any, decision: Any, reason: str) -> None:
        """Record an event whose end never arrived as abandoned."""
        self._llm_scopes.finish(event_id)
        with self._lock:
            decision.error = f"abandoned: no end event ({reason})"
            record = self._complete(str(event_id), decision)
        if record is not None:
            self._trigger_export(record)

    # LlamaIndex callback interface

    def on_event_start(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        parent_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Called when a LlamaIndex event starts.

        Returns event_id for correlation with on_event_end.
        """
        if event_type in self.event_starts_to_ignore:
            return event_id

        if not event_id:
            event_id = str(uuid.uuid4())

        payload = payload or {}

        if event_type == EventType.LLM and self.capture_llm:
            self._on_llm_start(event_id, parent_id, payload)
        elif event_type == EventType.EMBEDDING and self.capture_embeddings:
            self._on_embedding_start(event_id, parent_id, payload)
        elif event_type == EventType.RETRIEVE and self.capture_retrievals:
            self._on_retrieve_start(event_id, parent_id, payload)
        elif event_type == EventType.QUERY and self.capture_queries:
            self._on_query_start(event_id, parent_id, payload)
        elif event_type == EventType.SYNTHESIZE and self.capture_queries:
            self._on_synthesize_start(event_id, parent_id, payload)

        return event_id

    def on_event_end(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        event_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Called when a LlamaIndex event ends."""
        if event_type in self.event_ends_to_ignore:
            return

        payload = payload or {}

        if event_type == EventType.LLM and self.capture_llm:
            self._on_llm_end(event_id, payload)
        elif event_type == EventType.EMBEDDING and self.capture_embeddings:
            self._on_embedding_end(event_id, payload)
        elif event_type == EventType.RETRIEVE and self.capture_retrievals:
            self._on_retrieve_end(event_id, payload)
        elif event_type == EventType.QUERY and self.capture_queries:
            self._on_query_end(event_id, payload)
        elif event_type == EventType.SYNTHESIZE and self.capture_queries:
            self._on_synthesize_end(event_id, payload)
        elif event_type == EventType.EXCEPTION:
            self._on_exception(event_id, payload)

    def start_trace(self, trace_id: Optional[str] = None) -> None:
        """Called when a new trace (query pipeline) begins."""
        self._current_trace_id = trace_id or str(uuid.uuid4())
        self._trace_map[self._current_trace_id] = []

    def end_trace(
        self,
        trace_id: Optional[str] = None,
        trace_map: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        """Called when a trace (query pipeline) ends."""
        tid = trace_id or self._current_trace_id
        if tid and tid in self._trace_map:
            del self._trace_map[tid]
        self._current_trace_id = None

    # Internal event handlers

    def _on_llm_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        # Current LlamaIndex describes the model under "serialized"; older
        # payloads used "model_name" / "model_dict".
        serialized = _as_dict(payload.get("serialized"))
        model_dict = _as_dict(payload.get("model_dict"))
        model_name = _model_name(
            payload.get("model_name"),
            model_dict.get("model"),
            serialized.get("model"),
            serialized.get("model_name"),
            serialized.get("class_name"),
            default="unknown_llm",
        )

        messages = payload.get("messages", [])
        prompt = payload.get("formatted_prompt", "")
        template = payload.get("template", "")

        input_data: Dict[str, Any] = {}
        if messages:
            input_data["messages"] = _serialize_payload_messages(
                messages, self.max_input_chars
            )
        if prompt:
            input_data["prompt"] = str(prompt)[:self.max_input_chars]
        if template:
            input_data["template"] = str(template)[:self.max_input_chars]

        model_params = {}
        for source in (serialized, model_dict):
            for key in ("temperature", "max_tokens", "top_p"):
                if source.get(key) is not None:
                    model_params[key] = source[key]

        self._llm_scopes.start(event_id)
        self._begin(event_id, parent_id, CapturedDecision(
            decision_id=event_id,
            decision_type="llm",
            function_name=str(model_name),
            inputs=input_data,
            model_parameters=model_params,
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        ))

    def _on_llm_end(self, event_id: str, payload: Dict[str, Any]) -> None:
        if self._llm_scopes.finish(event_id):
            # auto(dedupe="framework"): a provider record stands for this call.
            with self._lock:
                self._inflight.pop(event_id, None)
                self._root_of.pop(event_id, None)
                self._children.pop(event_id, None)
            return
        self._finish(event_id, payload, self._fill_llm)

    def _fill_llm(self, decision: CapturedDecision, payload: Dict[str, Any]) -> None:
        # A chat call ends with "response" (ChatResponse), a completion call
        # with "completion" (CompletionResponse).
        response = payload.get("response")
        if response is None:
            response = payload.get("completion", "")
        if getattr(response, "text", None) is not None:
            response_text = response.text
        elif hasattr(response, "message") and hasattr(response.message, "content"):
            response_text = response.message.content
        else:
            response_text = response
        decision.outputs = {
            "text": str("" if response_text is None else response_text)[:self.max_output_chars],
        }
        token_usage = _token_usage(payload, response)
        if token_usage:
            decision.token_usage = token_usage

    def _on_embedding_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        serialized = payload.get("serialized", {})
        described = _as_dict(serialized)
        model_name = _model_name(
            payload.get("model_name"),
            described.get("model_name"),
            described.get("model"),
            described.get("class_name"),
            default="unknown_embedding_model",
        )

        self._begin(event_id, parent_id, CapturedDecision(
            decision_id=event_id,
            decision_type="embedding",
            function_name=str(model_name),
            inputs={
                "text_count": len(payload.get("texts", [])),
            },
            model_parameters=serialized if isinstance(serialized, dict) else {},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        ))

    def _on_embedding_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        self._finish(event_id, payload, self._fill_embedding)

    def _fill_embedding(self, decision: CapturedDecision, payload: Dict[str, Any]) -> None:
        # Current LlamaIndex ends an embedding event with the embedded texts
        # under "chunks" and the vectors under "embeddings"; older payloads
        # carried the vectors under "chunks".
        chunks = payload.get("chunks") or []
        vectors = payload.get("embeddings")
        if vectors is None:
            vectors = chunks
        elif chunks:
            decision.inputs = {"text_count": len(chunks)}
        first = vectors[0] if vectors else None
        decision.outputs = {
            "embedding_count": len(vectors),
            "dimensions": len(first) if isinstance(first, (list, tuple)) else 0,
        }

    def _on_retrieve_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        query_str = payload.get("query_str", "")

        self._begin(event_id, parent_id, CapturedDecision(
            decision_id=event_id,
            decision_type="retriever",
            function_name="retriever",
            inputs={"query": str(query_str)[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        ))

    def _on_retrieve_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        self._finish(event_id, payload, self._fill_retrieve)

    def _fill_retrieve(self, decision: CapturedDecision, payload: Dict[str, Any]) -> None:
        nodes = payload.get("nodes", [])
        doc_summaries = []
        for node in nodes:
            if hasattr(node, "text"):
                doc_summaries.append({
                    "content_preview": node.text[:200],
                    "score": getattr(node, "score", None),
                })
            elif hasattr(node, "node") and hasattr(node.node, "text"):
                doc_summaries.append({
                    "content_preview": node.node.text[:200],
                    "score": getattr(node, "score", None),
                })
            elif isinstance(node, dict):
                doc_summaries.append({
                    "content_preview": str(node.get("text", ""))[:200],
                    "score": node.get("score"),
                })

        decision.outputs = {
            "document_count": len(doc_summaries),
            "documents": doc_summaries,
        }

    def _on_query_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        query_str = payload.get("query_str", "")

        self._begin(event_id, parent_id, CapturedDecision(
            decision_id=event_id,
            decision_type="query",
            function_name="query_engine",
            inputs={"query": str(query_str)[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        ))

    def _on_query_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        self._finish(event_id, payload, self._fill_response)

    def _fill_response(self, decision: CapturedDecision, payload: Dict[str, Any]) -> None:
        response = payload.get("response", "")
        if hasattr(response, "response"):
            response_text = str(response.response)
        else:
            response_text = str(response)

        decision.outputs = {
            "response": response_text[:self.max_output_chars],
        }

    def _on_synthesize_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        query_str = payload.get("query_str", "")

        self._begin(event_id, parent_id, CapturedDecision(
            decision_id=event_id,
            decision_type="synthesize",
            function_name="synthesizer",
            inputs={"query": str(query_str)[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        ))

    def _on_synthesize_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        self._finish(event_id, payload, self._fill_response)

    def _on_exception(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        """Attach a separate exception event to the matching in-flight decision."""
        error = payload.get("exception", payload.get("error", "unknown error"))

        self._llm_scopes.finish(event_id)
        with self._lock:
            key = event_id
            decision = self._inflight.pop(event_id, None)
            if decision is None and self._inflight:
                # Fall back to the most recent in-flight decision
                key = list(self._inflight.keys())[-1]
                decision = self._inflight.pop(key)
            if decision is None:
                return
            decision.error = str(error) or type(error).__name__
            record = self._complete(key, decision)
        if record is not None:
            self._trigger_export(record)


# Helpers

def _serialize_payload_messages(
    messages: Any, max_chars: int
) -> List[Dict[str, str]]:
    """Serialize LlamaIndex messages to dicts."""
    result = []
    if not isinstance(messages, (list, tuple)):
        messages = [messages]

    for msg in messages:
        if hasattr(msg, "role") and hasattr(msg, "content"):
            role = getattr(msg, "role", "unknown")
            result.append({
                # MessageRole is a str enum; its value is the wire name.
                "role": str(getattr(role, "value", role)),
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


def _model_name(*candidates: Any, default: str) -> str:
    """First usable name. LlamaIndex reports ``"unknown"`` for a model with
    no configured name, so that placeholder yields to the next candidate
    (typically the integration's class name)."""
    for candidate in candidates:
        if candidate and candidate != "unknown":
            return str(candidate)
    return default


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read(source: Any, *names: str) -> Any:
    """First non-None value among ``names`` on a mapping or an object."""
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        if value is not None:
            return value
    return None


def _count(source: Any, *names: str) -> Optional[int]:
    value = _read(source, *names)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _normalize_usage(usage: Any) -> Optional[Dict[str, int]]:
    """Map a provider usage mapping/object onto prompt/completion/total tokens.

    The key names are the ones ``get_tokens_from_response`` accepts in
    llama-index-core 0.14.26 (OpenAI, Anthropic and Gemini spellings).
    """
    if not usage:
        return None
    prompt = _count(usage, "prompt_tokens", "input_tokens", "prompt_token_count")
    completion = _count(
        usage, "completion_tokens", "output_tokens", "candidates_token_count"
    )
    total = _count(usage, "total_tokens", "total_token_count")
    if prompt is None and completion is None and total is None:
        return None
    prompt = prompt or 0
    completion = completion or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if total is not None else prompt + completion,
    }


def _token_usage(payload: Dict[str, Any], response: Any) -> Optional[Dict[str, int]]:
    """Token usage for an LLM end event, or None when none was reported.

    LlamaIndex has no token-count payload key: the provider's usage sits on
    the response, under ``raw["usage"]`` / ``raw["usage_metadata"]`` (a dict
    or the provider SDK's response object), or flattened into
    ``additional_kwargs``. Only reported counts are recorded; nothing is
    estimated by tokenizing. A ``token_count`` payload entry, which older
    callers passed, is honoured first.
    """
    legacy = payload.get("token_count")
    if legacy:
        if isinstance(legacy, int) and not isinstance(legacy, bool):
            return {"prompt_tokens": legacy, "completion_tokens": 0, "total_tokens": legacy}
        usage = _normalize_usage(legacy)
        if usage:
            return usage

    raw = getattr(response, "raw", None)
    if raw is not None:
        usage = _normalize_usage(_read(raw, "usage", "usage_metadata"))
        if usage:
            return usage
    return _normalize_usage(getattr(response, "additional_kwargs", None))
