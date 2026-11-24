"""Key-level config edits for ``briefcase-ai connect``: plan, preview, write, undo.

The guarantees every config writer (connect, mcp install, policy init and off, --undo, --managed) relies on, and
the one function each is enforced in:

* **I1 nothing we do not own changes.** A file is only ever extended by inserted text and shrunk by deleting what
  we wrote (:mod:`briefcase.connect.jsontext` edits JSON as text; TOML gets an appended block). Every write goes
  through :func:`write_checked`: a new file is created exclusively, an existing one is replaced only while it
  still holds exactly what the preview saw. Undo removes a value only when it is still equal, type for type
  (:func:`typed_eq`), to what was written.
* **I2 nothing read from an existing file is shown.** :func:`render_diff` prints only our own lines.
* **I3 nothing is exported that was not asked for.** :func:`exposure` names existing capture or credential keys
  that would combine with our endpoint; :mod:`briefcase.connect.engine` refuses such a write.
* **I4 ownership is exact** (:mod:`briefcase.connect.mcpreg` for registries, journal records for files).
* **I5 results are true** (:mod:`briefcase.connect.engine` stops a plan with a failing target).
* **I6 a hash is over parsed structure** (:func:`content_hash`): our TOML tables by name, our JSON key paths.
* **I7 credentials are private**: new files are 0600.
Every write is preceded by a timestamped backup and recorded in a journal so ``--undo`` can remove exactly what
was written, even after the user has edited the file again.
"""

from __future__ import annotations

import copy
import difflib
import importlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from briefcase.connect import jsontext


def _load_toml_module() -> Any:
    """``tomllib`` (Python 3.11+), else ``tomli`` when installed, else ``None``."""
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


_toml: Any = _load_toml_module()

TOML_BEGIN = "# >>> briefcase >>>"
TOML_END = "# <<< briefcase <<<"
JOURNAL_VERSION = 3  # 3: TOML ownership is hashed over parsed tables, not the marked text

CHANGE, NOOP, REFUSED = "change", "noop", "refused"

# Locations no edit may ever target: managed/system configuration belongs to an administrator, and
# connect only prints a snippet for it. Both the symlinked and the resolved spelling are listed (macOS:
# /etc is /private/etc). Ordinary project directories (/opt/app, /var/tmp/app, /usr/local/...) are not here.
_SYSTEM_PREFIXES = (
    "/etc", "/private/etc", "/System", "/Library", "/usr", "/bin", "/sbin",
    "/var/db", "/var/root", "/private/var/db", "/private/var/root",
    "c:\\programdata", "c:\\windows", "c:\\program files",
)
_SYSTEM_EXCEPTIONS = ("/usr/local",)

_SECRET_LINE = re.compile(
    r"""^(?P<head>[-+ ]?\s*"?[\w.\-]*(?:key|token|secret|password|passwd|auth|credential|headers)[\w.\-]*"?"""
    r"""\s*[:=]\s*)(?P<value>.+?)(?P<comma>,?)\s*$""",
    re.IGNORECASE,
)


class EditRefused(Exception):
    """The file cannot be edited safely; the caller prints a snippet instead."""


def home() -> Path:
    return Path.home()


def briefcase_home() -> Path:
    override = os.environ.get("BRIEFCASE_HOME")
    return Path(override).expanduser() if override else home() / ".briefcase"


def journal_path() -> Path:
    return briefcase_home() / "connect" / "journal.json"


# --- plan items ---------------------------------------------------------------

@dataclass
class FileEdit:
    """A key-level edit of one config file.

    For ``format="json"``, ``ops`` is a list of ``{"op", "path", "value"}``
    dicts where ``op`` is ``set`` (assign), ``default`` (assign only when the
    key is absent) or ``append`` (add ``value`` to a list unless present; the
    ``owned_by`` substring marks an item as ours when looking for one already in the file).
    For ``format="toml"``, ``toml_body`` is the text between the markers
    and ``toml_tables`` the dotted table names it defines.
    """

    tool: str
    path: Path
    format: str
    purpose: str
    ops: List[Dict[str, Any]] = field(default_factory=list)
    toml_body: str = ""
    toml_tables: Tuple[str, ...] = ()
    notes: List[str] = field(default_factory=list)
    # What the edit is for: "telemetry" (also hooks that forward events) or "mcp".
    kind: str = "telemetry"
    # Existing keys of the file that change what our write exports: ``(path, "capture" | "credential")``. A
    # truthy capture key or a present credential key would combine with our endpoint to send prompts or an
    # existing credential somewhere new, so the write is refused unless this invocation asked for that.
    watch: List[Tuple[List[str], str]] = field(default_factory=list)
    # The tool's whole telemetry namespace in this file, as ``(object path, key prefix)`` (prefix ``""`` = every key,
    # matched case-insensitively). Any existing key in it that this edit does not itself write blocks the write:
    # only what we can account for is allowed to combine with our endpoint.
    namespace: List[Tuple[List[str], str]] = field(default_factory=list)
    # The edit writes a credential the user supplied: the file ends up 0600, existing files included.
    secret: bool = False
    # Other files the tool also reads settings from (managed, project, local layers): existing telemetry settings
    # there combine with ours exactly like ones in this file.
    layers: List[Any] = field(default_factory=list)  # a Path, or an :class:`ExternalLayer` (registry, plist)
    toml_added_newline: bool = False  # set by resolve: the file had no final newline and we added one


