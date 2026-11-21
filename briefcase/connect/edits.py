"""Key-level config edits for ``briefcase connect``: plan, diff, write, undo.

Nothing here rewrites a file wholesale. JSON files are parsed with the stdlib,
only the named keys are changed, and unrelated keys keep their order. TOML
files get one marker-delimited block that is appended or replaced in place.
Every write is preceded by a timestamped backup and recorded in a journal so
``--undo`` can restore exactly the keys that were set, even after the user has
edited the file again.
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
JOURNAL_VERSION = 1

CHANGE, NOOP, REFUSED = "change", "noop", "refused"

# Directories no edit may ever target: managed/system configuration belongs to
# an administrator, and connect only prints a snippet for it.
_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/opt", "/var", "/System", "/Library",
    "c:\\programdata", "c:\\windows", "c:\\program files",
)

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
    key is absent) or ``append`` (add ``value`` to a list unless present; an
    ``owned_by`` substring replaces a stale item of ours instead of adding a
    second one). For ``format="toml"``, ``toml_body`` is the text between the
    markers and ``toml_tables`` the dotted table names it defines.
    """

    tool: str
    path: Path
    format: str
    purpose: str
    ops: List[Dict[str, Any]] = field(default_factory=list)
    toml_body: str = ""
    toml_tables: Tuple[str, ...] = ()
    notes: List[str] = field(default_factory=list)


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


# --- path safety --------------------------------------------------------------

def is_system_path(path: Path) -> bool:
    text = str(path)
    lowered = text.lower()
    for prefix in _SYSTEM_PREFIXES:
        if lowered == prefix.lower() or lowered.startswith(prefix.lower().rstrip("/\\") + os.sep):
            return True
    return False


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
    linked = path.is_symlink() or any(p.is_symlink() for p in path.parents if _inside(p, home()))
    if linked and resolved != path:
        target = resolved
        if not _inside(resolved, real_home):
            warnings.append(
                f"{path} is a symlink to {resolved}, which is outside your home directory; "
                "the edit is written to that file"
            )
        else:
            warnings.append(f"{path} is a symlink to {resolved}")
    # Checked on the resolved path, with temp dirs (tests, CI homes) exempt
    # only when they are the user's actual home or working tree.
    if is_system_path(resolved) and not (
        _inside(resolved, real_home) or _inside(resolved, Path.cwd().resolve())
    ):
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


def _load_json(text: str, path: Path) -> Dict[str, Any]:
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except ValueError as exc:
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
            had = key in parent
            if had and (kind == "default" or parent[key] == value):
                continue
            entry = {
                "op": "set", "tool": tool, "path": path, "value": value,
                "had_prior": had, "prior": copy.deepcopy(parent.get(key)), "created": created,
            }
            parent[key] = copy.deepcopy(value)
            applied.append(entry)
        elif kind == "append":
            if key not in parent:
                parent[key] = []
                created.append(path)
            items = parent[key]
            if not isinstance(items, list):
                raise EditRefused(f"expected a list at {'.'.join(path)!r}, found {type(items).__name__}")
            if value in items:
                continue
            entry = {"op": "append", "tool": tool, "path": path, "value": value, "created": created}
            marker = op.get("owned_by")
            stale = None
            if marker:
                for index, item in enumerate(items):
                    if marker in json.dumps(item):
                        stale = index
                        break
            if stale is not None:
                entry["replaced"] = copy.deepcopy(items[stale])
                items[stale] = copy.deepcopy(value)
            else:
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
        "# Managed by `briefcase connect`; remove with `briefcase connect --undo`.\n"
        f"{body.rstrip()}\n"
        f"{TOML_END}\n"
    )


_BLOCK_RE = re.compile(
    r"^" + re.escape(TOML_BEGIN) + r"[^\n]*\n.*?^" + re.escape(TOML_END) + r"[^\n]*\n?",
    re.MULTILINE | re.DOTALL,
)


