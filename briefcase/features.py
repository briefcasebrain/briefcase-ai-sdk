"""The feature manifest: one small JSON file per feature in ``briefcase/_features/``.

``briefcase-ai features``, ``briefcase-ai --help`` and the docs catalog are all generated from
these files, so a feature that has a manifest can be found without reading source.

    from briefcase.features import load_catalog

    catalog = load_catalog()
    for feature in catalog.by_verb("enforce"):
        print(feature.id, feature.command)
    catalog.problems        # one line per file that was skipped or adjusted

A manifest (``briefcase/_features/policy-hooks.json``)::

    {
      "id": "policy-hooks",
      "title": "Policy for coding tools",
      "verb": "enforce",
      "one_line": "Allow, ask or deny what Claude Code and Cursor may run.",
      "command": "briefcase-ai policy init",
      "docs_slug": "enforce/policy-hooks",
      "status": "experimental",
      "requires_extra": null,
      "verified_against": "Claude Code 2.1.330 (real install); Cursor (docs only)"
    }

Required: ``id``, ``title``, ``verb`` (connect | capture | enforce | verify | review),
``one_line`` (under 80 characters), ``command``, ``docs_slug``, ``status`` (stable |
experimental). Optional: ``requires_extra`` (a pip extra name or null), ``verified_against``,
``docs_path`` (repo-relative path of the page, when it is not ``docs/features/<id>.md``),
``snippet`` (a few lines of code for a feature used from Python) and ``status_id`` (the id of
the ``briefcase-ai status`` row that says whether the feature is on; defaults to ``id``) and
``runtime`` (``"python"``, the default, or ``"node"``: the feature is started from another
runtime, so ``command`` need not begin with
 ``briefcase-ai``).
Unknown keys are kept in ``Feature.extra`` and otherwise ignored.

Loading never raises. A file that is not valid JSON, is missing a required key or names an
unknown verb is skipped; an unknown ``status`` is read as ``experimental``. Each such case
adds a line to ``Catalog.problems`` and is logged as a warning.

The files are read with ``importlib.resources``, so they load from an installed wheel as
well as from a checkout. They ship only if ``pyproject.toml`` lists
``"briefcase/_features/*.json"`` under ``[tool.maturin] include``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from briefcase._logging import get_logger

logger = get_logger(__name__)

PACKAGE = "briefcase"
DIRECTORY = "_features"
VERBS = ("connect", "capture", "enforce", "verify", "review")
VERB_TITLES = {"connect": "Connect", "capture": "Capture", "enforce": "Enforce",
               "verify": "Verify", "review": "Review"}
STATUSES = ("stable", "experimental")
REQUIRED = ("id", "title", "verb", "one_line", "command", "docs_slug", "status")
OPTIONAL = ("requires_extra", "verified_against", "docs_path", "snippet", "status_id", "runtime")
RUNTIMES = ("python", "node")
ONE_LINE_MAX = 80
REPO_URL = "https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/"


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    verb: str
    one_line: str
    command: str
    docs_slug: str
    status: str
    requires_extra: Optional[str] = None
    verified_against: Optional[str] = None
    docs_path: Optional[str] = None
    snippet: Optional[str] = None
    status_id: Optional[str] = None
    runtime: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def docs_file(self) -> str:
        """Repo-relative path of the feature's page (may carry a ``#anchor``)."""
        return self.docs_path or f"docs/features/{self.id}.md"

    @property
    def docs_url(self) -> str:
        if self.docs_file.startswith(("https://", "http://")):
            return self.docs_file  # a plugin's manifest may point at its own docs
        return REPO_URL + self.docs_file

    def to_dict(self) -> Dict[str, Any]:
        data = {name: getattr(self, name) for name in REQUIRED}
        data["requires_extra"] = self.requires_extra
        data["verified_against"] = self.verified_against
        if self.snippet:
            data["snippet"] = self.snippet
        if self.extra.get("plugin"):
            data["plugin"] = self.extra["plugin"]
        data["docs_file"] = self.docs_file
        data["docs_url"] = self.docs_url
        return data


@dataclass
class Catalog:
    features: List[Feature] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def get(self, feature_id: str) -> Optional[Feature]:
        for feature in self.features:
            if feature.id == feature_id:
                return feature
        return None

    def by_verb(self, verb: str) -> List[Feature]:
        return [feature for feature in self.features if feature.verb == verb]

    def groups(self) -> List[Tuple[str, List[Feature]]]:
        """``(verb, features)`` in flow order, verbs with no feature left out."""
        return [(verb, self.by_verb(verb)) for verb in VERBS if self.by_verb(verb)]

    def to_dict(self) -> Dict[str, Any]:
        groups = self.groups()
        return {
            "schema_version": 1,
            "count": len(self.features),
            "groups": [
                {"verb": verb, "title": VERB_TITLES[verb], "count": len(items),
                 "features": [item.to_dict() for item in items]}
                for verb, items in groups
            ],
        }


