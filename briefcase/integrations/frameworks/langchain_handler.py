"""LangChain callback handler for automatic AI decision capture.

Intercepts LLM calls, chain executions, tool invocations, and retriever
operations and records each as a Briefcase decision record. Top-level chain
completions are exported through the configured exporter with their child
spans attached.

Usage:
    from briefcase.integrations.frameworks import BriefcaseLangChainHandler

    handler = BriefcaseLangChainHandler(context_version="v2")

    # Pass as a callback to any LangChain component
    llm = ChatOpenAI(callbacks=[handler])
    chain.invoke({"input": "..."}, config={"callbacks": [handler]})

    decisions = handler.get_decisions()
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from briefcase._export_mixin import ExportMixin

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
    - LLM calls (model, prompts, completions, token usage)
    - Chain executions (inputs, outputs, chain type)
    - Tool invocations (tool name, input, output)
    - Retriever queries (query, retrieved documents)

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
    ):
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

        # Accumulated decisions
        self._decisions: List[CapturedDecision] = []

        # In-flight tracking (run_id to partial decision)
        self._inflight: Dict[str, CapturedDecision] = {}

        # LangChain BaseCallbackHandler compatibility flags
        self.raise_error = False
        self.run_inline = True
        self.ignore_llm = not capture_llm
        self.ignore_chain = not capture_chains
        self.ignore_agent = False
        self.ignore_retriever = not capture_retrievers

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

    @property
    def decision_count(self) -> int:
        """Number of completed decisions captured."""
        return len(self._decisions)

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
            run_key = str(run_id) if run_id else str(uuid.uuid4())
            model_name = _extract_model_name(serialized, kwargs)
            model_params = _extract_model_params(serialized, kwargs)

            truncated_prompts = [p[:self.max_input_chars] for p in prompts]

            decision = CapturedDecision(
                decision_id=run_key,
                decision_type="llm",
                function_name=model_name,
                inputs={"prompts": truncated_prompts},
                model_parameters=model_params,
                started_at=datetime.now(timezone.utc),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                tags=_merge_tags(tags, metadata),
                context_version=self.context_version,
            )
            self._inflight[run_key] = decision

            if HAS_OTEL:
                _emit_otel_event("llm_start", {
                    "model": model_name,
                    "prompt_count": len(prompts),
                })
        except Exception:
            pass

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM finishes generating."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        output_text, token_usage = _extract_llm_output(response)
        decision.outputs = {
            "text": output_text[:self.max_output_chars] if output_text else None,
        }
        decision.token_usage = token_usage
        self._decisions.append(decision)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM errors."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000
        decision.error = str(error)
        self._decisions.append(decision)

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

        run_key = str(run_id) if run_id else str(uuid.uuid4())
        model_name = _extract_model_name(serialized, kwargs)
        model_params = _extract_model_params(serialized, kwargs)

        serialized_messages = _serialize_messages(messages, self.max_input_chars)

        decision = CapturedDecision(
            decision_id=run_key,
            decision_type="llm",
            function_name=model_name,
            inputs={"messages": serialized_messages},
            model_parameters=model_params,
            started_at=datetime.now(timezone.utc),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            tags=_merge_tags(tags, metadata),
            context_version=self.context_version,
        )
        self._inflight[run_key] = decision

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
            run_key = str(run_id) if run_id else str(uuid.uuid4())
            chain_name = _extract_chain_name(serialized)

            decision = CapturedDecision(
                decision_id=run_key,
                decision_type="chain",
                function_name=chain_name,
                inputs=_truncate_dict(inputs, self.max_input_chars),
                started_at=datetime.now(timezone.utc),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                tags=_merge_tags(tags, metadata),
                context_version=self.context_version,
            )
            self._inflight[run_key] = decision
        except Exception:
            pass

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain finishes. Exports top-level chains."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000
        decision.outputs = _truncate_dict(outputs, self.max_output_chars)
        self._decisions.append(decision)

        if parent_run_id is None:
            self._trigger_export(self._assemble_decision_record(decision))

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a chain errors."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000
        decision.error = str(error)
        self._decisions.append(decision)

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
            run_key = str(run_id) if run_id else str(uuid.uuid4())
            tool_name = serialized.get("name", serialized.get("id", ["unknown"])[-1]
                                       if isinstance(serialized.get("id"), list)
                                       else "unknown_tool")

            decision = CapturedDecision(
                decision_id=run_key,
                decision_type="tool",
                function_name=tool_name,
                inputs={"input": input_str[:self.max_input_chars]},
                started_at=datetime.now(timezone.utc),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                tags=_merge_tags(tags, metadata),
                context_version=self.context_version,
            )
            self._inflight[run_key] = decision
        except Exception:
            pass

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool finishes."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000
        decision.outputs = {"output": str(output)[:self.max_output_chars]}
        self._decisions.append(decision)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a tool errors."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000
        decision.error = str(error)
        self._decisions.append(decision)

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

        run_key = str(run_id) if run_id else str(uuid.uuid4())
        retriever_name = serialized.get("name", "retriever")

        decision = CapturedDecision(
            decision_id=run_key,
            decision_type="retriever",
            function_name=retriever_name,
            inputs={"query": query[:self.max_input_chars]},
            started_at=datetime.now(timezone.utc),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            tags=_merge_tags(tags, metadata),
        )
        self._inflight[run_key] = decision

    def on_retriever_end(
        self,
        documents: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a retriever finishes."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000

        doc_summaries = _serialize_documents(documents, self.max_output_chars)
        decision.outputs = {
            "document_count": len(doc_summaries),
            "documents": doc_summaries,
        }
        self._decisions.append(decision)

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Called when a retriever errors."""
        run_key = str(run_id) if run_id else None
        decision = self._inflight.pop(run_key, None) if run_key else None
        if decision is None:
            return

        decision.ended_at = datetime.now(timezone.utc)
        if decision.started_at:
            delta = (decision.ended_at - decision.started_at).total_seconds()
            decision.execution_time_ms = delta * 1000
        decision.error = str(error)
        self._decisions.append(decision)

    # Export helpers

    def _assemble_decision_record(self, decision: CapturedDecision) -> Dict[str, Any]:
        """Build the exportable dict for a decision, including child spans."""
        record = decision.to_dict()
        record["child_spans"] = [
            d.to_dict() for d in self._decisions
            if d.parent_run_id == decision.decision_id
        ]
        return record

    # No-op callbacks required by the LangChain interface

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        """Streaming token; not captured."""
        pass

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
            if hasattr(first_gen, "text"):
                output_text = first_gen.text
            elif hasattr(first_gen, "message") and hasattr(first_gen.message, "content"):
                output_text = first_gen.message.content

    if hasattr(response, "llm_output") and response.llm_output:
        usage = response.llm_output.get("token_usage", {})
        if usage:
            token_usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }

    return output_text, token_usage


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
    except Exception:
        pass


def require_langchain() -> None:
    """Raise ImportError with an install hint when langchain-core is absent."""
    try:
        import langchain_core  # noqa: F401,F811
    except ImportError as exc:
        raise ImportError(
            "langchain-core is required for BriefcaseLangChainHandler. "
            "Install it with: pip install langchain-core"
        ) from exc