@dataclass
class Command:
    """A subprocess to run after confirmation. Always an argument list."""

    tool: str
    argv: List[str]
    purpose: str
    undo_hint: Optional[List[str]] = None


@dataclass
class Manual:
    """Something only the user (or their admin) can do; printed, never run."""

    tool: str
    title: str
    text: str
    reason: str = ""


@dataclass
class Resolved:
    """A FileEdit evaluated against the file as it is right now."""

    edit: FileEdit
    status: str
    target: Path
    old_text: str = ""
    new_text: str = ""
    applied: List[Dict[str, Any]] = field(default_factory=list)
    existed: bool = False
    reason: str = ""
    warnings: List[str] = field(default_factory=list)
    # Why this target is not written: "foreign" (present, not ours), "modified" (ours once, changed since),
    # "differs" (ours and intact, but asked for other settings), or "" (nothing blocks it).
    blocker: str = ""


# --- path safety --------------------------------------------------------------

def _under(text: str, prefix: str) -> bool:
    lowered, base = text.lower(), prefix.lower().rstrip("/\\")
    return lowered == base or lowered.startswith(base + os.sep)


def is_system_path(path: Path) -> bool:
    """Under one of the listed system locations (see ``_SYSTEM_PREFIXES``)."""
    text = str(path)
    if any(_under(text, exception) for exception in _SYSTEM_EXCEPTIONS):
        return False
    return any(_under(text, prefix) for prefix in _SYSTEM_PREFIXES)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _target_for(path: Path) -> Tuple[Path, List[str]]:
    """The file an edit of ``path`` really writes, plus warnings about it.

    A symlinked config (dotfile managers do this) is written through to its
    target, and the plan says so whenever the target is outside the home
    directory. A target in a system location is refused outright.
    """
    warnings: List[str] = []
    target = path
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise EditRefused(f"cannot resolve {path}: {exc}")
    real_home = home().resolve()
    try:
        linked = path.is_symlink() or any(p.is_symlink() for p in path.parents if _inside(p, home()))
    except OSError as exc:
        raise EditRefused(f"cannot inspect {path}: {exc}")
    if linked and resolved != path:
        target = resolved
        if not _inside(resolved, real_home):
            warnings.append(
                f"{path} is a symlink to {resolved}, which is outside your home directory; "
                "the edit is written to that file"
            )
        else:
            warnings.append(f"{path} is a symlink to {resolved}")
    # Checked on the resolved path; only the user's own home is exempt.
    if is_system_path(resolved) and not _inside(resolved, real_home):
        raise EditRefused(f"{resolved} is a managed/system path; briefcase never writes there")
    return target, warnings


# --- JSON ---------------------------------------------------------------------

def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas (outside strings).

    Used only to tell "JSON with comments" apart from "broken JSON" so the
    refusal message is accurate; the stripped text is never written.
    """
    out: List[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _detect_indent(text: str) -> Any:
    match = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    if not match:
        return 2
    lead = match.group(1)
    return "\t" if lead.startswith("\t") else len(lead)


def read_text(path: Path) -> str:
    """The file's text with its line endings exactly as stored (``Path.read_text`` would turn CRLF into LF and a
    write-back would then change every line)."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _load_json(text: str, path: Path) -> Dict[str, Any]:
    if not text.strip():
        return {}
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        if str(exc) == "duplicate key":
            raise EditRefused(f"{path} has the same key twice in one object; readers disagree on which one wins, "
                              "so it is not edited. Fix it, or add the snippet by hand")
        try:
            json.loads(_strip_jsonc(text))
        except ValueError:
            raise EditRefused(f"{path} is not valid JSON ({exc}); fix it or add the snippet by hand")
        raise EditRefused(
            f"{path} contains comments or trailing commas (JSONC); rewriting it would "
            "lose them, so add the snippet by hand"
        )
    if not isinstance(doc, dict):
        raise EditRefused(f"{path} does not hold a JSON object at the top level")
    return doc


def _dump_json(doc: Dict[str, Any], like: str) -> str:
    text = json.dumps(doc, indent=_detect_indent(like), ensure_ascii=False)
    return text + "\n"


def _descend(doc: Dict[str, Any], keys: Sequence[str], create: bool,
             created: Optional[List[List[str]]] = None) -> Optional[Dict[str, Any]]:
    node: Any = doc
    walked: List[str] = []
    for key in keys:
        walked.append(key)
        if key not in node:
            if not create:
                return None
            node[key] = {}
            if created is not None:
                created.append(list(walked))
        node = node[key]
        if not isinstance(node, dict):
            if create:
                raise EditRefused(f"expected an object at {'.'.join(walked)!r}, found {type(node).__name__}")
            return None
    return node


