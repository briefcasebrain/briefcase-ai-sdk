"""Read-only queries over recorded decisions, for the ``briefcase-ai-mcp`` tools.

Kept free of the ``mcp`` dependency: :mod:`briefcase.mcp.server` registers a thin MCP tool
around each public function here, and the tests call them directly as well as through an
MCP client session.

Sources (all read-only; nothing here writes a file, changes config or calls a model):

- the receiver's records, ``$BRIEFCASE_HOME/receiver/records.jsonl`` (default
  ``~/.briefcase/receiver/records.jsonl``), written by ``briefcase-ai receive --detach``;
- ``BRIEFCASE_EXPORTER`` when it names a ``*.jsonl`` file (your app's own records);
- files listed in ``BRIEFCASE_MCP_RECORDS`` (``os.pathsep``-separated);
- or, per call, the ``path`` the caller passes (a ``.jsonl`` records file, a hash-chain
  ``.jsonl`` file, a bitemporal SQLite ``.db``/``.sqlite`` file, or a baseline ``.json``).

Paths are confined: after resolving symlinks a path must lie under the user's home directory
or ``$BRIEFCASE_HOME`` or, when ``BRIEFCASE_MCP_ROOTS`` is set, under one of the directories
it lists (``os.pathsep``-separated) and nowhere else. A path containing ``..`` is refused.

The platform's read API (``decisions.list/get/tree/aggregate``) is tRPC behind a browser
session, not the exporter token, so it is not queried; see ``platform_note``.

Prompt, response and tool content is returned only when a tool is called with
``include_content=True``, and then capped.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

ENV_ROOTS = "BRIEFCASE_MCP_ROOTS"
ENV_RECORDS = "BRIEFCASE_MCP_RECORDS"
ENV_PLATFORM_URL = "BRIEFCASE_PLATFORM_URL"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SCAN = 100_000           # records read per source per call
MAX_LINE_BYTES = 1_000_000   # longer lines are skipped, never parsed
DEFAULT_CONTENT_CHARS = 2_000
MAX_CONTENT_CHARS = 20_000
MAX_RESULT_CHARS = 60_000    # every tool result is trimmed to fit this
MAX_TREE_NODES = 500
RECORD_SUFFIXES = (".jsonl",)
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")

# Keys whose values are prompt, response or tool content (never returned without
# include_content). Everything under ``inputs``/``outputs`` that is not in SAFE_KEYS is
# treated as content too.
CONTENT_KEYS = frozenset({
    "prompt", "response", "result", "tool_input", "tool_output", "target", "unit", "attributes",
    "messages", "args", "kwargs", "content", "text", "query", "documents", "completion",
    "input", "output", "tool_parameters",
})
SAFE_KEYS = frozenset({
    "source", "tool_name", "tool_kind", "session_id", "prompt_id", "model", "decision",
    "decision_source", "decision_type", "tool_use_id", "hook_event_name", "effect", "would",
    "mode", "enforced", "rule_id", "rule_source", "reason", "ask_fallback", "success",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd",
    "duration_ms", "status_code", "error_type", "tool_error_type", "request_id", "event_sequence",
    "args_count", "kwargs_count", "result_type", "result_sha256", "result_chars", "chunk_count",
    "stream_completed", "target_hmac_sha256", "prompt_length", "response_length", "agent_name",
    "query_source", "speed", "effort", "attempt", "unparseable", "app_version", "terminal_type",
})


_BARE_NAME = re.compile(r"^[A-Za-z_][\w.]{0,120}$")


class ToolError(ValueError):
    """A bad argument or a refused path; the message is safe to show the caller."""


# -- paths ------------------------------------------------------------------------------------

def briefcase_home() -> Path:
    override = os.environ.get("BRIEFCASE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".briefcase"


def allowed_roots() -> List[Path]:
    raw = os.environ.get(ENV_ROOTS)
    if raw:
        roots = [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    else:
        roots = [Path.home(), briefcase_home()]
    out = []
    for root in roots:
        try:
            out.append(Path(os.path.realpath(root)))
        except OSError:
            continue
    return out


def _short(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    if home not in ("", "/") and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def resolve_path(raw: str, suffixes: Sequence[str] = RECORD_SUFFIXES, *, must_exist: bool = True) -> Path:
    """The caller's ``raw`` path, resolved and confined; ToolError when refused."""
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("path must be a non-empty string")
    if "\x00" in raw:
        raise ToolError("path contains a NUL byte")
    candidate = Path(raw.strip()).expanduser()
    if ".." in candidate.parts:
        raise ToolError("path may not contain '..'; pass the full path")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = Path(os.path.realpath(candidate))
    roots = allowed_roots()
    if not any(resolved == root or root in resolved.parents for root in roots):
        where = f"the directories in {ENV_ROOTS}" if os.environ.get(ENV_ROOTS) else \
            f"your home directory or $BRIEFCASE_HOME (or set {ENV_ROOTS})"
        raise ToolError(f"{raw!r} is outside {where}; refused")
    if suffixes and resolved.suffix.lower() not in suffixes:
        raise ToolError(f"{raw!r} must end in {' or '.join(suffixes)}")
    if must_exist and not resolved.is_file():
        raise ToolError(f"{_short(resolved)} does not exist or is not a file")
    return resolved


def default_sources() -> List[Path]:
    """Record files read when the caller names none: the receiver's, ``BRIEFCASE_EXPORTER``
    when it is a ``.jsonl`` file, and ``BRIEFCASE_MCP_RECORDS``. Only existing, confined
    files are returned."""
    candidates: List[str] = [str(briefcase_home() / "receiver" / "records.jsonl")]
    exporter = (os.environ.get("BRIEFCASE_EXPORTER") or "").strip()
    if exporter.endswith(".jsonl") and "://" not in exporter:
        candidates.append(exporter)
    candidates += [p for p in (os.environ.get(ENV_RECORDS) or "").split(os.pathsep) if p.strip()]
    out: List[Path] = []
    for raw in candidates:
        try:
            path = resolve_path(raw)
        except ToolError:
            continue
        if path not in out:
            out.append(path)
    return out


