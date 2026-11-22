"""Content checks and the guard that runs them through
:class:`briefcase.guardrails.GuardrailPipeline`.

Each check is a guardrail environment (``name`` + ``evaluate(EvalRequest)``
returning an :class:`~briefcase.guardrails.EvalResult`): ``DENY`` when it
found something, with the findings in ``metadata["findings"]``. The pipeline
runs every applicable check (``PipelineMode.ALL``) and turns a check that
raised into a ``DENY`` carrying ``metadata["error"]``; :class:`ContentGuard`
then applies each check's ``on_error`` (default: ``block`` for a ``block``
check, i.e. fail closed; ``warn`` otherwise).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from briefcase._logging import get_logger
from briefcase.guardrails._types import Effect, EvalRequest, EvalResult
from briefcase.guardrails.content.detectors import (
    Finding, injection_findings, pii_findings, redact,
)
from briefcase.guardrails.framework.pipeline import GuardrailPipeline, PipelineMode

logger = get_logger("briefcase.guardrails.content")

EFFECTS = ("block", "redact", "warn")
ON_ERROR = ("block", "warn", "allow")
DIRECTIONS = ("input", "output")
SOURCES = ("system", "user", "assistant", "tool", "retrieved")
_RANK = {"allow": 0, "warn": 1, "redact": 2, "block": 3}


class Check:
    """Base class. Subclasses set ``kind`` and implement :meth:`find`.

    Args:
        effect: ``"block"``, ``"redact"`` (only for checks whose findings are
            spans of text) or ``"warn"``.
        direction: ``"input"`` (request text, before it is sent),
            ``"output"`` (the completion) or ``"both"``.
        sources: For input: which request texts to check (``system``,
            ``user``, ``assistant``, ``tool``, ``retrieved``). Default all.
        on_error: What a check that raises means: ``"block"`` (fail closed;
            the default for ``effect="block"``), ``"warn"`` (log, allow; the
            default otherwise) or ``"allow"``.
        name: Label in records and logs.
    """

    kind = "check"
    can_redact = False

    def __init__(self, effect: str = "warn", *, direction: str = "both",
                 sources: Optional[Sequence[str]] = None, on_error: Optional[str] = None,
                 name: Optional[str] = None) -> None:
        if effect not in EFFECTS:
            raise ValueError(f"effect must be one of {EFFECTS}, got {effect!r}")
        if effect == "redact" and not self.can_redact:
            raise ValueError(f"{type(self).__name__} cannot redact (its findings are not spans "
                             "of text); use 'block' or 'warn'")
        if direction not in DIRECTIONS + ("both",):
            raise ValueError(f"direction must be 'input', 'output' or 'both', got {direction!r}")
        on_error = on_error or ("block" if effect == "block" else "warn")
        if on_error not in ON_ERROR:
            raise ValueError(f"on_error must be one of {ON_ERROR}, got {on_error!r}")
        unknown = set(sources or ()) - set(SOURCES)
        if unknown:
            raise ValueError(f"unknown sources {sorted(unknown)}; use {SOURCES}")
        self.effect = effect
        self.direction = direction
        self.sources = tuple(sources) if sources else SOURCES
        self.on_error = on_error
        self.name = name or self.kind

    def applies(self, direction: str, source: str) -> bool:
        if self.direction not in (direction, "both"):
            return False
        return direction == "output" or source in self.sources

    def find(self, text: str, source: str) -> List[Finding]:  # pragma: no cover - abstract
        raise NotImplementedError

    # GuardrailEnv protocol (what GuardrailPipeline calls)
    def evaluate(self, request: EvalRequest) -> EvalResult:
        start = time.perf_counter()
        text = request.context["text"]
        findings = self.find(text, request.context.get("source", "user"))
        return EvalResult(
            effect=Effect.DENY if findings else Effect.ALLOW,
            guardrail_name=self.name,
            reason=", ".join(sorted({f.kind for f in findings})) or None,
            eval_time_ms=(time.perf_counter() - start) * 1000,
            metadata={"findings": findings, "check": self},
        )

    def describe(self) -> Dict[str, Any]:
        return {"check": self.name, "kind": self.kind, "effect": self.effect,
                "direction": self.direction, "sources": list(self.sources),
                "on_error": self.on_error}


class PIICheck(Check):
    """PII and secrets: the native Sanitizer when importable plus the
    pure-Python detectors in :mod:`.detectors`.

    Args:
        kinds: Only these finding kinds (e.g. ``{"email", "phone_intl"}``);
            default all.
        native: Use the native Sanitizer when importable (default True).
        sanitizer: An explicit Sanitizer-like object (``.sanitize(text)``
            returning ``.redactions``) instead of the native one.
    """

    kind = "pii"
    can_redact = True

    def __init__(self, effect: str = "redact", *, kinds: Optional[Sequence[str]] = None,
                 native: bool = True, sanitizer: Any = None, **kw: Any) -> None:
        super().__init__(effect, **kw)
        self.kinds = frozenset(kinds) if kinds else None
        self.native = native
        self.sanitizer = sanitizer

    def find(self, text: str, source: str) -> List[Finding]:
        found = pii_findings(text, use_native=self.native, sanitizer=self.sanitizer)
        if self.kinds is not None:
            found = [f for f in found if f.kind in self.kinds]
        return found


Classifier = Callable[[str, str], Any]


class InjectionCheck(Check):
    """Prompt-injection heuristics (:data:`.detectors.INJECTION_PATTERNS`)
    on untrusted request text, plus an optional model-based ``classifier``.

    Heuristics are bypassable; a clean result is not evidence of safety.

    Args:
        sources: Default ``("tool", "retrieved")``: tool results and strings
            wrapped with :func:`retrieved`. Add ``"user"`` to check user turns.
        classifier: ``(text, source) -> bool | float | dict`` (a dict may hold
            ``score``/``injection`` and ``reason``). A float at or above
            ``threshold`` (or True) is a finding.
        heuristics: Run the built-in patterns (default True).
    """

    kind = "injection"

    def __init__(self, effect: str = "warn", *, sources: Sequence[str] = ("tool", "retrieved"),
                 classifier: Optional[Classifier] = None, threshold: float = 0.5,
                 heuristics: bool = True, **kw: Any) -> None:
        kw.setdefault("direction", "input")
        super().__init__(effect, sources=sources, **kw)
        self.classifier = classifier
        self.threshold = threshold
        self.heuristics = heuristics

    def find(self, text: str, source: str) -> List[Finding]:
        found = injection_findings(text) if self.heuristics else []
        if self.classifier is not None:
            verdict = self.classifier(text, source)
            if isinstance(verdict, dict):
                verdict = verdict.get("injection", verdict.get("score", False))
            hit = verdict >= self.threshold if isinstance(verdict, (int, float)) and not isinstance(
                verdict, bool) else bool(verdict)
            if hit:
                found.append(Finding("classifier", -1, -1, "classifier"))
        return found


class RegexCheck(Check):
    """A regular expression on text. ``must_match=False`` (default): a match
    is a finding (and can be redacted). ``must_match=True``: no match is a
    finding."""

    kind = "regex"

    def __init__(self, pattern: Union[str, "re.Pattern[str]"], effect: str = "block", *,
                 must_match: bool = False, **kw: Any) -> None:
        self.can_redact = not must_match
        kw.setdefault("direction", "output")
        super().__init__(effect, **kw)
        self.pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        self.must_match = must_match

    def find(self, text: str, source: str) -> List[Finding]:
        if self.must_match:
            return [] if self.pattern.search(text) else [Finding("no_match", -1, -1, self.name)]
        return [Finding("match", m.start(), m.end(), self.name)
                for m in self.pattern.finditer(text) if m.end() > m.start()]


class JSONSchemaCheck(Check):
    """The completion must be JSON valid against ``schema`` (needs the
    ``jsonschema`` package). A fenced ```json block is unwrapped first."""

    kind = "json_schema"

    def __init__(self, schema: Dict[str, Any], effect: str = "block", **kw: Any) -> None:
        try:
            import jsonschema  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("JSONSchemaCheck needs jsonschema: pip install jsonschema") from exc
        kw.setdefault("direction", "output")
        super().__init__(effect, **kw)
        jsonschema.validators.validator_for(schema).check_schema(schema)
        self.validator = jsonschema.validators.validator_for(schema)(schema)

    def find(self, text: str, source: str) -> List[Finding]:
        body = text.strip()
        fenced = re.match(r"^```(?:json)?\s*\n(.*)\n```$", body, re.DOTALL)
        if fenced:
            body = fenced.group(1)
        try:
            value = json.loads(body)
        except ValueError:
            return [Finding("not_json", -1, -1, self.name)]
        errors = list(self.validator.iter_errors(value))
        return [Finding("schema_violation", -1, -1, self.name)] if errors else []


class CallableCheck(Check):
    """``fn(text) -> bool | (bool, reason) | None``: False (or a reason
    string) is a finding; True / None passes."""

    kind = "callable"

    def __init__(self, fn: Callable[[str], Any], effect: str = "block", **kw: Any) -> None:
        kw.setdefault("direction", "output")
        kw.setdefault("name", getattr(fn, "__name__", "callable"))
        super().__init__(effect, **kw)
        self.fn = fn

    def find(self, text: str, source: str) -> List[Finding]:
        verdict = self.fn(text)
        if isinstance(verdict, tuple):
            verdict = verdict[0]
        if verdict is None or verdict is True:
            return []
        return [Finding("rejected", -1, -1, self.name)]


# -- the guard ---------------------------------------------------------------------------

@dataclass
class Decision:
    """The outcome of checking one or more texts."""

    effect: str = "allow"
    #: ``[{"check", "kind", "direction", "source", "count", "effect"}]``
    findings: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    #: ``(check, findings)`` for checks whose effect is redact.
    redactions: List[Finding] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def merge(self, other: "Decision") -> "Decision":
        self.effect = max(self.effect, other.effect, key=lambda e: _RANK[e])
        for item in other.findings:
            for mine in self.findings:
                keys = ("check", "kind", "direction", "source", "effect")
                if all(mine[k] == item[k] for k in keys):
                    mine["count"] += item["count"]
                    break
            else:
                self.findings.append(dict(item))
        self.errors += other.errors
        self.elapsed_ms += other.elapsed_ms
        return self

    def summary(self) -> str:
        if not self.findings and not self.errors:
            return "nothing found"
        parts = [f"{f['check']}:{f['kind']} x{f['count']} -> {f['effect']}" for f in self.findings]
        return "; ".join(parts + [f"error: {e}" for e in self.errors])


class ContentGuard:
    """Runs a list of checks over request and completion text.

    Args:
        checks: :class:`Check` objects.
        chunk_check_chars: For streams, re-check the completion every this
            many new characters (``block`` output checks only) and stop the
            stream at the first finding. 0 (default) checks only the final
            text. Each re-check scans the last ``4 * chunk_check_chars``
            characters, so the added time grows with stream length / interval.
        unredactable: When a ``redact`` effect fires where the text cannot be
            replaced without changing an SDK object's type (streams, raw
            responses, Bedrock InvokeModel, uninspectable request parts):
            ``"block"`` (default) or ``"warn"``.
    """

    def __init__(self, checks: Sequence[Check], *, chunk_check_chars: int = 0,
                 unredactable: str = "block") -> None:
        if unredactable not in ("block", "warn"):
            raise ValueError("unredactable must be 'block' or 'warn'")
        self.checks = list(checks)
        self.chunk_check_chars = int(chunk_check_chars)
        self.unredactable = unredactable

    def has(self, direction: str) -> bool:
        return any(c.direction in (direction, "both") for c in self.checks)

    def scan(self, text: str, direction: str = "input", source: str = "user") -> Decision:
        stages = [c for c in self.checks if c.applies(direction, source)]
        decision = Decision()
        if not stages or not text:
            return decision
        pipeline = GuardrailPipeline(stages, mode=PipelineMode.ALL, name="content",  # type: ignore[arg-type]
                                     on_error="deny")
        result = pipeline.evaluate(EvalRequest(
            agent="briefcase", action=direction, resource=source,
            context={"text": text, "source": source}))
        decision.elapsed_ms = result.eval_time_ms
        for stage, item in zip(stages, result.individual_results):
            if item.effect == Effect.ALLOW:
                continue
            if item.metadata.get("error"):
                message = f"{stage.name}: {item.metadata.get('exception_type')}"
                decision.errors.append(message)
                logger.warning("briefcase guard: check %s raised %s; on_error=%s", stage.name,
                               item.metadata.get("exception_type"), stage.on_error)
                if stage.on_error == "allow":
                    continue
                effect = stage.on_error
                decision.findings.append({"check": stage.name, "kind": "checker_error",
                                          "direction": direction, "source": source, "count": 1,
                                          "effect": effect})
                decision.effect = max(decision.effect, effect, key=lambda e: _RANK[e])
                continue
            findings: List[Finding] = item.metadata.get("findings") or []
            effect = stage.effect
            if effect == "redact" and not all(f.redactable for f in findings):
                effect = "block"
            by_kind: Dict[str, int] = {}
            for f in findings:
                by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
            for kind, count in sorted(by_kind.items()):
                decision.findings.append({"check": stage.name, "kind": kind,
                                          "direction": direction, "source": source,
                                          "count": count, "effect": effect})
            if effect == "redact":
                decision.redactions.extend(findings)
            decision.effect = max(decision.effect, effect, key=lambda e: _RANK[e])
        return decision

    def apply(self, text: str, decision: Decision) -> str:
        """The text with every redact finding replaced."""
        return redact(text, decision.redactions) if decision.redactions else text

    def check_text(self, text: str, direction: str = "input",
                   source: str = "user") -> Tuple[Decision, str]:
        """``(decision, redacted text)`` for one text, without any call."""
        decision = self.scan(text, direction, source)
        return decision, self.apply(text, decision)

    def describe(self) -> List[Dict[str, Any]]:
        return [c.describe() for c in self.checks]


__all__ = [
    "CallableCheck", "Check", "ContentGuard", "Decision", "EFFECTS", "InjectionCheck",
    "JSONSchemaCheck", "PIICheck", "RegexCheck",
]
