"""Compare a replayed output with the recorded one.

Levels, strongest first:

- ``exact``: the text is byte-for-byte equal and the tool calls are equal
  in their raw form (name and raw arguments, in order).
- ``normalized``: equal after Unicode NFKC, case folding and collapsing
  whitespace; tool calls equal structurally (same names, same parsed
  arguments, in order).
- ``json``: both texts parse as JSON (a surrounding Markdown code fence is
  allowed) and are equal as structures (key order ignored); tool calls
  equal structurally.
- ``semantic``: a user-supplied ``scorer(recorded_text, replay_text) ->
  float`` reached ``threshold``. No scorer is bundled.

A tool-call mismatch is a change at every level: text similarity never
excuses calling a different tool or passing different arguments.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional

Scorer = Callable[[str, str], float]

LEVELS = ("exact", "normalized", "json", "semantic")

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.S)
_SPACE = re.compile(r"\s+")


def normalize_text(text: Optional[str]) -> str:
    """NFKC, case-folded, whitespace collapsed and stripped."""
    if not text:
        return ""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


_NO_JSON = object()


def parse_json(text: Optional[str]) -> Any:
    """The JSON value in ``text`` (optionally fenced), or ``_NO_JSON``."""
    if not isinstance(text, str) or not text.strip():
        return _NO_JSON
    match = _FENCE.match(text)
    body = match.group(1) if match else text
    try:
        return json.loads(body)
    except ValueError:
        return _NO_JSON


def _tools_raw(calls: List[Dict[str, Any]]) -> List[Any]:
    return [(c.get("name"), json.dumps(c.get("raw"), sort_keys=False, default=str)
             if not isinstance(c.get("raw"), str) else c.get("raw")) for c in calls]


def _tools_struct(calls: List[Dict[str, Any]]) -> List[Any]:
    return [(c.get("name"), json.dumps(c.get("arguments"), sort_keys=True, default=str))
            for c in calls]


def render(output: Optional[Dict[str, Any]]) -> str:
    """Text used for the unified diff: the text, then one line per tool call."""
    if not output:
        return ""
    lines = []
    if output.get("text") is not None:
        lines.append(output["text"])
    for call in output.get("tool_calls") or []:
        args = json.dumps(call.get("arguments"), sort_keys=True, default=str)
        lines.append(f"[tool call] {call.get('name')}({args})")
    if output.get("refusal"):
        lines.append(f"[refusal] {output['refusal']}")
    return "\n".join(lines)


def unified_diff(recorded: Optional[Dict[str, Any]], replayed: Optional[Dict[str, Any]],
                 context: int = 3) -> str:
    a = render(recorded).splitlines()
    b = render(replayed).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "recorded", "replay", n=context, lineterm=""))


def compare(recorded: Dict[str, Any], replayed: Dict[str, Any], *,
            scorer: Optional[Scorer] = None, threshold: float = 0.9) -> Dict[str, Any]:
    """Compare two outputs (``{"text", "tool_calls"}``).

    Returns ``{"match_level", "checks", "score"}``: ``match_level`` is the
    strongest level that held, or ``None``; ``checks`` maps each level to
    ``True`` / ``False``, or ``None`` when it was not applicable (``json``
    when either side is not JSON, ``semantic`` without a scorer).
    """
    text_a, text_b = recorded.get("text"), replayed.get("text")
    tools_a, tools_b = recorded.get("tool_calls") or [], replayed.get("tool_calls") or []
    tools_same = _tools_struct(tools_a) == _tools_struct(tools_b)
    refusal_same = bool(recorded.get("refusal")) == bool(replayed.get("refusal"))

    checks: Dict[str, Optional[bool]] = {}
    checks["exact"] = (
        (text_a or "") == (text_b or "") and _tools_raw(tools_a) == _tools_raw(tools_b)
        and recorded.get("refusal") == replayed.get("refusal")
    )
    checks["normalized"] = (
        normalize_text(text_a) == normalize_text(text_b) and tools_same and refusal_same
    )
    json_a, json_b = parse_json(text_a), parse_json(text_b)
    if json_a is _NO_JSON or json_b is _NO_JSON:
        if not text_a and not text_b and (tools_a or tools_b):
            checks["json"] = tools_same and refusal_same
        else:
            checks["json"] = None
    else:
        checks["json"] = json_a == json_b and tools_same and refusal_same
    score: Optional[float] = None
    if scorer is not None:
        try:
            score = float(scorer(text_a or "", text_b or ""))
        except Exception as exc:  # a broken scorer is reported, never a match
            checks["semantic"] = None
            return {"match_level": _level(checks), "checks": checks, "score": None,
                    "scorer_error": f"{type(exc).__name__}: {exc}"}
        checks["semantic"] = score >= threshold and tools_same and refusal_same
    else:
        checks["semantic"] = None
    return {"match_level": _level(checks), "checks": checks, "score": score}


def _level(checks: Dict[str, Optional[bool]]) -> Optional[str]:
    for level in LEVELS:
        if checks.get(level):
            return level
    return None


__all__ = ["LEVELS", "Scorer", "compare", "normalize_text", "parse_json", "render", "unified_diff"]