def _toml_has_table(doc: Dict[str, Any], dotted: str) -> bool:
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _resolve_toml(edit: FileEdit, old: str) -> Tuple[str, str]:
    if _toml is None:
        raise EditRefused("no TOML parser is available on this Python (needs 3.11+ or `pip install tomli`)")
    block = toml_block(edit.toml_body)
    match = _BLOCK_RE.search(old)
    outside = old[:match.start()] + old[match.end():] if match else old
    try:
        parsed = _toml.loads(outside)
    except Exception as exc:
        raise EditRefused(f"{edit.path} is not valid TOML ({exc}); fix it or add the snippet by hand")
    for table in edit.toml_tables:
        if _toml_has_table(parsed, table):
            raise EditRefused(
                f"{edit.path} already defines [{table}] outside the briefcase block; "
                "merge the snippet into it by hand"
            )
    if match:
        new = old[:match.start()] + block + old[match.end():]
    elif old.strip():
        new = old.rstrip("\n") + "\n\n" + block
    else:
        new = block
    try:
        _toml.loads(new)
    except Exception as exc:  # pragma: no cover - our own block is malformed
        raise EditRefused(f"refusing to write invalid TOML to {edit.path}: {exc}")
    return new, block


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
            old = target.read_text(encoding="utf-8")
        if edit.format == "toml":
            new, block = _resolve_toml(edit, old)
            applied = [{"op": "toml_block", "tool": edit.tool, "tables": list(edit.toml_tables)}]
            if new == old:
                applied = []
        else:
            doc = _load_json(old, edit.path)
            applied = _apply_json_ops(doc, edit.ops, edit.tool)
            new = _dump_json(doc, old) if applied else old
    except EditRefused as exc:
        return Resolved(edit, REFUSED, target, old_text=old, existed=existed,
                        reason=str(exc), warnings=warnings)
    except (OSError, UnicodeDecodeError) as exc:
        return Resolved(edit, REFUSED, target, existed=existed,
                        reason=f"cannot read {target}: {exc}", warnings=warnings)
    status = CHANGE if applied and new != old else NOOP
    return Resolved(edit, status, target, old_text=old, new_text=new, applied=applied,
                    existed=existed, warnings=warnings)


_INLINE_HEADERS = re.compile(r"(headers\s*=\s*)\{[^{}]*[^{}\s][^{}]*\}")


def mask_secrets(line: str) -> str:
    line = _INLINE_HEADERS.sub(r"\1{ *** }", line)
    match = _SECRET_LINE.match(line)
    if not match or match.group("value").strip() in ("{", "[", "{}", "[]"):
        return line
    return f"{match.group('head')}\"***\"{match.group('comma')}"


def render_diff(resolved: Resolved) -> str:
    """Unified diff of the change. Values of secret-looking keys are masked so
    a neighbouring API key never lands in a terminal scrollback or CI log."""
    name = str(resolved.edit.path)
    lines = difflib.unified_diff(
        resolved.old_text.splitlines(), resolved.new_text.splitlines(),
        fromfile=name if resolved.existed else "/dev/null", tofile=name, lineterm="", n=2,
    )
    out = []
    for line in lines:
        out.append(line if line.startswith(("---", "+++", "@@")) else mask_secrets(line))
    return "\n".join(out)


# --- journal ------------------------------------------------------------------

def load_journal() -> Dict[str, Any]:
    path = journal_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == JOURNAL_VERSION:
            data.setdefault("files", {})
            data.setdefault("commands", [])
            data.setdefault("tools", {})
            return data
    except (OSError, ValueError):
        pass
    return {"version": JOURNAL_VERSION, "files": {}, "commands": [], "tools": {}}


def save_journal(journal: Dict[str, Any]) -> None:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, json.dumps(journal, indent=2, ensure_ascii=False) + "\n", 0o600)


def _op_key(op: Dict[str, Any]) -> str:
    extra = json.dumps(op["value"], sort_keys=True) if op["op"] == "append" else ""
    return json.dumps([op["op"], op.get("path"), extra])