def platform_note() -> Optional[str]:
    """Why the platform is not queried, when ``BRIEFCASE_PLATFORM_URL`` is set. The token is
    never read or echoed here."""
    if not (os.environ.get(ENV_PLATFORM_URL) or "").strip():
        return None
    return ("BRIEFCASE_PLATFORM_URL is set, but the platform's read API (decisions.list, .get, "
            ".tree, .aggregate) is tRPC behind a signed-in browser session and does not accept "
            "the exporter token, so only local records are searched")


# -- reading ----------------------------------------------------------------------------------

@dataclass
class SourceStats:
    path: Path
    records: int = 0
    skipped_lines: int = 0
    not_records: int = 0
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": _short(self.path), "records": self.records}
        if self.skipped_lines:
            out["skipped_lines"] = self.skipped_lines
        if self.not_records:
            out["not_decision_records"] = self.not_records
        if self.truncated:
            out["truncated"] = f"stopped after {MAX_SCAN} records"
        return out


@dataclass
class Loaded:
    records: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[SourceStats] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def meta(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"sources": [s.to_dict() for s in self.sources]}
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _is_record(obj: Any) -> bool:
    return isinstance(obj, dict) and any(isinstance(obj.get(k), str) for k in
                                         ("decision_id", "decision_type", "function_name"))


def iter_file(path: Path, stats: SourceStats) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """``(line number, record)`` for each decision record in a JSONL file. A nested
    ``child_spans`` entry is yielded as its own record with ``_nested_in`` set."""
    with open(path, "rb") as fh:
        for number, raw in enumerate(fh, 1):
            if stats.records >= MAX_SCAN:
                stats.truncated = True
                return
            if len(raw) > MAX_LINE_BYTES:
                stats.skipped_lines += 1
                continue
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                stats.skipped_lines += 1
                continue
            if not _is_record(obj):
                stats.not_records += 1
                continue
            stats.records += 1
            obj["_line"] = number
            yield number, obj
            for child in obj.get("child_spans") or []:
                if _is_record(child):
                    child = dict(child, _line=number, _nested_in=obj.get("decision_id"))
                    yield number, child


def load(path: Optional[str] = None) -> Loaded:
    """Records from ``path`` (confined) or from :func:`default_sources`."""
    loaded = Loaded()
    paths = [resolve_path(path)] if path else default_sources()
    if not paths:
        loaded.notes.append(
            "no record files found: run `briefcase-ai receive --detach` (coding tools), set "
            "BRIEFCASE_EXPORTER=runs.jsonl in your app, or pass path=")
    for source in paths:
        stats = SourceStats(source)
        loaded.sources.append(stats)
        for _, record in iter_file(source, stats):
            record["_file"] = _short(source)
            loaded.records.append(record)
    note = platform_note()
    if note:
        loaded.notes.append(note)
    return loaded


# -- record fields ----------------------------------------------------------------------------

def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    return int(number) if number is not None else None


