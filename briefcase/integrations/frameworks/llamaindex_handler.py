"""LlamaIndex callback handler for automatic AI decision capture.

Intercepts LLM calls, query engine operations, retriever events, and
embedding operations and records each as a Briefcase decision record.
Query completions are exported through the configured exporter.

Usage:
    from briefcase.integrations.frameworks import BriefcaseLlamaIndexHandler

    handler = BriefcaseLlamaIndexHandler(context_version="v2")

    # As a global callback
    # from llama_index.core import Settings
    # Settings.callback_manager.add_handler(handler)

    decisions = handler.get_decisions()
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from briefcase._export_mixin import ExportMixin
from briefcase.integrations.frameworks.langchain_handler import CapturedDecision

_INSTALL_HINT = (
    "llama-index-core is required for BriefcaseLlamaIndexHandler. "
    "Install with: pip install briefcase-ai[llamaindex] or pip install llama-index-core"
)

try:
    import llama_index.core  # noqa: F401
    _LLAMAINDEX_AVAILABLE = True
except ImportError:
    _LLAMAINDEX_AVAILABLE = False


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

        # Accumulated decisions
        self._decisions: List[CapturedDecision] = []

        # In-flight tracking (event_id to partial decision)
        self._inflight: Dict[str, CapturedDecision] = {}

        # Trace map for hierarchical event tracking
        self._trace_map: Dict[str, List[str]] = {}
        self._current_trace_id: Optional[str] = None

    # Public API

    def get_decisions(self) -> List[CapturedDecision]:
        """Return all captured decisions."""
        return list(self._decisions)

    def get_decisions_as_dicts(self) -> List[Dict[str, Any]]:
        """Return all captured decisions as serializable dicts."""
        return [d.to_dict() for d in self._decisions]

    def clear(self) -> None:
        """Clear all captured decisions and in-flight state."""
        self._decisions.clear()
        self._inflight.clear()
        self._trace_map.clear()

    @property
    def decision_count(self) -> int:
        """Number of completed decisions captured."""
        return len(self._decisions)

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
        model_name = (
            payload.get("model_name")
            or payload.get("model_dict", {}).get("model", "unknown_llm")
        )

        messages = payload.get("messages", [])
        template = payload.get("template", "")

        input_data: Dict[str, Any] = {}
        if messages:
            input_data["messages"] = _serialize_payload_messages(
                messages, self.max_input_chars
            )
        if template:
            input_data["template"] = str(template)[:self.max_input_chars]

        model_params = {}
        model_dict = payload.get("model_dict", {})
        for key in ("temperature", "max_tokens", "top_p"):
            if key in model_dict:
                model_params[key] = model_dict[key]

        decision = CapturedDecision(
            decision_id=event_id,
            decision_type="llm",
            function_name=model_name,
            inputs=input_data,
            model_parameters=model_params,
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        )
        self._inflight[event_id] = decision

    def _on_llm_end(self, event_id: str, payload: Dict[str, Any]) -> None:
        decision = self._inflight.pop(event_id, None)
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        response = payload.get("response", "")
        if hasattr(response, "text"):
            response_text = response.text
        elif hasattr(response, "message") and hasattr(response.message, "content"):
            response_text = response.message.content
        else:
            response_text = str(response)

        decision.outputs = {
            "text": response_text[:self.max_output_chars],
        }

        token_count = payload.get("token_count")
        if token_count:
            decision.token_usage = {
                "prompt_tokens": getattr(token_count, "prompt_tokens",
                                         token_count if isinstance(token_count, int) else 0),
                "completion_tokens": getattr(token_count, "completion_tokens", 0),
                "total_tokens": getattr(token_count, "total_tokens",
                                        token_count if isinstance(token_count, int) else 0),
            }

        self._decisions.append(decision)

    def _on_embedding_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        model_name = payload.get("model_name", "unknown_embedding_model")
        serialized = payload.get("serialized", {})

        decision = CapturedDecision(
            decision_id=event_id,
            decision_type="embedding",
            function_name=model_name,
            inputs={
                "text_count": len(payload.get("texts", [])),
            },
            model_parameters=serialized if isinstance(serialized, dict) else {},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        )
        self._inflight[event_id] = decision

    def _on_embedding_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        decision = self._inflight.pop(event_id, None)
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        chunks = payload.get("chunks", payload.get("embeddings", []))
        decision.outputs = {
            "embedding_count": len(chunks),
            "dimensions": len(chunks[0]) if chunks and isinstance(chunks[0], (list, tuple)) else 0,
        }
        self._decisions.append(decision)

    def _on_retrieve_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        query_str = payload.get("query_str", "")

        decision = CapturedDecision(
            decision_id=event_id,
            decision_type="retriever",
            function_name="retriever",
            inputs={"query": str(query_str)[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        )
        self._inflight[event_id] = decision

    def _on_retrieve_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        decision = self._inflight.pop(event_id, None)
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

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
        self._decisions.append(decision)

    def _on_query_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        query_str = payload.get("query_str", "")

        decision = CapturedDecision(
            decision_id=event_id,
            decision_type="query",
            function_name="query_engine",
            inputs={"query": str(query_str)[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        )
        self._inflight[event_id] = decision

    def _on_query_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        decision = self._inflight.pop(event_id, None)
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        response = payload.get("response", "")
        if hasattr(response, "response"):
            response_text = str(response.response)
        else:
            response_text = str(response)

        decision.outputs = {
            "response": response_text[:self.max_output_chars],
        }
        self._decisions.append(decision)
        self._trigger_export(decision.to_dict())

    def _on_synthesize_start(
        self, event_id: str, parent_id: str, payload: Dict[str, Any]
    ) -> None:
        query_str = payload.get("query_str", "")

        decision = CapturedDecision(
            decision_id=event_id,
            decision_type="synthesize",
            function_name="synthesizer",
            inputs={"query": str(query_str)[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=parent_id or None,
            context_version=self.context_version,
        )
        self._inflight[event_id] = decision

    def _on_synthesize_end(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        decision = self._inflight.pop(event_id, None)
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        response = payload.get("response", "")
        if hasattr(response, "response"):
            response_text = str(response.response)
        else:
            response_text = str(response)

        decision.outputs = {
            "response": response_text[:self.max_output_chars],
        }
        self._decisions.append(decision)

    def _on_exception(
        self, event_id: str, payload: Dict[str, Any]
    ) -> None:
        """Attach exception events to the matching in-flight decision."""
        error = payload.get("exception", payload.get("error", "unknown error"))

        decision = self._inflight.pop(event_id, None)
        if decision is None:
            # Fall back to the most recent in-flight decision
            if self._inflight:
                last_key = list(self._inflight.keys())[-1]
                decision = self._inflight.pop(last_key)

        if decision:
            decision.ended_at = datetime.now(timezone.utc)
            if decision.started_at:
                delta = (decision.ended_at - decision.started_at).total_seconds()
                decision.execution_time_ms = delta * 1000
            decision.error = str(error)
            self._decisions.append(decision)


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
            result.append({
                "role": str(getattr(msg, "role", "unknown")),
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
