"""Named, content-hashed prompt templates.

A prompt gets a name and a version: the first 12 hex characters of the
SHA-256 of its template text. Records made while the prompt is in use carry
``prompt_name`` and ``prompt_version``, so evaluation datasets can be filtered
by prompt and eval results grouped by prompt version ("did the new prompt
help?").

    from briefcase.prompts import prompt

    TRIAGE = prompt("triage", "Classify this ticket as bug/question/other:\\n{text}")

    @briefcase.capture
    def triage(text):
        return llm(TRIAGE.render(text=text))   # the triage record is tagged

    with TRIAGE:                               # or tag everything inside a block
        client.messages.create(...)            # provider records are tagged

A prompt is "used" by ``render()`` or by entering it with ``with``. A record
is tagged when it is made while the prompt is active (``with``), or when the
prompt is used at any point while the record's call is running.

Each distinct (name, version) is also saved, best-effort, under
``$BRIEFCASE_HOME/prompts`` (default ``~/.briefcase/prompts``) so
``briefcase prompt list|show|diff`` can show history. That directory never
leaves the machine. Set ``BRIEFCASE_PROMPT_REGISTRY=off`` to keep templates
in memory only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import string
import threading
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION_CHARS = 12
ENV_REGISTRY = "BRIEFCASE_PROMPT_REGISTRY"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

_ACTIVE: ContextVar[Optional["Prompt"]] = ContextVar("briefcase_prompt_active", default=None)
# Open record frames in this context; a use is noted in every open frame so a
# record learns about prompts used anywhere during its call.
_FRAMES: ContextVar[Tuple[list, ...]] = ContextVar("briefcase_prompt_frames", default=())
_MAX_FRAMES = 32
_MAX_OPEN = 4096
_OPEN: "OrderedDict[int, list]" = OrderedDict()
_OPEN_LOCK = threading.Lock()

_REGISTRY: Dict[Tuple[str, str], "Prompt"] = {}
_REGISTRY_LOCK = threading.Lock()


def prompt_version(template: str) -> str:
    """Short content hash of a template: stable across processes and machines."""
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:VERSION_CHARS]


class Prompt:
    """A named template with a content-derived version.

    ``render(**values)`` fills ``{placeholders}`` (``str.format`` syntax) and
    marks the prompt as used. ``with prompt:`` makes it active for the block.
    """

    __slots__ = ("name", "template", "version", "_tokens")

    def __init__(self, name: str, template: str) -> None:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(
                f"prompt name must match {_NAME_RE.pattern} (letters, digits, '.', '_', '-'), got {name!r}"
            )
        if not isinstance(template, str):
            raise TypeError(f"prompt template must be a str, got {type(template).__name__}")
        self.name = name
        self.template = template
        self.version = prompt_version(template)
        self._tokens: List[Any] = []

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def fields(self) -> Dict[str, str]:
        return {"prompt_name": self.name, "prompt_version": self.version}

    @property
    def placeholders(self) -> List[str]:
        names = []
        for _, field, _, _ in string.Formatter().parse(self.template):
            if field and field not in names:
                names.append(field)
        return names

    def render(self, **values: Any) -> str:
        _note_use(self)
        return self.template.format(**values)

    def __enter__(self) -> "Prompt":
        self._tokens.append(_ACTIVE.set(self))
        _note_use(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._tokens:
            try:
                _ACTIVE.reset(self._tokens.pop())
            except ValueError:  # exited in a different context
                _ACTIVE.set(None)

    def __repr__(self) -> str:
        return f"Prompt({self.name!r}, version={self.version!r})"


def prompt(name: str, template: str, *, save: Optional[bool] = None) -> Prompt:
    """Register a template under ``name`` and return a :class:`Prompt`.

    ``save`` (default: on unless ``BRIEFCASE_PROMPT_REGISTRY=off``) writes the
    template to the local registry so the CLI can list and diff versions.
    """
    p = Prompt(name, template)
    with _REGISTRY_LOCK:
        _REGISTRY[(name, p.version)] = p
    if save is None:
        save = os.environ.get(ENV_REGISTRY, "").strip().lower() not in ("0", "off", "false", "no")
    if save:
        try:
            save_prompt(p)
        except OSError:
            pass
    return p


def current_prompt() -> Optional[Prompt]:
    """The prompt made active with ``with`` in this context, if any."""
    return _ACTIVE.get()


def registered() -> List[Prompt]:
    """Prompts created in this process, in creation order."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


# -- record hooks (called by briefcase.decorators; never raise) -------------

def _note_use(p: Prompt) -> None:
    for frame in _FRAMES.get():
        if frame:
            frame[0] = p
        else:
            frame.append(p)


def on_record_start(record: dict) -> None:
    """Tag a record being built with the active prompt and open a frame that
    collects prompts used while its call runs."""
    try:
        active = _ACTIVE.get()
        if active is not None:
            record.update(active.fields)
        frame: list = []
        frames = _FRAMES.get() + (frame,)
        _FRAMES.set(frames[-_MAX_FRAMES:])
        with _OPEN_LOCK:
            _OPEN[id(record)] = frame
            while len(_OPEN) > _MAX_OPEN:
                _OPEN.popitem(last=False)
    except Exception:
        pass


def on_record_end(record: dict) -> None:
    """Close the record's frame; tag it with the last prompt used during the
    call when it was not already tagged at start."""
    try:
        with _OPEN_LOCK:
            frame = _OPEN.pop(id(record), None)
        if frame is None:
            return
        frames = _FRAMES.get()
        if any(f is frame for f in frames):
            _FRAMES.set(tuple(f for f in frames if f is not frame))
        if "prompt_name" not in record and frame:
            record.update(frame[0].fields)
    except Exception:
        pass


# -- local registry ---------------------------------------------------------

def registry_dir(home: Optional[Path] = None) -> Path:
    if home is None:
        env = os.environ.get("BRIEFCASE_HOME")
        home = Path(env) if env else Path.home() / ".briefcase"
    return Path(home) / "prompts"


def save_prompt(p: Prompt, home: Optional[Path] = None) -> Path:
    """Write ``<registry>/<name>/<version>.json`` once (versions are immutable)."""
    root = registry_dir(home)
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    directory = root / p.name
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{p.version}.json"
    if path.exists():
        return path
    payload = {
        "name": p.name,
        "version": p.version,
        "sha256": hashlib.sha256(p.template.encode("utf-8")).hexdigest(),
        "template": p.template,
        "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_saved(home: Optional[Path] = None) -> Dict[str, List[dict]]:
    """``{name: [version entries, oldest first]}`` from the local registry.
    Entries whose template no longer matches their version are flagged
    ``"tampered": True``."""
    root = registry_dir(home)
    out: Dict[str, List[dict]] = {}
    if not root.is_dir():
        return out
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        entries = []
        for path in directory.glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("template"), str):
                continue
            entry["tampered"] = prompt_version(entry["template"]) != entry.get("version")
            entries.append(entry)
        if entries:
            entries.sort(key=lambda e: (str(e.get("first_seen", "")), str(e.get("version"))))
            out[directory.name] = entries
    return out


__all__ = [
    "Prompt",
    "prompt",
    "prompt_version",
    "current_prompt",
    "registered",
    "load_saved",
    "save_prompt",
    "registry_dir",
]
