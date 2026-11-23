"""Content guardrails on provider calls: PII and secrets in prompts and
completions, prompt-injection heuristics on tool and retrieved content, and
policies on the completion (regex, JSON schema, callable).

::

    import briefcase
    from briefcase.guardrails import content

    briefcase.auto()                      # instrument the provider SDKs
    content.guard(
        content.pii(input="redact", output="warn"),
        content.injection(effect="block"),
        content.output_regex(r"(?i)internal use only", effect="block"),
    )

    docs = content.retrieved(search(query))   # mark RAG text as untrusted
    client.chat.completions.create(model="gpt-4o", messages=[...docs...])

Effects:

- ``block``: an input check raises :class:`GuardrailBlocked` before the
  request is sent. An output check raises after the response was received:
  **that call was already made and paid for**; the exception carries the
  record fields.
- ``redact``: input text is replaced in the request before it is sent
  (containers are copied, the caller's objects are untouched); output text
  is replaced on the returned SDK object in place, so its type does not
  change. Where that is not possible (streams, ``with_raw_response``,
  Bedrock InvokeModel, request parts that are not plain text) the effect
  becomes ``block`` (``ContentGuard(unredactable="warn")`` to let it pass).
- ``warn``: the call goes through; a WARNING is logged (kinds and counts,
  never the text) and the record says what was found.

A check that raises fails closed when its effect is ``block`` (configurable
per check with ``on_error``).

Streams: input checks run before the request; output checks run on the
final text when the stream is read to its end (the block then raises from
the last ``next()``, after every chunk was delivered). ``chunk_check_chars``
re-checks while streaming and stops the stream at the first finding.

Every call's record gets ``guard_effect`` (``allow``/``warn``/``redact``/
``block``) and ``guard_findings`` (check, kind, direction, source, count,
effect; never the matched text), plus ``guard_limits`` when something could
not be checked.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from briefcase._logging import get_logger
from briefcase.guardrails.content.checks import (
    CallableCheck,
    Check,
    ContentGuard,
    Decision,
    InjectionCheck,
    JSONSchemaCheck,
    PIICheck,
    RegexCheck,
)
from briefcase.guardrails.content.detectors import native_sanitizer
from briefcase.integrations.providers._base import CallBlocked, add_call_hook, remove_call_hook
from briefcase.integrations.providers._requests import RetrievedText

logger = get_logger("briefcase.guardrails.content")

ENV_FILE = "BRIEFCASE_GUARD_FILE"
STATE = "guard"


class GuardrailBlocked(CallBlocked):
    """A content check stopped a call.

    ``direction``: ``"input"`` (nothing was sent) or ``"output"`` (the call
    was made and is billed; the completion was withheld). ``findings``: the
    record's ``guard_findings``. ``paid``: True for output blocks.
    ``partial``: True when a stream was stopped early by ``chunk_check_chars``.
    """

    record_effect = "block"

    def __init__(self, message: str, *, direction: str, findings: List[Dict[str, Any]],
                 partial: bool = False) -> None:
        super().__init__(message)
        self.direction = direction
        self.findings = findings
        self.paid = direction == "output"
        self.partial = partial


def retrieved(text: str) -> RetrievedText:
    """Mark text as retrieved (a RAG chunk, a web page, a file): injection
    checks treat it as untrusted wherever it is placed in a request. The
    result is a ``str`` and is sent unchanged."""
    return RetrievedText(text)


# -- constructors ------------------------------------------------------------------------

def pii(input: str = "redact", output: str = "warn", **options: Any) -> List[Check]:  # noqa: A002
    """PII/secret checks for the request (``input``) and the completion
    (``output``); ``"off"`` disables a side. Options: see :class:`PIICheck`."""
    checks: List[Check] = []
    if input != "off":
        checks.append(PIICheck(input, direction="input", name="pii", **options))
    if output != "off":
        checks.append(PIICheck(output, direction="output", name="pii", **options))
    return checks


def injection(effect: str = "warn", **options: Any) -> InjectionCheck:
    """Prompt-injection heuristics (+ optional ``classifier=``); see
    :class:`InjectionCheck`."""
    return InjectionCheck(effect, **options)


def output_regex(pattern: str, effect: str = "block", **options: Any) -> RegexCheck:
    return RegexCheck(pattern, effect, **options)


def output_json_schema(schema: Dict[str, Any], effect: str = "block",
                       **options: Any) -> JSONSchemaCheck:
    return JSONSchemaCheck(schema, effect, **options)


def output_check(fn: Callable[[str], Any], effect: str = "block", **options: Any) -> CallableCheck:
    return CallableCheck(fn, effect, **options)


def default_checks() -> List[Check]:
    """``pii(input="redact", output="warn")`` and ``injection("warn")``."""
    return [*pii(), injection()]


# -- the hook -----------------------------------------------------------------------------

def _fields(ctx: Any, decision: Decision) -> None:
    state = ctx.state.setdefault(STATE, Decision())
    state.merge(decision)
    ctx.fields["guard_effect"] = state.effect
    ctx.fields["guard_findings"] = [dict(f) for f in state.findings]


def _limit(ctx: Any, note: str) -> None:
    limits = ctx.fields.setdefault("guard_limits", [])
    if note not in limits:
        limits.append(note)


def _escalate(decision: Decision, guard: ContentGuard) -> None:
    """A redact that cannot be applied: block, or warn when configured."""
    to = guard.unredactable
    for item in decision.findings:
        if item["effect"] == "redact":
            item["effect"] = to
    decision.redactions = []
    if decision.effect == "redact":
        decision.effect = to


def _warn(decision: Decision, where: str) -> None:
    logger.warning("briefcase-ai guard: %s: %s", where, decision.summary())


class GuardHook:
    """Runs a :class:`ContentGuard` on every instrumented provider call."""

    def __init__(self, guard: ContentGuard) -> None:
        self.guard = guard

    def before(self, ctx: Any) -> None:
        guard = self.guard
        decision = Decision()
        if guard.has("input"):
            walk = ctx.spec.inspect(ctx.kwargs)
            if walk.uninspected:
                _limit(ctx, "request parts not checked: " + ", ".join(sorted(set(walk.uninspected))))
            per_text: Dict[int, Decision] = {}
            for index, (source, text) in enumerate(walk.texts):
                found = guard.scan(text, "input", source)
                per_text[index] = found
                decision.merge(found)
            if decision.effect == "redact":
                cursor = iter(range(len(walk.texts)))

                def edit(text: str, source: str) -> Optional[str]:
                    found = per_text.get(next(cursor))
                    return guard.apply(text, found) if found and found.redactions else None

                if walk.uninspected or not ctx.rewrite_request(edit):
                    _limit(ctx, "request could not be rewritten in place")
                    _escalate(decision, guard)
        _fields(ctx, decision)
        if decision.effect == "block":
            raise GuardrailBlocked(
                f"content guard blocked the request before it was sent: {decision.summary()}",
                direction="input", findings=ctx.fields["guard_findings"])
        if decision.effect in ("warn",) or (decision.effect == "redact" and any(
                f["effect"] == "warn" for f in decision.findings)):
            _warn(decision, f"{ctx.provider} request")
        if guard.has("output"):
            if not ctx.spec.output_checks:
                _limit(ctx, f"output not checked: {ctx.provider} {ctx.operation} returns its "
                            "body to the application unread")
                return
            ctx.wants_text = True
            if ctx.stream and guard.chunk_check_chars > 0:
                ctx.chunk_check = self._chunk_checker(ctx)

    def _chunk_checker(self, ctx: Any) -> Callable[[str], None]:
        guard = self.guard
        step = guard.chunk_check_chars
        blockers = [c for c in guard.checks if c.effect == "block"
                    and c.direction in ("output", "both")]
        early = ContentGuard(blockers)
        seen = [0]

        def check(text: str) -> None:
            if len(text) - seen[0] < step:
                return
            seen[0] = len(text)
            window = text[-4 * step:]
            found = early.scan(window, "output", "assistant")
            if found.effect == "block":
                _fields(ctx, found)
                raise GuardrailBlocked(
                    f"content guard stopped the stream: {found.summary()} (the call is billed "
                    "for what was generated)", direction="output",
                    findings=ctx.fields["guard_findings"], partial=True)

        return check

    def after(self, ctx: Any, outcome: Any) -> None:
        guard = self.guard
        if not guard.has("output") or not ctx.spec.output_checks:
            return
        if not outcome.completed:
            if outcome.sent and outcome.error is None:
                _limit(ctx, "output not checked: the response was not read to its end")
            return
        text = outcome.text
        if text is None:
            _limit(ctx, "output not checked: no completion text in the response")
            return
        decision = guard.scan(text, "output", "assistant")
        if decision.effect == "redact":
            if ctx.response_redactable():
                redacted = guard.apply(text, decision)
                outcome.set_text(redacted)

                def rewrite(value: str) -> str:
                    return guard.apply(value, guard.scan(value, "output", "assistant"))

                ctx.output_rewrite = rewrite
            else:
                _limit(ctx, "completion could not be redacted in place")
                _escalate(decision, guard)
        _fields(ctx, decision)
        if decision.effect == "block":
            outcome.set_text("")
            raise GuardrailBlocked(
                f"content guard blocked the completion: {decision.summary()}. The call was "
                "already made and is billed.", direction="output",
                findings=ctx.fields["guard_findings"])
        if decision.effect == "warn" or any(f["effect"] == "warn" for f in decision.findings):
            _warn(decision, f"{ctx.provider} completion")


# -- configuration ------------------------------------------------------------------------

_lock = threading.Lock()
_guard: Optional[ContentGuard] = None
_hook: Optional[GuardHook] = None


def guard(*checks: Union[Check, Sequence[Check]], chunk_check_chars: int = 0,
          unredactable: str = "block") -> ContentGuard:
    """Enforce these checks on every instrumented provider call (replacing
    any earlier guard). With no checks: :func:`default_checks`."""
    flat: List[Check] = []
    for item in checks:
        if isinstance(item, Check):
            flat.append(item)
        else:
            flat.extend(item)
    return install(ContentGuard(flat or default_checks(), chunk_check_chars=chunk_check_chars,
                                unredactable=unredactable))


def install(content_guard: ContentGuard) -> ContentGuard:
    global _guard, _hook
    with _lock:
        if _hook is not None:
            remove_call_hook(_hook)
        _guard, _hook = content_guard, GuardHook(content_guard)
        add_call_hook(_hook)
    return content_guard


def disable() -> None:
    global _guard, _hook
    with _lock:
        if _hook is not None:
            remove_call_hook(_hook)
        _guard, _hook = None, None


def active() -> Optional[ContentGuard]:
    return _guard


def config_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_FILE):
        return Path(env[ENV_FILE]).expanduser()
    home = env.get("BRIEFCASE_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".briefcase") / "guard.toml"


def load(path: Optional[Union[str, Path]] = None) -> Optional[ContentGuard]:
    """A guard from ``guard.toml`` (``path``, ``$BRIEFCASE_GUARD_FILE`` or
    ``~/.briefcase/guard.toml``); None when the file does not exist.

    ::

        [pii]
        input = "redact"      # block | redact | warn | off
        output = "warn"

        [injection]
        effect = "warn"
        sources = ["tool", "retrieved"]

        [[output]]
        regex = "(?i)internal use only"
        effect = "block"

        [stream]
        chunk_check_chars = 0
    """
    import tomllib

    target = Path(path) if path is not None else config_path()
    if not target.exists():
        return None
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{target}: {exc}") from exc
    unknown = set(data) - {"pii", "injection", "output", "stream", "unredactable"}
    if unknown:
        raise ValueError(f"{target}: unknown sections {sorted(unknown)}")
    checks: List[Check] = []
    try:
        section = data.get("pii", {})
        if section.get("enabled", True):
            checks += pii(section.get("input", "redact"), section.get("output", "warn"),
                          **{k: v for k, v in section.items() if k in ("kinds",)})
        section = data.get("injection", {})
        if section.get("enabled", True):
            checks.append(injection(section.get("effect", "warn"),
                                    **{k: v for k, v in section.items() if k == "sources"}))
        for rule in data.get("output", []):
            effect = rule.get("effect", "block")
            if "regex" in rule:
                checks.append(output_regex(rule["regex"], effect,
                                           must_match=bool(rule.get("must_match", False)),
                                           name=rule.get("name")))
            elif "json_schema" in rule:
                import json

                checks.append(output_json_schema(json.loads(rule["json_schema"]), effect,
                                                 name=rule.get("name")))
            else:
                raise ValueError("an [[output]] rule needs regex or json_schema")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{target}: {exc}") from exc
    stream = data.get("stream", {})
    return ContentGuard(checks, chunk_check_chars=int(stream.get("chunk_check_chars", 0)),
                        unredactable=data.get("unredactable", "block"))


def enable(path: Optional[Union[str, Path]] = None) -> ContentGuard:
    """Enforce ``guard.toml`` (see :func:`load`), or :func:`default_checks`
    when there is no file."""
    loaded = load(path)
    return install(loaded if loaded is not None else ContentGuard(default_checks()))


def check_text(text: str, *, direction: str = "input", source: str = "user",
               checks: Optional[Sequence[Check]] = None) -> Dict[str, Any]:
    """Run checks on one text without a call (what ``briefcase-ai guard test``
    does). Returns ``{"effect", "findings", "errors", "redacted",
    "elapsed_ms"}``."""
    use = ContentGuard(list(checks)) if checks is not None else (_guard or ContentGuard(
        default_checks()))
    decision, redacted = use.check_text(text, direction, source)
    return {"effect": decision.effect, "findings": decision.findings, "errors": decision.errors,
            "redacted": redacted, "elapsed_ms": decision.elapsed_ms}


def native_available() -> bool:
    return native_sanitizer() is not None


__all__ = [
    "CallableCheck", "Check", "ContentGuard", "Decision", "GuardHook", "GuardrailBlocked",
    "InjectionCheck", "JSONSchemaCheck", "PIICheck", "RegexCheck", "RetrievedText", "active",
    "check_text", "config_path", "default_checks", "disable", "enable", "guard", "injection",
    "install", "load", "native_available", "output_check", "output_json_schema", "output_regex",
    "pii", "retrieved",
]