def typed_eq(a: Any, b: Any) -> bool:
    """JSON equality that keeps types apart: ``true`` is not ``1`` and ``1`` is not ``1.0``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(typed_eq(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(typed_eq(x, y) for x, y in zip(a, b))
    return bool(a == b)


def _index_of(items: Sequence[Any], value: Any) -> int:
    return next((i for i, item in enumerate(items) if typed_eq(item, value)), -1)


def _apply_json_ops(doc: Dict[str, Any], ops: Sequence[Dict[str, Any]], tool: str) -> List[Dict[str, Any]]:
    """Apply ``ops`` to ``doc`` in place; return journal entries for what changed."""
    applied: List[Dict[str, Any]] = []
    for op in ops:
        kind, path, value = op["op"], list(op["path"]), op["value"]
        created: List[List[str]] = []
        parent = _descend(doc, path[:-1], create=True, created=created)
        assert parent is not None
        key = path[-1]
        if kind in ("set", "default"):
            if key in parent:
                if kind == "default" or typed_eq(parent[key], value):
                    continue
                raise EditRefused(f"{'.'.join(path)!r} already holds another value; it is never overwritten")
            entry = {"op": "set", "tool": tool, "path": path, "value": value, "created": created}
            if kind == "default":
                entry["default"] = True
            parent[key] = copy.deepcopy(value)
            applied.append(entry)
        elif kind == "append":
            if key not in parent:
                parent[key] = []
                created.append(path)
            items = parent[key]
            if not isinstance(items, list):
                raise EditRefused(f"expected a list at {'.'.join(path)!r}, found {type(items).__name__}")
            if _index_of(items, value) >= 0:
                continue
            entry = {"op": "append", "tool": tool, "path": path, "value": value, "created": created}
            if op.get("owned_by"):
                entry["owned_by"] = op["owned_by"]
            items.append(copy.deepcopy(value))
            applied.append(entry)
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown op {kind!r}")
    return applied


def json_snippet(ops: Sequence[Dict[str, Any]]) -> str:
    """The minimal JSON document that the ops would produce on an empty file."""
    doc: Dict[str, Any] = {}
    _apply_json_ops(doc, ops, tool="")
    return json.dumps(doc, indent=2, ensure_ascii=False)


# --- TOML ---------------------------------------------------------------------

def toml_block(body: str) -> str:
    return (
        f"{TOML_BEGIN}\n"
        "# Managed by `briefcase-ai connect`; remove with `briefcase-ai connect --undo`.\n"
        f"{body.rstrip()}\n"
        f"{TOML_END}\n"
    )


_BLOCK_RE = re.compile(
    r"^" + re.escape(TOML_BEGIN) + r"[^\n]*\n.*?^" + re.escape(TOML_END) + r"[^\n]*\n?",
    re.MULTILINE | re.DOTALL,
)
_TOML_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _toml_found(text: str, tables: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The values of the named tables as the file defines them (wherever they sit), or ``None`` when the file is
    not valid TOML. A table that is not there is left out."""
    if _toml is None:
        return None
    try:
        doc = _toml.loads(text)
    except Exception:
        return None
    found: Dict[str, Any] = {}
    for table in tables:
        node: Any = doc
        for part in table.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = _ABSENT
                break
        if node is not _ABSENT:
            found[table] = node
    return found


def _toml_hash(text: str, tables: Sequence[str]) -> Optional[str]:
    """A hash of what the file says our tables are: parsed, so spacing, comments, key order and the trailing
    newline do not matter, and anything the tool added elsewhere (even between our markers) is not ours."""
    found = _toml_found(text, tables)
    return _sha(found) if found else None


def _resolve_toml(edit: FileEdit, old: str) -> Tuple[str, str]:
    if _toml is None:
        raise EditRefused("no TOML parser is available on this Python (needs 3.11+ or `pip install tomli`)")
    found = _toml_found(old, edit.toml_tables) if old.strip() else {}
    if found is None:
        raise EditRefused(f"{edit.path} is not valid TOML; fix it or add the snippet by hand")
    if found:
        raise EditRefused(f"{edit.path} already defines [{next(iter(found))}]; it is never rewritten")
    block = toml_block(edit.toml_body)
    nl = "\r\n" if "\r\n" in old else "\n"
    if nl != "\n":
        block = block.replace("\n", nl)
    if old.strip():
        lead = old if old.endswith("\n") else old + nl
        edit.toml_added_newline = not old.endswith("\n")
        new = lead + ("" if lead.endswith(nl + nl) else nl) + block  # only ever adds bytes
    else:
        new = old + block
    try:
        _toml.loads(new)
    except Exception as exc:  # pragma: no cover - our own block is malformed
        raise EditRefused(f"refusing to write invalid TOML to {edit.path}: {exc}")
    return new, block