def _merge_ops(existing: List[Dict[str, Any]], applied: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold a new run's ops into the journal.

    When a key we already own is set again (say, a new endpoint), the journal
    keeps the value from before the *first* connect as the prior, so undo
    restores what the user had, not our own earlier value.
    """
    merged = list(existing)
    for op in applied:
        if op["op"] == "toml_block":
            merged = [o for o in merged if o["op"] != "toml_block"] + [op]
            continue
        if op["op"] == "append" and "replaced" in op:
            stale = [o for o in merged if o["op"] == "append" and o.get("path") == op["path"]
                     and o["value"] == op["replaced"]]
            merged = [o for o in merged if o not in stale]
            op = {k: v for k, v in op.items() if k != "replaced"}
            # The containers the replaced entry created are still ours to prune.
            op["created"] = [c for o in stale for c in o.get("created", [])] + op.get("created", [])
        for index, old in enumerate(merged):
            if old["op"] == op["op"] and _op_key(old) == _op_key(op):
                if op["op"] == "set":
                    op = dict(op, had_prior=old["had_prior"], prior=old["prior"])
                op = dict(op, created=old.get("created", []) + [
                    c for c in op.get("created", []) if c not in old.get("created", [])])
                merged[index] = op
                break
        else:
            merged.append(op)
    return merged


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
        # The file may have changed between plan and confirmation; never write
        # a merge computed against stale content.
        if target.read_text(encoding="utf-8") != resolved.old_text:
            raise EditRefused(f"{target} changed while the plan was on screen; run connect again")
        mode = target.stat().st_mode & 0o7777
        backup = backup_path(target)
        shutil.copy2(str(target), str(backup))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, resolved.new_text, mode)

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
        return isinstance(current, list) and op["value"] in current
    return current == op["value"]


def check_entry(path: str, entry: Dict[str, Any], tool: Optional[str] = None) -> List[Tuple[bool, str]]:
    """For each journalled key of ``tool`` in ``path``: is our value still there?"""
    results: List[Tuple[bool, str]] = []
    ops = [o for o in entry["ops"] if tool is None or o.get("tool") == tool]
    if not ops:
        return results
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return [(False, f"{path}: file is gone")]
    if entry["format"] == "toml":
        results.append((bool(_BLOCK_RE.search(text)), f"{path}: briefcase block"))
        return results
    try:
        doc = _load_json(text, Path(path))
    except EditRefused as exc:
        return [(False, str(exc))]
    for op in ops:
        results.append((_json_op_present(doc, op), f"{path}: {'.'.join(op['path'])}"))
    return results


def _prune(doc: Dict[str, Any], created: Sequence[Sequence[str]]) -> None:
    for path in sorted(created, key=len, reverse=True):
        parent = _descend(doc, path[:-1], create=False)
        if parent is not None and path[-1] in parent and parent[path[-1]] in ({}, []):
            del parent[path[-1]]


def undo_entry(path: str, entry: Dict[str, Any], tool: Optional[str] = None,
               dry_run: bool = False) -> Tuple[List[str], bool]:
    """Remove the journalled keys of ``tool`` (all tools when ``None``) from
    ``path``, leaving everything else, including later user edits, alone.

    Returns ``(messages, fully_undone)``; when fully undone the caller drops
    the journal entry.
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
    old = target.read_text(encoding="utf-8")
    if entry["format"] == "toml":
        new = old
        match = _BLOCK_RE.search(old)
        if match:
            # Also take back the blank line that was inserted ahead of the block.
            start = match.start() - 1 if old[:match.start()].endswith("\n\n") else match.start()
            new = old[:start] + old[match.end():]
        if not new.strip():
            new = ""
        messages.append(f"{path}: removed the briefcase block" if new != old
                        else f"{path}: briefcase block already removed")
    else:
        try:
            doc = _load_json(old, target)
        except EditRefused as exc:
            return [f"{exc}; nothing was changed. Remove the briefcase keys by hand."], False
        created: List[List[str]] = []
        for op in reversed(mine):
            dotted = ".".join(op["path"])
            created.extend(op.get("created", []))
            parent = _descend(doc, op["path"][:-1], create=False)
            key = op["path"][-1]
            if parent is None or key not in parent:
                messages.append(f"{path}: {dotted} already removed")
                continue
            if op["op"] == "append":
                if isinstance(parent[key], list) and op["value"] in parent[key]:
                    parent[key].remove(op["value"])
                    messages.append(f"{path}: removed our entry from {dotted}")
                else:
                    messages.append(f"{path}: our entry in {dotted} already removed")
            elif parent[key] == op["value"]:
                if op.get("had_prior"):
                    parent[key] = op["prior"]
                    messages.append(f"{path}: restored {dotted}")
                else:
                    del parent[key]
                    messages.append(f"{path}: removed {dotted}")
            else:
                messages.append(f"{path}: {dotted} was changed after connect; left as it is")
        _prune(doc, created)
        new = _dump_json(doc, old) if doc else ""
    if not dry_run:
        if new == old:
            pass
        elif not new.strip() and entry.get("created_file") and not rest:
            target.unlink()
            messages.append(f"{path}: deleted (briefcase created it and nothing else is in it)")
        else:
            if not new.strip():
                new = "{}\n" if entry["format"] == "json" else ""
            backup = backup_path(target)
            shutil.copy2(str(target), str(backup))
            _atomic_write(target, new, target.stat().st_mode & 0o7777)
        entry["ops"] = rest
    return messages, not rest