def parse_feature(data: Any, source: str = "<data>") -> Tuple[Optional[Feature], List[str]]:
    """Validate one manifest. Returns ``(feature or None, problems)``."""
    problems: List[str] = []
    if not isinstance(data, dict):
        return None, [f"{source}: skipped, the top level must be a JSON object"]
    missing = [key for key in REQUIRED if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        return None, [f"{source}: skipped, missing or non-text key(s): {', '.join(missing)}"]
    verb = data["verb"].strip().lower()
    if verb not in VERBS:
        return None, [f"{source}: skipped, verb {data['verb']!r} is not one of {', '.join(VERBS)}"]
    status = data["status"].strip().lower()
    if status not in STATUSES:
        problems.append(f"{source}: status {data['status']!r} is not one of {', '.join(STATUSES)}; "
                        "shown as experimental")
        status = "experimental"
    one_line = " ".join(data["one_line"].split())
    if len(one_line) >= ONE_LINE_MAX:
        problems.append(f"{source}: one_line is {len(one_line)} characters; keep it under {ONE_LINE_MAX}")
    optional: Dict[str, Optional[str]] = {}
    for key in OPTIONAL:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"{source}: {key} must be text or null; ignored")
            value = None
        optional[key] = value or None
    if optional["runtime"] is not None:
        optional["runtime"] = optional["runtime"].strip().lower()
        if optional["runtime"] not in RUNTIMES:
            problems.append(f"{source}: runtime {optional['runtime']!r} is not one of "
                            f"{', '.join(RUNTIMES)}; ignored")
            optional["runtime"] = None
    known = set(REQUIRED) | set(OPTIONAL)
    feature = Feature(
        id=data["id"].strip(), title=data["title"].strip(), verb=verb, one_line=one_line,
        command=data["command"].strip(), docs_slug=data["docs_slug"].strip(), status=status,
        extra={key: value for key, value in data.items() if key not in known}, **optional,
    )
    return feature, problems


def _entries(root: Any) -> Iterable[Any]:
    try:
        return sorted((entry for entry in root.iterdir() if entry.name.endswith(".json")),
                      key=lambda entry: entry.name)
    except (OSError, ValueError):  # the directory is not in this install
        return []


def load_catalog(root: Any = None, *, plugins: bool = False) -> Catalog:
    """Read every manifest. ``root`` is a directory (``pathlib.Path`` or any
    ``importlib.resources`` traversable); by default the packaged ``briefcase/_features``.
    With ``plugins``, manifests that installed packages ship under the ``briefcase.features``
    entry point group are added (a built-in id always wins); each carries
    ``extra["plugin"]`` naming its distribution."""
    catalog = Catalog()
    if root is None:
        from importlib import resources

        try:
            root = resources.files(PACKAGE).joinpath(DIRECTORY)
        except (ImportError, TypeError, ValueError) as exc:  # pragma: no cover - broken install
            catalog.problems.append(f"{DIRECTORY}: cannot locate the packaged manifests ({exc})")
            return catalog
    seen: Dict[str, str] = {}
    for entry in _entries(root):
        source = f"{DIRECTORY}/{entry.name}"
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            catalog.problems.append(f"{source}: skipped, not readable as JSON ({exc})")
            continue
        feature, problems = parse_feature(data, source)
        catalog.problems.extend(problems)
        if feature is None:
            continue
        if feature.id in seen:
            catalog.problems.append(f"{source}: skipped, id {feature.id!r} is already used by {seen[feature.id]}")
            continue
        if feature.id != entry.name[: -len(".json")]:
            catalog.problems.append(f"{source}: file name does not match id {feature.id!r}")
        seen[feature.id] = source
        catalog.features.append(feature)
    if plugins:
        _add_plugin_features(catalog, seen)
    catalog.features.sort(key=lambda item: (VERBS.index(item.verb), item.title.lower()))
    for problem in catalog.problems:
        logger.warning("feature manifest: %s", problem)
    return catalog


def _add_plugin_features(catalog: Catalog, seen: Dict[str, str]) -> None:
    from briefcase import plugins as _plugins

    group = "briefcase.features"
    for name, obj in _plugins.get_all(group, list(seen)).items():
        plugin = _plugins.find(group, name)
        dist = plugin.dist if plugin is not None and plugin.dist else "unknown"
        source = f"plugin {dist} ({name})"
        try:
            data = obj() if callable(obj) else obj
            if isinstance(data, str):
                data = json.loads(data)
        except Exception as exc:  # noqa: BLE001 - a broken plugin manifest is skipped
            catalog.problems.append(f"{source}: skipped, manifest could not be read ({exc})")
            continue
        feature, problems = parse_feature(data, source)
        catalog.problems.extend(problems)
        if feature is None:
            continue
        if feature.id in seen:
            catalog.problems.append(f"{source}: skipped, id {feature.id!r} is already used by {seen[feature.id]}")
            continue
        seen[feature.id] = source
        feature.extra["plugin"] = dist
        catalog.features.append(feature)


__all__ = ["Catalog", "Feature", "VERBS", "VERB_TITLES", "load_catalog", "parse_feature"]