def _toml_remove(old: str, tables: Sequence[str], added_newline: bool = False) -> str:
    """``old`` without the named tables (and their sub-tables), and without our marker lines when nothing else is
    left between them. Everything else, including tables a tool put inside our markers, stays byte for byte. A
    ``[name]`` line inside a multi-line string is not a header. The caller checks the result parses back to exactly
    ``old`` minus those tables."""
    lines = old.splitlines(keepends=True)
    keep: List[str] = []
    skipping = False
    inside: Optional[str] = None  # the delimiter of the multi-line string we are in
    for line in lines:
        stripped = line.strip()
        if inside is None and stripped.startswith((TOML_BEGIN, TOML_END)):
            skipping = False
            keep.append(line)
            continue
        header = _TOML_HEADER.match(line) if inside is None else None
        if header:
            name = ".".join(part.strip().strip("\"'") for part in header.group(1).split("."))
            skipping = any(name == t or name.startswith(t + ".") for t in tables)
        if not skipping:
            keep.append(line)
        for delimiter in ('"""', "'''"):
            count = line.count(delimiter)
            if inside == delimiter and count % 2 == 1:
                inside = None
            elif inside is None and count % 2 == 1:
                inside = delimiter
    text = "".join(keep)
    out = text
    for match in reversed(list(_BLOCK_RE.finditer(text))):
        inner = match.group(0).splitlines()[1:-1]
        if all(not ln.strip() or ln.strip().startswith("#") for ln in inner):
            start = match.start()
            before = out[:start]
            for nl in ("\r\n", "\n"):
                if before.endswith(nl + nl):
                    start -= len(nl)
                    break
            out = out[:start] + out[match.end():]
    if added_newline and out.endswith("\r\n"):
        out = out[:-2]
    elif added_newline and out.endswith("\n"):
        out = out[:-1]
    return out


def _without_tables(doc: Dict[str, Any], tables: Sequence[str]) -> Dict[str, Any]:
    import copy as _copy

    out = _copy.deepcopy(doc)
    for table in tables:
        parts = table.split(".")
        node: Any = out
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(parts[-1], None)
            # an emptied parent that only existed for our table goes with it
            if len(parts) > 1 and not node:
                parent: Any = out
                for part in parts[:-2]:
                    parent = parent.get(part) if isinstance(parent, dict) else None
                if isinstance(parent, dict) and parent.get(parts[-2]) == {}:
                    parent.pop(parts[-2], None)
    return out


# --- resolve / render ---------------------------------------------------------

def snippet(edit: FileEdit) -> str:
    if edit.format == "toml":
        return toml_block(edit.toml_body).rstrip("\n")
    return json_snippet(edit.ops)


def resolve(edit: FileEdit) -> Resolved:
    """Evaluate ``edit`` against the current file without touching it."""
    try:
        target, warnings = _target_for(edit.path)
    except EditRefused as exc:
        return Resolved(edit, REFUSED, edit.path, reason=str(exc))
    existed = target.exists()
    old = ""
    try:
        if existed:
            old = read_text(target)
        if edit.format == "toml":
            new, block = _resolve_toml(edit, old)
            applied = [{"op": "toml_block", "tool": edit.tool, "tables": list(edit.toml_tables),
                        "hash": content_hash("toml", [], new, edit.toml_tables),
                        "added_newline": edit.toml_added_newline,
                        "lines": sorted({ln.strip() for ln in block.splitlines() if ln.strip()})}]
            if new == old:
                applied = []
        else:
            doc = _load_json(old, edit.path)
            applied = _apply_json_ops(doc, edit.ops, edit.tool)
            if not applied:
                new = old
            elif old.strip():
                new = _json_apply_text(old, applied)
            else:
                new = _dump_json(doc, old)
    except EditRefused as exc:
        return Resolved(edit, REFUSED, target, old_text=old, existed=existed,
                        reason=str(exc), warnings=warnings)
    except (OSError, UnicodeDecodeError) as exc:
        return Resolved(edit, REFUSED, target, existed=existed,
                        reason=f"cannot read {target}: {exc}", warnings=warnings)
    status = CHANGE if applied and new != old else NOOP
    return Resolved(edit, status, target, old_text=old, new_text=new, applied=applied,
                    existed=existed, warnings=warnings)


def _json_apply_text(old: str, applied: Sequence[Dict[str, Any]]) -> str:
    """``old`` with each applied op inserted as text: every byte already in the file stays where it is."""
    text = old
    nl = "\r\n" if "\r\n" in old else "\n"
    for entry in applied:
        root = jsontext.parse(text)
        path = list(entry["path"])
        value = entry["value"]
        if entry["op"] == "append":
            arr = jsontext.find(root, path)
            if arr is not None and arr.kind == "array":
                text = jsontext.append_item(text, arr, value, nl)
                continue
            value = [value]
        depth = len(path) - 1
        while depth > 0:
            node = jsontext.find(root, path[:depth])
            if node is not None and node.kind == "object":
                break
            depth -= 1
        parent = jsontext.find(root, path[:depth]) if depth else root
        assert parent is not None
        missing = path[depth:]
        for key in reversed(missing[1:]):
            value = {key: value}
        text = jsontext.insert_member(text, parent, missing[0], value, nl)
    return text


_INLINE_HEADERS = re.compile(r"(headers\s*=\s*)\{[^{}]*[^{}\s][^{}]*\}")


def mask_secrets(line: str) -> str:
    line = _INLINE_HEADERS.sub(r"\1{ *** }", line)
    match = _SECRET_LINE.match(line)
    if not match or match.group("value").strip() in ("{", "[", "{}", "[]"):
        return line
    return f"{match.group('head')}\"***\"{match.group('comma')}"


# Credentials we were never told about, by shape: a bearer token, or a key whose name says it is a secret.
_SCHEME = r"(?:(?:bearer|basic|digest|negotiate|token)\s+)"
_GENERIC_SECRET = re.compile(
    r"((?i:bearer\s+)|(?i:basic\s+)(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z0-9])(?=[A-Za-z0-9+/]{8})"
    r"|(?i:\b(?:authorization|token|password|passwd|secret|api[_-]?key|apikey)[\"']?\s*[=:]\s*"
    r"[\"']?(?i:" + _SCHEME + r")?[\"']?))[^\s\"',;]{4,}")