def when(record: Dict[str, Any]) -> Optional[datetime]:
    value = record.get("started_at") or record.get("ended_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def model_of(record: Dict[str, Any]) -> Optional[str]:
    for scope, key in ((_dict(record.get("model_parameters")), "model_name"), (record, "model"),
                       (_dict(record.get("inputs")), "model"), (_dict(record.get("model_parameters")), "model")):
        value = scope.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def tokens_of(record: Dict[str, Any]) -> Dict[str, Optional[int]]:
    usage = _dict(record.get("token_usage"))
    outputs = _dict(record.get("outputs"))
    tin = _int(usage.get("prompt_tokens"))
    tout = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens"))
    if tin is None:
        tin = _int(usage.get("input_tokens")) if usage else _int(outputs.get("input_tokens"))
    if tout is None:
        tout = _int(usage.get("output_tokens")) if usage else _int(outputs.get("output_tokens"))
    if total is None and (tin is not None or tout is not None):
        total = (tin or 0) + (tout or 0)
    return {"input": tin, "output": tout, "total": total}


def cost_of(record: Dict[str, Any]) -> Optional[float]:
    value = _num(record.get("cost_usd"))
    if value is not None:
        return value
    cost = record.get("cost")
    if isinstance(cost, dict):
        for key in ("total_usd", "usd", "total"):
            if _num(cost.get(key)) is not None:
                return _num(cost.get(key))
    elif _num(cost) is not None:
        return _num(cost)
    return _num(_dict(record.get("outputs")).get("cost_usd"))


def status_of(record: Dict[str, Any]) -> str:
    status = record.get("status")
    if isinstance(status, str) and status:
        return status
    if record.get("decision_type") == "policy.tool_call":
        effect = _dict(record.get("outputs")).get("effect")
        if isinstance(effect, str):
            return effect
    return "error" if record.get("error") else "ok"


def tool_of(record: Dict[str, Any]) -> Optional[str]:
    value = _dict(record.get("inputs")).get("tool_name")
    return value if isinstance(value, str) else None


def agent_of(record: Dict[str, Any]) -> Optional[str]:
    inputs = _dict(record.get("inputs"))
    for value in (record.get("agent"), inputs.get("agent_name"), inputs.get("source"), record.get("parent_framework")):
        if isinstance(value, str) and value:
            return value
    return None


def session_of(record: Dict[str, Any]) -> Optional[str]:
    for value in (record.get("session_id"), _dict(record.get("inputs")).get("session_id")):
        if isinstance(value, str) and value:
            return value
    return None


def _has_content(record: Dict[str, Any]) -> bool:
    for scope in (_dict(record.get("inputs")), _dict(record.get("outputs"))):
        for key, value in scope.items():
            if value in (None, "", [], {}):
                continue
            if key in CONTENT_KEYS or key not in SAFE_KEYS:
                return True
    error = record.get("error")
    # An error recorded as a bare exception name (capture_content="none") or a hash is not content.
    return (isinstance(error, str) and error not in ("", "[redacted]") and ":sha256:" not in error
            and not _BARE_NAME.match(error))


def row(record: Dict[str, Any]) -> Dict[str, Any]:
    """A compact, content-free summary of one record."""
    tokens = tokens_of(record)
    out: Dict[str, Any] = {
        "id": record.get("decision_id"),
        "type": record.get("decision_type"),
        "function": record.get("function_name"),
        "status": status_of(record),
        "started_at": record.get("started_at"),
        "duration_ms": _num(record.get("execution_time_ms")),
        "model": model_of(record),
        "tool": tool_of(record),
        "agent": agent_of(record),
        "session_id": session_of(record),
        "trace_id": record.get("trace_id"),
        "workflow_id": record.get("workflow_id"),
        "parent_run_id": record.get("parent_run_id"),
        "nested_in": record.get("_nested_in"),
        "prompt": (f"{record.get('prompt_name')}@{record.get('prompt_version')}"
                   if record.get("prompt_name") else None),
        "tokens": tokens["total"],
        "cost_usd": cost_of(record),
        "content_captured": _has_content(record) or None,
        "file": record.get("_file"),
        "line": record.get("_line"),
    }
    if record.get("decision_type") == "policy.tool_call":
        outputs = _dict(record.get("outputs"))
        out["rule_id"] = outputs.get("rule_id")
    return {key: value for key, value in out.items() if value is not None}


# -- filters and cursors ----------------------------------------------------------------------

def _parse_when(value: Optional[str], label: str) -> Optional[datetime]:
    if not value:
        return None
    from briefcase.drift.baseline import parse_when

    try:
        return parse_when(value)
    except ValueError:
        raise ToolError(f"{label} must be ISO-8601 (2026-11-01T00:00:00Z) or an age like 30m, 24h, 7d")


def _glob(value: Optional[str], pattern: Optional[str]) -> bool:
    if pattern is None or pattern == "":
        return True
    if value is None:
        return False
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(value.lower(), pattern.lower())
    return value.lower() == pattern.lower()


@dataclass
class Filters:
    since: Optional[str] = None
    until: Optional[str] = None
    decision_type: Optional[str] = None
    status: Optional[str] = None
    model: Optional[str] = None
    agent: Optional[str] = None
    tool: Optional[str] = None
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    workflow_id: Optional[str] = None
    prompt_name: Optional[str] = None
    prompt_version: Optional[str] = None
    text: Optional[str] = None

    def compile(self):
        lo = _parse_when(self.since, "since")
        hi = _parse_when(self.until, "until")
        needle = (self.text or "").lower().strip() or None

        def test(record: Dict[str, Any]) -> bool:
            if lo or hi:
                moment = when(record)
                if moment is None or (lo and moment < lo) or (hi and moment >= hi):
                    return False
            if not _glob(record.get("decision_type"), self.decision_type):
                return False
            if not _glob(status_of(record), self.status):
                return False
            if not _glob(model_of(record), self.model):
                return False
            if self.agent and not (_glob(agent_of(record), self.agent)
                                   or _glob(record.get("function_name"), self.agent + "*")):
                return False
            if not _glob(tool_of(record), self.tool):
                return False
            for value, wanted in ((session_of(record), self.session_id), (record.get("trace_id"), self.trace_id),
                                  (record.get("workflow_id"), self.workflow_id),
                                  (record.get("prompt_name"), self.prompt_name),
                                  (record.get("prompt_version"), self.prompt_version)):
                if wanted and value != wanted:
                    return False
            if needle:
                haystack = " ".join(str(v) for v in (record.get("decision_id"), record.get("function_name"),
                                                     tool_of(record), record.get("trace_id"),
                                                     record.get("workflow_id"), session_of(record)) if v)
                if needle not in haystack.lower():
                    return False
            return True
        return test


def _sort_key(record: Dict[str, Any]) -> Tuple[str, str]:
    return (str(record.get("started_at") or ""), str(record.get("decision_id") or ""))


def _encode_cursor(key: Tuple[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> Tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        started, ident = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return str(started), str(ident)
    except Exception:
        raise ToolError("cursor is not one this server returned; start again without it")


def _limit(value: Any, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolError(f"limit must be an integer from 1 to {cap}")
    return min(value, cap)


def fit(result: Dict[str, Any], list_key: str) -> Dict[str, Any]:
    """Drop items from ``result[list_key]`` until the JSON fits ``MAX_RESULT_CHARS``."""
    items = result.get(list_key)
    if not isinstance(items, list):
        return result
    dropped = 0
    while items and len(json.dumps(result, default=str)) > MAX_RESULT_CHARS:
        items.pop()
        dropped += 1
    if dropped:
        result["trimmed"] = f"{dropped} item(s) left out to keep the reply under {MAX_RESULT_CHARS} characters"
    return result


# -- tools ------------------------------------------------------------------------------------

def search_decisions(path: Optional[str] = None, limit: Optional[int] = None, cursor: Optional[str] = None,
                     **filters: Any) -> Dict[str, Any]:
    """Newest-first compact rows matching ``filters`` (see :class:`Filters`)."""
    size = _limit(limit)
    unknown = set(filters) - set(Filters.__dataclass_fields__)
    if unknown:
        raise ToolError(f"unknown filter(s): {', '.join(sorted(unknown))}")
    test = Filters(**filters).compile()
    loaded = load(path)
    matched = sorted((r for r in loaded.records if test(r)), key=_sort_key, reverse=True)
    if cursor:
        after = _decode_cursor(cursor)
        matched = [r for r in matched if _sort_key(r) < after]
    page = matched[:size]
    more = len(matched) > size
    result: Dict[str, Any] = {
        "items": [row(r) for r in page],
        "count": len(page),
        "matched": len(matched),
        "next_cursor": _encode_cursor(_sort_key(page[-1])) if more and page else None,
    }
    result.update(loaded.meta())
    return fit(result, "items")


def _find(loaded: Loaded, decision_id: str) -> Optional[Dict[str, Any]]:
    for record in loaded.records:
        if record.get("decision_id") == decision_id:
            return record
    return None


def _cap_content(value: Any, chars: int) -> Tuple[Any, bool]:
    """``value`` with every string cut to ``chars`` and the whole JSON to ``4 * chars``."""
    cut = [False]

    def walk(item: Any, depth: int = 0) -> Any:
        if depth > 8:
            cut[0] = True
            return "[nested too deep]"
        if isinstance(item, str):
            if len(item) > chars:
                cut[0] = True
                return item[:chars] + f"... [{len(item) - chars} more characters]"
            return item
        if isinstance(item, dict):
            return {str(k): walk(v, depth + 1) for k, v in list(item.items())[:200]}
        if isinstance(item, list):
            if len(item) > 200:
                cut[0] = True
            return [walk(v, depth + 1) for v in item[:200]]
        return item

    capped = walk(value)
    text = json.dumps(capped, default=str)
    if len(text) > 4 * chars:
        cut[0] = True
        capped = text[:4 * chars] + "... [truncated]"
    return capped, cut[0]


def _metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in record.items():
        if key in ("inputs", "outputs", "error", "child_spans") or key.startswith("_"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif key in ("model_parameters", "token_usage", "tags", "capture_limits"):
            out[key] = value
    for side in ("inputs", "outputs"):
        safe = {k: v for k, v in _dict(record.get(side)).items()
                if k in SAFE_KEYS and k not in CONTENT_KEYS and isinstance(v, (str, int, float, bool))}
        if safe:
            out[f"{side}_metadata"] = safe
        keys = sorted(_dict(record.get(side)))
        if keys:
            out[f"{side}_keys"] = keys
    if record.get("error"):
        out["has_error"] = True
    return out


def get_decision(decision_id: str, path: Optional[str] = None, include_content: bool = False,
                 content_chars: Optional[int] = None) -> Dict[str, Any]:
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ToolError("decision_id is required")
    chars = _limit(content_chars, DEFAULT_CONTENT_CHARS, MAX_CONTENT_CHARS) if content_chars else DEFAULT_CONTENT_CHARS
    loaded = load(path)
    record = _find(loaded, decision_id.strip())
    if record is None:
        result: Dict[str, Any] = {"found": False, "decision_id": decision_id,
                                  "hint": "use search_decisions to find ids"}
        result.update(loaded.meta())
        return result
    captured = _has_content(record)
    result = {"found": True, "summary": row(record), "metadata": _metadata(record),
              "content_captured": captured}
    if not captured:
        result["content"] = None
        result["content_note"] = ("no prompt, response or tool content was captured for this record "
                                  "(only metadata); nothing more to show")
    elif include_content:
        content = {key: record.get(key) for key in ("inputs", "outputs", "error") if record.get(key)}
        result["content"], cut = _cap_content(content, chars)
        result["content_note"] = (f"strings cut to {chars} characters" if cut else "complete") + \
            "; this is recorded content and may be sensitive"
    else:
        result["content"] = None
        result["content_note"] = ("content was captured; call again with include_content=true to see it "
                                  f"(capped at {chars} characters per string)")
    result.update(loaded.meta())
    return result


# -- traces -----------------------------------------------------------------------------------

_FRAMEWORK_LLM_TYPES = ("llm", "chat_model", "llm_call", "llm_run")


def _is_provider_record(record: Dict[str, Any]) -> bool:
    return bool(record.get("parent_framework")) or bool(_dict(record.get("model_parameters")).get("provider"))


def duplicate_pairs(records: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """``{framework run id: provider record id}`` for model calls recorded twice: a provider
    record whose ``parent_run_id`` names a framework LLM run. Count tokens and cost once (the
    provider record's)."""
    by_id = {r.get("decision_id"): r for r in records if r.get("decision_id")}
    pairs: Dict[str, str] = {}
    for record in by_id.values():
        parent = by_id.get(record.get("parent_run_id"))
        if parent is None or not _is_provider_record(record):
            continue
        if str(parent.get("decision_type") or "").lower() in _FRAMEWORK_LLM_TYPES:
            pairs[str(parent["decision_id"])] = str(record["decision_id"])
    return pairs


def _pricing() -> Any:
    try:
        from briefcase import pricing

        return pricing.load()
    except Exception:  # noqa: BLE001 - a broken pricing file means "unpriced", not a crash
        return None


def price(record: Dict[str, Any], table: Any) -> Tuple[Optional[float], str]:
    """``(cost, how)``: how is ``recorded``, ``estimated``, or ``unpriced: why``."""
    recorded = cost_of(record)
    if recorded is not None:
        return recorded, "recorded"
    tokens = tokens_of(record)
    model = model_of(record)
    if model is None or tokens["input"] is None or tokens["output"] is None:
        return None, "unpriced: no model or token counts"
    if table is None:
        return None, "unpriced: pricing table unavailable"
    try:
        return table.estimate(model, tokens["input"], tokens["output"])["total_cost"], "estimated"
    except Exception as exc:  # UnknownModel, NeedsNative, ValueError
        name = type(exc).__name__
        return None, f"unpriced: {'model not in the pricing table' if name == 'UnknownModel' else str(exc) or name}"


def get_trace(id: str, kind: str = "auto", path: Optional[str] = None,
              max_nodes: Optional[int] = None) -> Dict[str, Any]:
    if not isinstance(id, str) or not id.strip():
        raise ToolError("id is required")
    if kind not in ("auto", "trace", "session", "workflow", "decision"):
        raise ToolError("kind must be auto, trace, session, workflow or decision")
    cap = _limit(max_nodes, MAX_TREE_NODES, MAX_TREE_NODES)
    loaded = load(path)
    ident = id.strip()
    keys = {"trace": lambda r: r.get("trace_id"), "session": session_of, "workflow": lambda r: r.get("workflow_id")}
    chosen = kind
    members: List[Dict[str, Any]] = []
    for candidate in ([kind] if kind != "auto" else ["trace", "session", "workflow", "decision"]):
        if candidate == "decision":
            members = _descendants(loaded.records, ident)
        else:
            members = [r for r in loaded.records if keys[candidate](r) == ident]
        if members:
            chosen = candidate
            break
    if not members:
        result: Dict[str, Any] = {"found": False, "id": ident, "kind": kind,
                                  "hint": "pass a trace_id, session_id, workflow_id or decision id from "
                                          "search_decisions"}
        result.update(loaded.meta())
        return result
    members = _with_children(loaded.records, members)
    members.sort(key=_sort_key)
    truncated = len(members) > cap
    members = members[:cap]
    pairs = duplicate_pairs(members)
    table = _pricing()
    starts: List[datetime] = [m for m in (when(r) for r in members) if m is not None]
    origin = min(starts) if starts else None
    nodes: Dict[str, Dict[str, Any]] = {}
    totals = {"decisions": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
              "tokens_unknown": 0, "cost_usd": 0.0, "cost_estimated_usd": 0.0, "unpriced": 0,
              "deduplicated_model_calls": len(pairs)}
    for record in members:
        node = row(record)
        for key in ("file", "line", "trace_id", "session_id", "workflow_id"):
            node.pop(key, None)
        moment = when(record)
        if origin and moment:
            node["offset_ms"] = round((moment - origin).total_seconds() * 1000, 3)
        tokens = tokens_of(record)
        node["tokens"] = {k: v for k, v in tokens.items() if v is not None} or None
        rid = str(record.get("decision_id"))
        totals["decisions"] += 1
        if node["status"] in ("error", "cancelled", "deny"):
            totals["errors"] += 1
        if rid in pairs:
            node["counted_in"] = pairs[rid]
            node["note"] = "same model call as its provider child; counted once, there"
        else:
            if tokens["total"] is None:
                if model_of(record):  # a model call that did not report its tokens
                    totals["tokens_unknown"] += 1
            else:
                totals["input_tokens"] += tokens["input"] or 0
                totals["output_tokens"] += tokens["output"] or 0
                totals["total_tokens"] += tokens["total"]
            cost, how = price(record, table)
            if cost is not None:
                node["cost_usd"] = round(cost, 8)
                node["cost_basis"] = how
                totals["cost_usd" if how == "recorded" else "cost_estimated_usd"] += cost
            elif model_of(record):
                totals["unpriced"] += 1
                node["cost_basis"] = how
        node["children"] = []
        nodes[rid] = node
    roots: List[Dict[str, Any]] = []
    for record in members:
        rid = str(record.get("decision_id"))
        parent_id = record.get("parent_run_id") or record.get("_nested_in")
        parent = nodes.get(str(parent_id)) if parent_id else None
        if parent is not None and parent is not nodes[rid]:
            parent["children"].append(nodes[rid])
        else:
            if parent_id:
                nodes[rid]["parent_missing"] = True
            roots.append(nodes[rid])
    duration = None
    if origin:
        ends: List[datetime] = [m for m in (_ended(r) for r in members) if m is not None]
        last = max(ends) if ends else max(starts)
        duration = round((last - origin).total_seconds() * 1000, 3)
    totals["cost_usd"] = round(totals["cost_usd"], 8)
    totals["cost_estimated_usd"] = round(totals["cost_estimated_usd"], 8)
    result = {"found": True, "kind": chosen, "id": ident, "started_at": origin.isoformat() if origin else None,
              "duration_ms": duration, "totals": totals, "truncated": truncated, "roots": roots}
    if totals["cost_estimated_usd"]:
        result["cost_note"] = ("cost_estimated_usd comes from briefcase.pricing (packaged rates are "
                               "unverified; `briefcase-ai pricing check`), not from the records")
    result.update(loaded.meta())
    while len(json.dumps(result, default=str)) > MAX_RESULT_CHARS and len(result["roots"]) > 1:
        result["roots"] = result["roots"][: len(result["roots"]) // 2]
        result["trimmed"] = ("later root nodes left out to keep the reply under "
                             f"{MAX_RESULT_CHARS} characters; totals cover every node; pass max_nodes "
                             "or a narrower id")
    return result


def _ended(record: Dict[str, Any]) -> Optional[datetime]:
    value = record.get("ended_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _descendants(records: List[Dict[str, Any]], root_id: str) -> List[Dict[str, Any]]:
    root = next((r for r in records if r.get("decision_id") == root_id), None)
    if root is None:
        return []
    family: Set[Any] = {root_id}
    out = [root]
    changed = True
    while changed:
        changed = False
        for record in records:
            rid = record.get("decision_id")
            parent = record.get("parent_run_id") or record.get("_nested_in")
            if rid not in family and parent in family:
                family.add(rid)
                out.append(record)
                changed = True
    return out


def _with_children(records: List[Dict[str, Any]], members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add records whose parent is a member (provider calls under a framework run, nested
    spans), even when they lack the trace/session/workflow id."""
    ids = {r.get("decision_id") for r in members}
    out = list(members)
    changed = True
    while changed:
        changed = False
        for record in records:
            parent = record.get("parent_run_id") or record.get("_nested_in")
            if record.get("decision_id") not in ids and parent in ids:
                ids.add(record.get("decision_id"))
                out.append(record)
                changed = True
    return out


# -- usage ------------------------------------------------------------------------------------

def summarize_usage(path: Optional[str] = None, group_by: str = "model", since: Optional[str] = None,
                    until: Optional[str] = None, limit: Optional[int] = None, **filters: Any) -> Dict[str, Any]:
    if group_by not in ("model", "tool", "day", "type", "agent"):
        raise ToolError("group_by must be model, tool, day, type or agent")
    size = _limit(limit)
    test = Filters(since=since, until=until, **filters).compile()
    loaded = load(path)
    records = [r for r in loaded.records if test(r)]
    pairs = duplicate_pairs(records)
    table = _pricing()

    def key(record: Dict[str, Any]) -> str:
        if group_by == "model":
            return model_of(record) or "(no model)"
        if group_by == "tool":
            return tool_of(record) or "(not a tool call)"
        if group_by == "day":
            moment = when(record)
            return moment.date().isoformat() if moment else "(no time)"
        if group_by == "type":
            return str(record.get("decision_type") or "(no type)")
        return agent_of(record) or "(unknown)"

    buckets: Dict[str, Dict[str, Any]] = {}
    unpriced_models: Dict[str, str] = {}
    totals = {"count": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
              "cost_recorded_usd": 0.0, "cost_estimated_usd": 0.0, "priced": 0, "estimated": 0, "unpriced": 0}
    for record in records:
        bucket = buckets.setdefault(key(record), {k: (0.0 if "usd" in k else 0) for k in totals})
        for target in (bucket, totals):
            target["count"] += 1
            if status_of(record) in ("error", "cancelled"):
                target["errors"] += 1
        if str(record.get("decision_id")) in pairs:
            continue  # the provider record of the same call is counted instead
        tokens = tokens_of(record)
        cost, how = price(record, table)
        for target in (bucket, totals):
            target["input_tokens"] += tokens["input"] or 0
            target["output_tokens"] += tokens["output"] or 0
            target["total_tokens"] += tokens["total"] or 0
            if how == "recorded":
                target["cost_recorded_usd"] += cost or 0.0
                target["priced"] += 1
            elif how == "estimated":
                target["cost_estimated_usd"] += cost or 0.0
                target["estimated"] += 1
            elif model_of(record):
                target["unpriced"] += 1
        if how.startswith("unpriced") and model_of(record):
            unpriced_models.setdefault(model_of(record) or "", how[len("unpriced: "):])
    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    if group_by == "day":
        ordered = sorted(buckets.items())
    rows = []
    for name, data in ordered[:size]:
        data = dict(data, key=name)
        data["cost_recorded_usd"] = round(data["cost_recorded_usd"], 8)
        data["cost_estimated_usd"] = round(data["cost_estimated_usd"], 8)
        data["error_rate"] = round(data["errors"] / data["count"], 4) if data["count"] else 0.0
        rows.append(data)
    totals["cost_recorded_usd"] = round(totals["cost_recorded_usd"], 8)
    totals["cost_estimated_usd"] = round(totals["cost_estimated_usd"], 8)
    result: Dict[str, Any] = {
        "group_by": group_by, "buckets": rows, "bucket_count": len(buckets), "totals": totals,
        "unpriced_models": unpriced_models, "deduplicated_model_calls": len(pairs),
        "cost_note": ("cost_recorded_usd is what the records carry; cost_estimated_usd is computed "
                      "here from token counts with briefcase.pricing (packaged rates are unverified "
                      "unless `briefcase-ai pricing check` says otherwise); unpriced calls are counted "
                      "in `unpriced` and add nothing"),
    }
    result.update(loaded.meta())
    return fit(result, "buckets")


# -- policy -----------------------------------------------------------------------------------

def explain_policy_decision(decision_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    loaded = load(path)
    record = _find(loaded, decision_id)
    if record is None:
        result: Dict[str, Any] = {"found": False, "decision_id": decision_id,
                                  "hint": "search_decisions(decision_type='policy.tool_call') lists them"}
        result.update(loaded.meta())
        return result
    if record.get("decision_type") != "policy.tool_call":
        raise ToolError(f"{decision_id} is a {record.get('decision_type')!r} record, not a policy decision "
                        "(decision_type 'policy.tool_call')")
    inputs, outputs = _dict(record.get("inputs")), _dict(record.get("outputs"))
    effect, would, mode = outputs.get("effect"), outputs.get("would"), outputs.get("mode")
    rule_id, source, reason = outputs.get("rule_id"), outputs.get("rule_source"), outputs.get("reason")
    tool, kind = inputs.get("source"), inputs.get("tool_kind")
    verdict = {"allow": "allowed", "ask": "asked the user about", "deny": "denied"}.get(str(effect), str(effect))
    sentence = f"{tool or 'The tool'}'s {kind or 'tool'} call ({inputs.get('tool_name') or '?'}) was {verdict}"
    if rule_id:
        sentence += f" by rule {rule_id!r}" + (f" from the {source} policy" if source else "")
    else:
        sentence += " because no rule matched (the default for this kind)"
    if reason:
        sentence += f": {str(reason).rstrip('.')}"
    if mode == "observe" and would and would != effect:
        sentence += f". The rules said {would!r}, but the policy was in observe mode, so it was only logged"
    if outputs.get("ask_fallback"):
        sentence += f". Where the tool cannot ask, it falls back to {outputs['ask_fallback']!r}"
    result = {
        "found": True, "decision_id": decision_id, "explanation": sentence + ".",
        "recorded": {k: v for k, v in {
            "effect": effect, "would": would, "mode": mode, "enforced": outputs.get("enforced"),
            "rule_id": rule_id, "rule_source": source, "reason": reason, "tool": tool, "kind": kind,
            "tool_name": inputs.get("tool_name"), "session_id": inputs.get("session_id"),
            "target_captured": "target" in inputs, "at": record.get("started_at"),
            "hook_error": bool(record.get("error")),
        }.items() if v is not None},
        "current_policy": _current_rule(rule_id, inputs, kind),
    }
    result.update(loaded.meta())
    return result


def _current_rule(rule_id: Optional[str], inputs: Dict[str, Any], kind: Optional[str]) -> Dict[str, Any]:
    """The rule as the policy files on this machine define it today (read-only)."""
    try:
        from briefcase.policy import model as policy_model
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "why": f"briefcase.policy could not be imported ({type(exc).__name__})"}
    loaded = policy_model.load()
    if loaded.policy is None:
        why = "; ".join(f"{src}: {msg}" for src, _, msg in loaded.errors) or "no policy file on this machine"
        return {"available": False, "why": why, "fix": "briefcase-ai policy init"}
    policy = loaded.policy
    out: Dict[str, Any] = {"available": True, "mode": policy.settings.get("mode"),
                           "preset": policy.settings.get("preset")}
    if rule_id:
        rules = list(policy.all_rules()) + list(getattr(policy, "protect", []) or [])
        rule = next((r for r in rules if r.id == rule_id), None)
        if rule is None:
            out["rule"] = None
            out["rule_note"] = f"rule {rule_id!r} is no longer in the policy"
        else:
            out["rule"] = {"id": rule.id, "effect": rule.effect, "source": rule.source,
                           "matches": rule.describe(), "reason": rule.reason or None}
    target = inputs.get("target")
    if isinstance(target, str) and kind in ("shell", "read", "write", "web"):
        try:
            from briefcase.policy.engine import ToolCall, evaluate

            call = ToolCall(kind, command=target if kind == "shell" else None,
                            paths=[target] if kind in ("read", "write") else (),
                            url=target if kind == "web" else None)
            decision = evaluate(policy, call)
            out["today"] = {"effect": decision.effect, "would": decision.would, "rule_id": decision.rule_id,
                            "reason": decision.reason or None}
        except Exception as exc:  # noqa: BLE001
            out["today"] = {"error": f"could not re-evaluate ({type(exc).__name__})"}
    else:
        out["today_note"] = ("the call's target was not captured (only its keyed hash), so it cannot be "
                             "re-evaluated against today's policy")
    return out


# -- integrity --------------------------------------------------------------------------------

def verify_records(path: str, records_path: Optional[str] = None, max_problems: int = 20) -> Dict[str, Any]:
    """Verify a hash-chain file (or a bitemporal SQLite store) and say plainly what was found."""
    suffix = Path(path).suffix.lower()
    if suffix in SQLITE_SUFFIXES:
        return _verify_sqlite(resolve_path(path, SQLITE_SUFFIXES))
    chain_file = resolve_path(path)
    from briefcase.integrity.chain import GENESIS_PRIOR_HASH, compute_entry_hash, verify_chain_segment
    from briefcase.integrity.stores import _entry_from_dict

    entries = []
    problems: List[Dict[str, Any]] = []
    decision_lines = 0
    with open(chain_file, "rb") as fh:
        for number, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                problems.append({"line": number, "problem": "not valid JSON (truncated or edited)"})
                continue
            if isinstance(obj, dict) and "prior_hash" in obj and "payload_hash" in obj:
                try:
                    entries.append((number, _entry_from_dict(obj)))
                except (KeyError, ValueError, TypeError):
                    problems.append({"line": number, "problem": "chain entry is missing fields"})
            elif _is_record(obj):
                decision_lines += 1
    if not entries:
        verdict = ("this file holds decision records, not hash-chain entries: records on their own are "
                   "not tamper-evident. Pass the chain file (written by HashChainAppender with "
                   "JsonlHashChainStore) as path, and this records file as records_path"
                   if decision_lines else "no hash-chain entries found")
        return {"ok": False, "verdict": verdict, "entries": 0, "decision_records": decision_lines,
                "problems": problems[:max_problems], "path": _short(chain_file)}
    segments: Dict[Tuple[str, Optional[str]], List[Tuple[int, Any]]] = {}
    for item in entries:
        segments.setdefault((item[1].table, item[1].entity_id), []).append(item)
    for (table, entity), items in segments.items():
        ok, failing = verify_chain_segment([e for _, e in items])
        if ok:
            continue
        expected = GENESIS_PRIOR_HASH
        for position, (line, entry) in enumerate(items):
            if entry.row_id == failing:
                recomputed = compute_entry_hash(entry.row_id, entry.table, entry.entity_id, entry.observed_at,
                                                entry.recorded_at, entry.payload_hash, entry.supersedes,
                                                entry.prior_hash)
                if entry.prior_hash != expected:
                    what = ("its prior_hash does not match the entry before it: an entry was removed, "
                            "inserted or reordered here")
                elif recomputed != entry.hash:
                    what = "its hash does not match its own fields: this entry was edited after it was written"
                else:  # pragma: no cover - verify_chain_segment and this loop agree
                    what = "failed verification"
                problems.append({"line": line, "row_id": entry.row_id,
                                 "segment": table if entity is None else f"{table}/{entity}",
                                 "position": position, "problem": what})
                break
            expected = entry.hash
    payload = _check_payloads(entries, records_path, problems) if records_path else None
    signed = sum(1 for _, e in entries if e.signature)
    ok = not problems
    verdict = (f"intact: {len(entries)} entries in {len(segments)} chain segment(s) link up and every "
               "entry hash matches its fields" if ok else
               f"NOT intact: {len(problems)} problem(s); the first is at line {problems[0].get('line')}")
    result: Dict[str, Any] = {"ok": ok, "verdict": verdict, "path": _short(chain_file), "entries": len(entries),
                              "segments": len(segments), "problems": problems[:max_problems]}
    if payload is not None:
        result["payloads"] = payload
    if signed:
        result["signatures"] = (f"{signed} entr{'y' if signed == 1 else 'ies'} carry an Ed25519 signature; "
                                "they are not checked here (no public key is configured)")
    result["checked"] = ("chain linkage and entry hashes" + (" and record payload hashes" if payload else "")
                         + "; this proves the chain file is self-consistent, not who wrote it")
    return result


def _check_payloads(entries: List[Tuple[int, Any]], records_path: str,
                    problems: List[Dict[str, Any]]) -> Dict[str, Any]:
    from briefcase.integrity.chain import compute_payload_hash

    source = resolve_path(records_path)
    stats = SourceStats(source)
    by_id: Dict[Any, Dict[str, Any]] = {}
    for _, record in iter_file(source, stats):
        record.pop("_line", None)
        if record.get("_nested_in"):
            continue
        by_id[record.get("decision_id")] = record
    matched = mismatched = 0
    for line, entry in entries:
        chained_record = by_id.get(entry.row_id)
        if chained_record is None:
            continue
        matched += 1
        if compute_payload_hash(chained_record) != entry.payload_hash:
            mismatched += 1
            problems.append({"line": line, "row_id": entry.row_id,
                             "problem": "the record's payload hash differs from the chain: the record was "
                                        "edited after it was chained"})
    chained = {e.row_id for _, e in entries}
    return {"records_path": _short(source), "records": len(by_id), "checked": matched,
            "mismatched": mismatched, "records_not_in_chain": len([i for i in by_id if i not in chained])}


def _verify_sqlite(path: Path) -> Dict[str, Any]:
    import sqlite3

    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ToolError(f"cannot open {_short(path)} read-only: {exc}")
    try:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "bitemporal_records" not in tables:
            return {"ok": False, "path": _short(path),
                    "verdict": "no hash chain or content hashes in this database (no bitemporal_records "
                               "table); the native SqliteBackend stores snapshots without a chain"}
        from briefcase.bitemporal.backends.sqlite import _row_to_record

        problems = []
        count = 0
        for r in conn.execute("SELECT * FROM bitemporal_records ORDER BY transaction_time, record_id"):
            count += 1
            if _row_to_record(r).content_hash() != r["content_hash"]:
                problems.append({"record_id": r["record_id"], "key": r["key"],
                                 "problem": "stored value does not match its content_hash (edited)"})
    finally:
        conn.close()
    ok = not problems
    return {"ok": ok, "path": _short(path), "records": count, "problems": problems[:20],
            "verdict": (f"{count} rows; every value matches its content hash" if ok
                        else f"NOT intact: {len(problems)} row(s) were edited"),
            "checked": "per-row content hashes of a bitemporal SQLite store; rows have no chain, so a "
                       "deleted row is not detectable here (deletes are blocked by a trigger)"}


# -- replay and drift -------------------------------------------------------------------------

class _PlanOnly:
    """An executor that is never run: it accepts every call so the plan shows what a live
    replay would send. The MCP tools only plan."""

    name = "plan-only (MCP never replays)"
    live = True

    def check(self, call: Any, overrides: Any) -> List[str]:
        return []

    def execute(self, call: Any, overrides: Any) -> Any:  # pragma: no cover - never called
        raise RuntimeError("the MCP server does not execute replays")


def replay_plan(path: str, decision_ids: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    from briefcase.replay.recorded import load_records, make_plan

    source = resolve_path(path)
    size = _limit(limit)
    stats = SourceStats(source)
    records = []
    for _, record in iter_file(source, stats):
        if record.get("_nested_in"):
            continue
        record.pop("_line", None)
        if decision_ids and record.get("decision_id") not in set(decision_ids):
            continue
        records.append(record)
    calls = load_records(records)
    plan = make_plan(calls, _PlanOnly(), limit=size)
    summary = plan.summary()
    summary.pop("executor", None)
    summary.pop("live", None)
    per_call = [call.summary() for call in calls[:size]]
    result = {
        "plan": summary,
        "calls": per_call,
        "missing_ids": sorted(set(decision_ids or []) - {r.get("decision_id") for r in records}),
        "note": ("plan only: nothing was replayed and no model was called. To run it yourself: "
                 f"`briefcase-ai replay run {_short(source)}` (it asks before spending money)"),
        "sources": [stats.to_dict()],
    }
    return fit(result, "calls")


def drift_check(baseline: str, path: Optional[str] = None, since: Optional[str] = None,
                until: Optional[str] = None, last: Optional[int] = None) -> Dict[str, Any]:
    from briefcase.drift import baseline as drift

    if baseline.endswith(".json"):
        profile = drift.load_profile(resolve_path(baseline, (".json",)))
    else:
        try:
            drift.check_name(baseline)
        except ValueError as exc:
            raise ToolError(str(exc))
        if not drift.profile_path(baseline).is_file():
            names = [p["name"] for p in drift.list_profiles()]
            raise ToolError(f"no saved baseline {baseline!r}; saved: {', '.join(names) or 'none'} "
                            "(create one with `briefcase-ai drift baseline save NAME --from FILE`)")
        profile = drift.load_profile(baseline)
    loaded = load(path)
    window = drift.select_records([{k: v for k, v in r.items() if not k.startswith("_")}
                                   for r in loaded.records if not r.get("_nested_in")],
                                  since=since, until=until, last=_limit(last, 10_000, MAX_SCAN) if last else None)
    report = drift.check(profile, window)
    signals = [{k: s.get(k) for k in ("name", "state", "reading", "value", "threshold") if k in s}
               for s in report.get("signals", [])]
    states: Dict[str, int] = {}
    for signal in signals:
        states[signal.get("state", "?")] = states.get(signal.get("state", "?"), 0) + 1
    result: Dict[str, Any] = {
        "baseline": profile.get("name"), "baseline_records": profile.get("n"), "window_records": len(window),
        "summary": states, "attention": [s for s in signals if s.get("state") == "attention"],
        "signals": signals,
    }
    result.update(loaded.meta())
    return fit(result, "signals")


__all__ = [
    "ToolError", "allowed_roots", "default_sources", "drift_check", "explain_policy_decision",
    "get_decision", "get_trace", "replay_plan", "resolve_path", "search_decisions", "summarize_usage",
    "verify_records",
]