def secret_parts(values: Sequence[str]) -> List[str]:
    """Every string that must never be printed: each supplied secret value, and its tokens of 6+ characters."""
    parts: List[str] = []
    for value in values:
        value = str(value)
        if value:
            parts.append(value)
            parts += [tok for tok in re.split(r"[\s,;=]+", value) if len(tok) >= 6 and tok.lower() not in (
                "bearer", "basic", "token")]
    return sorted(set(parts), key=len, reverse=True)


def redact(text: str, secrets: Sequence[str]) -> str:
    """``text`` with every supplied secret replaced; the one function all printing of our own output goes through."""
    for part in secrets:
        text = text.replace(part, "***")
    return _GENERIC_SECRET.sub(lambda m: m.group(1) + "***", text)


def render_diff(resolved: Resolved) -> str:
    """What this edit would add, and nothing else: the lines of our own snippet, never the file's existing
    content or its neighbours. Secret-looking values in our own lines are masked."""
    edit = resolved.edit
    if edit.format == "toml":
        text = toml_block(edit.toml_body).rstrip("\n")
    else:
        ops = [{"op": o["op"] if o["op"] != "set" else "set", "path": o["path"], "value": o["value"]}
               for o in resolved.applied] or list(edit.ops)
        text = json_snippet(ops)
    return "\n".join("+" + mask_secrets(line) for line in text.splitlines())


# --- journal ------------------------------------------------------------------

class LegacyJournal(RuntimeError):
    """The journal was written by an earlier, unreleased build. Nothing in it is treated as owned."""

    def __init__(self, path: Path, recorded: List[str]):
        self.path, self.recorded = path, recorded
        listing = "".join(f"\n  {item}" for item in recorded) or " (it lists no files)"
        super().__init__(
            f"{path} was written by an earlier build and is not read: nothing is changed. Remove by hand what "
            f"that build wrote:{listing}\nthen delete {path} and run `briefcase-ai connect` again")


def _recorded(data: Any) -> List[str]:
    """What an earlier build's journal says it wrote: its files and its MCP registrations."""
    import shlex

    if not isinstance(data, dict):
        return []
    items = [str(path) for path in sorted(data.get("files") or {})]
    for command in data.get("commands") or []:
        try:
            argv = [str(a) for a in (command.get("undo_hint") or command.get("argv") or [])]
            profile = command.get("config_dir") or command.get("profile")
            var = "CODEX_HOME" if argv[:1] == ["codex"] else "CLAUDE_CONFIG_DIR"
            prefix = f"{var}={shlex.quote(str(profile))} " if profile else ""
            where = f"cd {shlex.quote(str(command['cwd']))} && " if command.get("cwd") else ""
            items.append(f"MCP registration (profile {profile or 'default'}); to remove it run: "
                         f"{where}{prefix}{shlex.join(argv)}")
        except (AttributeError, TypeError):
            continue
    return items


def load_journal() -> Dict[str, Any]:
    path = journal_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        data = None
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("version") == JOURNAL_VERSION:
        data.setdefault("files", {})
        data.setdefault("commands", [])
        data.setdefault("targets", {})
        return data
    if data is not None:
        raise LegacyJournal(path, _recorded(data))
    return {"version": JOURNAL_VERSION, "files": {}, "commands": [], "targets": {}}


def save_journal(journal: Dict[str, Any]) -> None:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, json.dumps(journal, indent=2, ensure_ascii=False) + "\n", 0o600)


def _merge_ops(existing: List[Dict[str, Any]], applied: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add a new run's ops to the journal. Nothing already journalled is ever rewritten: a target that is
    present is left alone, so a new op never meets an old one for the same key."""
    return list(existing) + list(applied)


# --- what we wrote, as a hash --------------------------------------------------

_ABSENT = object()


def _sha(value: Any) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _json_values(ops: Sequence[Dict[str, Any]], doc: Optional[Dict[str, Any]]) -> List[Any]:
    out = []
    for op in ops:
        if op["op"] not in ("set", "append"):
            continue
        path = list(op["path"])
        parent = _descend(doc, path[:-1], create=False) if doc is not None else None
        current = parent.get(path[-1], _ABSENT) if parent is not None else _ABSENT
        if op["op"] == "append":
            present = isinstance(current, list) and _index_of(current, op["value"]) >= 0
            out.append([path, op["value"] if present else None])
        else:
            out.append([path, None if current is _ABSENT else current])
    return out


def content_hash(fmt: str, ops: Sequence[Dict[str, Any]], text: Optional[str],
                 tables: Sequence[str] = ()) -> Optional[str]:
    """A hash of what was written, as ``text`` holds it now, over parsed structure (typed, so ``true`` is not
    ``1``; spacing, key order and the trailing newline do not count): our named tables for TOML, the values at
    ``ops``' key paths for JSON. ``None`` when every one of them is absent."""
    if text is None:
        return None
    if fmt == "toml":
        return _toml_hash(text, tables)
    try:
        doc: Optional[Dict[str, Any]] = _load_json(text, Path("(target)")) if text.strip() else {}
    except EditRefused:
        doc = None
    values = _json_values(ops, doc)
    return _sha(values) if any(v[1] is not None for v in values) else None


def written_ops(applied: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The key paths and values a write set, for :func:`content_hash` to check later."""
    return [{"op": o["op"], "path": o["path"], "value": o["value"]} for o in applied
            if o["op"] in ("set", "append") and not o.get("default")]


def same_content(edit: FileEdit, rec: Dict[str, Any]) -> bool:
    """Is what ``edit`` would write the same as what the record says was written? (A ``default`` key is in
    neither: it is only ever set when absent.)"""
    if edit.format == "toml":
        return _toml_hash(edit.toml_body, edit.toml_tables) == rec.get("hash")
    wanted = sorted(_sha([list(op["path"]), op["value"]]) for op in edit.ops if op["op"] in ("set", "append"))
    wrote = sorted(_sha([list(op["path"]), op["value"]]) for op in rec.get("ops", []))
    return wanted == wrote


def exposure(edit: FileEdit, text: Optional[str], given: Dict[str, Any], other_layer: bool = False) -> List[str]:
    """Dotted names (never values) of existing keys in the tool's telemetry namespace that this edit does not write
    itself. Those would combine with the endpoint ``edit`` writes (a header, a helper command, a per-signal
    endpoint, a capture switch), so the write is refused; the only keys allowed are the ones we can account for."""
    if not text or edit.format != "json" or not (edit.namespace or edit.watch):
        return []
    try:
        doc = _load_json(text, Path("(target)"))
    except EditRefused:
        return []
    ours = set() if other_layer else {tuple(op["path"]) for op in edit.ops}
    found: List[str] = []
    for parent_path, prefix in edit.namespace:
        parent = _descend(doc, list(parent_path), create=False) if parent_path else doc
        if not isinstance(parent, dict):
            continue
        for key in parent:
            if key.lower().startswith(prefix.lower()) and tuple(list(parent_path) + [key]) not in ours:
                found.append(".".join(list(parent_path) + [key]))
    for path, _what in edit.watch:  # keys named explicitly, wherever they are
        parent = _descend(doc, list(path)[:-1], create=False)
        if parent is not None and path[-1] in parent and tuple(path) not in ours and ".".join(path) not in found:
            found.append(".".join(path))
    return found


class ExternalLayer(object):
    """A settings layer that is not a file we can read with :func:`read_text` (a Windows registry value, a macOS
    managed-preferences plist): ``read`` returns its JSON text, ``None`` when it does not exist, and raises
    ``OSError`` when it exists but cannot be read."""

    def __init__(self, name: str, read: Any) -> None:
        self.name = name
        self.read = read


def layer_risks(edit: FileEdit, layer: Any, given: Dict[str, Any]) -> List[str]:
    """Risks named by one other settings layer of the tool. A layer that exists but cannot be read, or whose content
    cannot be understood, counts as a risk: not knowing what it sets is not the same as it setting nothing."""
    name = getattr(layer, "name", None) or str(layer)
    try:
        text = layer.read() if isinstance(layer, ExternalLayer) else read_text(layer)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"{name}:(exists but cannot be read: {exc.__class__.__name__})"]
    if text is None or not text.strip():
        return []
    try:
        doc = _load_json(text, Path("(layer)"))
    except EditRefused:
        return [f"{name}:(is not valid JSON, so what it sets is unknown)"]
    if not isinstance(doc, dict):
        return [f"{name}:(is not a settings object, so what it sets is unknown)"]
    return [f"{name}:{item}" for item in exposure(edit, text, given, other_layer=True)]


def environment_risks(edit: FileEdit, environ: Any) -> List[str]:
    """Names (never values) of variables already exported in this process that the tool also reads as telemetry
    settings (the ``env`` object of its settings file) and that this edit does not itself write."""
    ours = {tuple(op["path"]) for op in edit.ops}
    found = []
    for parent, prefix in edit.namespace:
        if list(parent) != ["env"]:
            continue
        for key in environ:
            if key.lower().startswith(prefix.lower()) and ("env", key) not in ours:
                found.append(f"environment:{key}")
    for path, _what in edit.watch:
        if len(path) == 2 and path[0] == "env" and path[1] in environ and tuple(path) not in ours:
            item = f"environment:{path[1]}"
            if item not in found:
                found.append(item)
    return sorted(set(found))


def holds_any(edit: FileEdit, text: Optional[str]) -> bool:
    """Does the file already hold something at the places ``edit`` would write (whoever wrote it)?"""
    if not text:
        return False
    if edit.format == "toml":
        return bool(_toml_found(text, edit.toml_tables))
    try:
        doc = _load_json(text, Path("(target)"))
    except EditRefused:
        return False
    for op in edit.ops:
        if op["op"] not in ("set", "append"):
            continue
        parent = _descend(doc, list(op["path"])[:-1], create=False)
        if parent is None or op["path"][-1] not in parent:
            continue
        if op["op"] == "set":
            return True
        marker = op.get("owned_by") or ""
        items = parent[op["path"][-1]]
        if isinstance(items, list) and any(
                typed_eq(op["value"], i) or (marker and marker in json.dumps(i)) for i in items):
            return True
    return False


# --- write --------------------------------------------------------------------

def _atomic_write(target: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_path(target: Path, now: Optional[float] = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
    candidate = target.with_name(f"{target.name}.briefcase-bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.briefcase-bak-{stamp}-{counter}")
        counter += 1
    return candidate


def write_checked(target: Path, text: str, expect: Optional[str], mode: int) -> None:
    """The one way a config file is written. ``expect`` is the exact content the file must still have
    (``None``: it must not exist). A new file is created exclusively (hard link of a private temp file, so it is
    complete the moment it appears and an existing file is never replaced); an existing one is replaced only
    when it still holds ``expect``. Anything else is refused, never overwritten."""
    if expect is None:
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            os.chmod(tmp, mode)
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise EditRefused(f"{target} appeared after the preview; it was not touched") from None
            except OSError:  # no hard links here: still exclusive
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    with os.fdopen(os.open(target, flags, mode), "w", encoding="utf-8", newline="") as handle:
                        handle.write(text)
                except FileExistsError:
                    raise EditRefused(f"{target} appeared after the preview; it was not touched") from None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return
    try:
        current = read_text(target)
    except (OSError, UnicodeDecodeError):
        raise EditRefused(f"{target} changed after the preview; it was not touched") from None
    if current != expect:
        raise EditRefused(f"{target} changed after the preview; it was not touched")
    _atomic_write(target, text, mode)


def apply(resolved: Resolved, journal: Dict[str, Any]) -> Optional[Path]:
    """Write one resolved edit, back the file up first, and journal it.

    Returns the backup path (``None`` when the file is new). The journal is
    updated in memory; the caller saves it once per run.
    """
    if resolved.status != CHANGE:
        return None
    target = resolved.target
    backup: Optional[Path] = None
    mode = 0o600
    if resolved.existed:
        mode = 0o600 if resolved.edit.secret else target.stat().st_mode & 0o7777  # a credential: never group/world
        if read_text(target) != resolved.old_text:
            raise EditRefused(f"{target} changed after the preview; it was not touched")
        backup = backup_path(target)
        shutil.copy2(str(target), str(backup))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_checked(target, resolved.new_text, resolved.old_text if resolved.existed else None, mode)
    except EditRefused:
        if backup is not None:
            try:
                backup.unlink()
            except OSError:
                pass
        raise

    entry = journal["files"].setdefault(str(target), {
        "format": resolved.edit.format, "created_file": not resolved.existed, "ops": [], "backups": [],
    })
    entry["ops"] = _merge_ops(entry["ops"], resolved.applied)
    if backup is not None:
        entry["backups"].append(str(backup))
    entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return backup


# --- verify / undo ------------------------------------------------------------

def _json_op_present(doc: Dict[str, Any], op: Dict[str, Any]) -> bool:
    parent = _descend(doc, op["path"][:-1], create=False)
    if parent is None or op["path"][-1] not in parent:
        return False
    current = parent[op["path"][-1]]
    if op["op"] == "append":
        return isinstance(current, list) and _index_of(current, op["value"]) >= 0
    return typed_eq(current, op["value"])


def check_entry(path: str, entry: Dict[str, Any], tool: Optional[str] = None) -> List[Tuple[bool, str]]:
    """For each journalled key of ``tool`` in ``path``: is our value still there, exactly as written?"""
    results: List[Tuple[bool, str]] = []
    ops = [o for o in entry["ops"] if tool is None or o.get("tool") == tool]
    if not ops:
        return results
    try:
        text = read_text(Path(path))
    except OSError:
        return [(False, f"{path}: file is gone")]
    if entry["format"] == "toml":
        for op in (o for o in ops if o["op"] == "toml_block"):
            ok = _toml_hash(text, op.get("tables", [])) == op.get("hash")
            results.append((ok, f"{path}: {', '.join(op.get('tables', [])) or 'briefcase block'}"))
        return results
    try:
        doc = _load_json(text, Path(path))
    except EditRefused as exc:
        return [(False, str(exc))]
    for op in ops:
        results.append((_json_op_present(doc, op), f"{path}: {'.'.join(op['path'])}"))
    return results


def _remove_json(text: str, op: Dict[str, Any]) -> Tuple[str, str]:
    """Remove one journalled op from ``text`` if it still holds exactly what we wrote. Returns ``(text, outcome)``
    with outcome ``removed``, ``gone`` or ``changed``."""
    root = jsontext.parse(text)
    path = list(op["path"])
    parent = jsontext.find(root, path[:-1]) if len(path) > 1 else root
    if parent is None or parent.kind != "object":
        return text, "gone"
    index = next((i for i, m in enumerate(parent.members) if m.key == path[-1]), -1)
    if index < 0:
        return text, "gone"
    node = parent.members[index].value
    if op["op"] == "append":
        if node.kind != "array":
            return text, "changed"
        items = [json.loads(text[n.start:n.end]) for n in node.items]
        at = _index_of(items, op["value"])
        if at >= 0:
            return jsontext.remove_item(text, node, at), "removed"
        marker = op.get("owned_by") or ""
        edited = marker and any(marker in json.dumps(item) for item in items)
        return text, ("changed" if edited else "gone")
    if typed_eq(json.loads(text[node.start:node.end]), op["value"]):
        return jsontext.remove_member(text, parent, index), "removed"
    return text, "changed"


def _prune_json(text: str, created: Sequence[Sequence[str]]) -> str:
    for path in sorted(created, key=len, reverse=True):
        root = jsontext.parse(text)
        parent = jsontext.find(root, list(path)[:-1]) if len(path) > 1 else root
        if parent is None or parent.kind != "object":
            continue
        index = next((i for i, m in enumerate(parent.members) if m.key == path[-1]), -1)
        if index >= 0:
            node = parent.members[index].value
            if node.kind in ("object", "array") and not (node.members or node.items):
                text = jsontext.remove_member(text, parent, index)
    return text


def undo_entry(path: str, entry: Dict[str, Any], tool: Optional[str] = None,
               dry_run: bool = False, keep_unremoved: bool = False,
               stats: Optional[Dict[str, int]] = None) -> Tuple[List[str], bool]:
    """Remove the journalled keys of ``tool`` (all tools when ``None``) from ``path`` as text deletions, only where
    the file still holds exactly what we wrote; everything else, including later user edits, stays byte for byte.
    With ``keep_unremoved`` what could not be removed stays in the journal.

    Returns ``(messages, fully_undone)``; when fully undone the caller drops the journal entry.
    """
    messages: List[str] = []
    target = Path(path)
    mine = [o for o in entry["ops"] if tool is None or o.get("tool") == tool]
    rest = [o for o in entry["ops"] if o not in mine]
    if not mine:
        return messages, not entry["ops"]
    if not target.exists():
        entry["ops"] = rest
        return [f"{path}: already gone"], not rest
    old = read_text(target)
    retained: List[Dict[str, Any]] = []
    pending: List[List[str]] = []
    if entry["format"] == "toml":
        new = old
        for op in mine:
            tables = op.get("tables", [])
            found = _toml_hash(old, tables)
            if found is None:
                messages.append(f"{path}: {', '.join(tables)} already removed")
            elif op.get("hash") and found != op["hash"]:
                messages.append(f"{path}: {', '.join(tables)} was changed after connect; left as it is")
                retained.append(op)
            else:
                candidate = _toml_remove(new, tables, bool(op.get("added_newline")))
                try:
                    exact = _toml.loads(candidate) == _without_tables(_toml.loads(new), tables)
                except Exception:
                    exact = False
                if exact and op.get("lines"):  # only lines we wrote may go (a user's comment inside is theirs)
                    gone = difflib.ndiff(new.splitlines(), candidate.splitlines())
                    removed_lines = [ln[2:].strip() for ln in gone if ln.startswith("- ")]
                    exact = all(not ln or ln in set(op["lines"]) for ln in removed_lines)
                if not exact:
                    messages.append(f"{path}: {', '.join(tables)} cannot be removed without risking the rest of "
                                    "the file (the text around it does not parse back identically), so nothing "
                                    "was changed; remove those tables by hand")
                    retained.append(op)
                else:
                    new = candidate
                    messages.append(f"{path}: removed {', '.join(tables)}")
        if not new.strip():
            new = ""
    else:
        try:
            jsontext.parse(old)
            _load_json(old, target)  # an ambiguous document (a key twice) is not edited by undo either
        except (ValueError, EditRefused):
            return [f"{path} cannot be read unambiguously as JSON; nothing was changed. Remove the briefcase keys "
                    "by hand."], False
        new = old
        created: List[List[str]] = []
        for op in reversed(mine):
            dotted = ".".join(op["path"])
            created.extend(op.get("created", []))
            new, outcome = _remove_json(new, op)
            if outcome == "removed":
                if stats is not None:
                    stats["removed"] = stats.get("removed", 0) + 1
                messages.append(f"{path}: removed {dotted}" if op["op"] == "set"
                                else f"{path}: removed our entry from {dotted}")
            elif outcome == "gone":
                messages.append(f"{path}: {dotted} already removed")
            else:
                messages.append(f"{path}: {dotted} was changed after connect; left as it is")
                retained.append(op)
        created.extend(entry.get("pending_prune", []))
        new = _prune_json(new, created)
        root = jsontext.parse(new) if new.strip() else None
        for item in created:
            if root is not None and jsontext.find(root, list(item)) is not None and list(item) not in pending:
                pending.append(list(item))
    if not dry_run:
        if new == old:
            pass
        else:
            empty = (not new.strip()) or (entry["format"] == "json" and json.loads(new) == {})
            if empty and entry.get("created_file") and not rest and not retained:
                backup = backup_path(target)
                shutil.copy2(str(target), str(backup))
                if read_text(target) == old:
                    target.unlink()
                    messages.append(f"{path}: deleted (briefcase created it and nothing else is in it)")
            else:
                if not new.strip() and entry["format"] == "json":
                    new = "{}\n"
                backup = backup_path(target)
                shutil.copy2(str(target), str(backup))
                write_checked(target, new, old, target.stat().st_mode & 0o7777)
        entry["ops"] = rest + (retained if keep_unremoved else [])
        if entry["format"] == "json":
            if pending and entry["ops"]:
                entry["pending_prune"] = pending
            else:
                entry.pop("pending_prune", None)
    return messages, not (rest or (retained and keep_unremoved))
