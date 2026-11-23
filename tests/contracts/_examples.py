"""Deterministic example values generated from API schemas.

:class:`JsonSchemaExamples` walks a JSON Schema (OpenAPI 3.1 component,
pydantic-generated schema, converted discovery document) and
:class:`ShapeExamples` a botocore shape. Both fill every declared property,
so a response carries every field the spec defines, and give each integer a
distinct value (1001, 1002, ...): a test can then tell from the record
exactly which response field a value came from, and a swapped or dropped
counter shows up.

Unions (``anyOf`` / ``oneOf``, botocore unions) take the first non-null
variant unless ``prefer`` names a discriminator value to pick: a variant
whose ``type`` property is the constant ``"message"`` is chosen by
``prefer=("message",)``. Booleans are ``False`` (flags off), strings are
``"<field>-<n>"`` unless an enum, const or format says otherwise.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

Path = Tuple[str, ...]


class _Counter:
    def __init__(self, start: int = 1001) -> None:
        self.value = start - 1

    def next(self) -> int:
        self.value += 1
        return self.value


class JsonSchemaExamples:
    """Examples from a JSON Schema document (``doc``; ``$ref`` values are
    local JSON pointers into it)."""

    def __init__(self, doc: Any, *, prefer: Sequence[str] = (),
                 choose: Optional[Callable[[Path, List[Any]], Optional[int]]] = None,
                 max_depth: int = 60, start: int = 1001) -> None:
        self.doc = doc
        self.prefer = tuple(prefer)
        self.choose = choose
        self.max_depth = max_depth
        self.counter = _Counter(start)

    # -- references ---------------------------------------------------------

    def resolve(self, ref: str) -> Any:
        node = self.doc
        for part in ref.split("#", 1)[-1].lstrip("/").split("/"):
            if part:
                node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def _deref(self, node: Any, seen: int = 0) -> Any:
        while isinstance(node, dict) and "$ref" in node and seen < 50:
            target = self.resolve(node["$ref"])
            rest = {k: v for k, v in node.items() if k != "$ref"}
            node = dict(target, **rest) if rest else target
            seen += 1
        return node

    def discriminator(self, node: Any) -> Optional[Any]:
        """The constant ``type`` (or ``event``) of an object schema, if any."""
        node = self._deref(node)
        if not isinstance(node, dict):
            return None
        for part in node.get("allOf", ()):
            found = self.discriminator(part)
            if found is not None:
                return found
        props = node.get("properties") or {}
        for key in ("type", "event"):
            prop = self._deref(props.get(key)) if key in props else None
            if isinstance(prop, dict):
                if "const" in prop:
                    return prop["const"]
                enum = prop.get("enum")
                if isinstance(enum, list) and len(enum) == 1:
                    return enum[0]
        return None

    # -- generation ---------------------------------------------------------

    def generate(self, schema: Any, path: Path = ()) -> Any:
        return self._gen(schema, path, (), 0)

    def _pick(self, variants: List[Any], path: Path) -> Any:
        if self.choose is not None:
            index = self.choose(path, variants)
            if index is not None:
                return variants[index]
        for wanted in self.prefer:
            for variant in variants:
                if self.discriminator(variant) == wanted:
                    return variant
        for variant in variants:
            node = self._deref(variant)
            if not (isinstance(node, dict) and node.get("type") == "null"):
                return variant
        return variants[0]

    def _gen(self, node: Any, path: Path, refs: Tuple[str, ...], depth: int) -> Any:
        if node is True or node is None or node == {}:
            return f"{path[-1] if path else 'value'}-{self.counter.next()}"
        if not isinstance(node, dict):
            return None
        if "$ref" in node:
            ref = node["$ref"]
            if refs.count(ref) >= 1 and depth > 2 or depth > self.max_depth:
                return self._minimal(self._deref(node))
            target = self.resolve(ref)
            rest = {k: v for k, v in node.items() if k not in ("$ref",)}
            merged = dict(target, **rest) if rest else target
            return self._gen(merged, path, refs + (ref,), depth + 1)
        if "const" in node:
            return node["const"]
        if "enum" in node and node["enum"]:
            non_null = [v for v in node["enum"] if v is not None]
            return non_null[0] if non_null else None
        if "allOf" in node:
            out: Dict[str, Any] = {}
            scalar: Any = None
            for part in node["allOf"]:
                value = self._gen(part, path, refs, depth + 1)
                if isinstance(value, dict):
                    out.update(value)
                else:
                    scalar = value
            rest = {k: v for k, v in node.items() if k != "allOf"}
            if rest.get("properties"):
                extra = self._gen(rest, path, refs, depth + 1)
                if isinstance(extra, dict):
                    out.update(extra)
            return out if out or scalar is None else scalar
        for key in ("oneOf", "anyOf"):
            if key in node:
                variant = self._pick(list(node[key]), path)
                rest = {k: v for k, v in node.items() if k not in (key, "discriminator")}
                if rest.get("properties") or rest.get("required"):
                    base = self._gen(rest, path, refs, depth + 1)
                    value = self._gen(variant, path, refs, depth + 1)
                    if isinstance(base, dict) and isinstance(value, dict):
                        base.update(value)
                        return base
                    return value
                return self._gen(variant, path, refs, depth + 1)
        kind = node.get("type")
        if isinstance(kind, list):
            non_null = [k for k in kind if k != "null"]
            kind = non_null[0] if non_null else "null"
        if kind is None:
            if "properties" in node or "additionalProperties" in node:
                kind = "object"
            elif "items" in node:
                kind = "array"
        if kind == "object":
            out = {}
            for name, prop in (node.get("properties") or {}).items():
                out[name] = self._gen(prop, path + (name,), refs, depth + 1)
            return out
        if kind == "array":
            count = max(1, int(node.get("minItems") or 0))
            if node.get("maxItems") == 0:
                return []
            return [self._gen(node.get("items", {}), path + ("0",), refs, depth + 1)
                    for _ in range(count)]
        if kind == "string":
            return self._string(node, path)
        if kind == "integer":
            return self._int(node)
        if kind == "number":
            return self._number(node)
        if kind == "boolean":
            return False
        if kind == "null":
            return None
        return f"{path[-1] if path else 'value'}-{self.counter.next()}"

    def _int(self, node: Dict[str, Any]) -> int:
        value = self.counter.next()
        maximum = node.get("maximum")
        minimum = node.get("minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            value = int(maximum)
        if isinstance(minimum, (int, float)) and value < minimum:
            value = int(minimum)
        return value

    def _number(self, node: Dict[str, Any]) -> float:
        value = self.counter.next() + 0.5
        maximum = node.get("maximum")
        minimum = node.get("minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            value = float(maximum)
        if isinstance(minimum, (int, float)) and value < minimum:
            value = float(minimum)
        return value

    def _string(self, node: Dict[str, Any], path: Path) -> str:
        fmt = node.get("format")
        n = self.counter.next()
        if fmt in ("date-time", "google-datetime"):
            return f"2026-01-01T00:00:{n % 60:02d}Z"
        if fmt == "byte":
            import base64
            return base64.b64encode(f"{path[-1] if path else 'bytes'}-{n}".encode()).decode()
        if fmt == "google-duration":
            return f"{n}.5s"
        if fmt == "date":
            return "2026-01-01"
        if fmt in ("uri", "url"):
            return f"https://example.com/{n}"
        text = f"{path[-1] if path else 'string'}-{n}"
        min_length = node.get("minLength")
        if isinstance(min_length, int) and len(text) < min_length:
            text = text.ljust(min_length, "x")
        return text

    def _minimal(self, node: Any) -> Any:
        """A value that ends a recursion: null, or an empty container."""
        if not isinstance(node, dict):
            return None
        kind = node.get("type")
        if isinstance(kind, list):
            return None if "null" in kind else self._minimal(dict(node, type=kind[0]))
        if kind == "array" or "items" in node:
            return []
        if kind == "string":
            return "x"
        if kind in ("integer", "number"):
            return 0
        for key in ("anyOf", "oneOf"):
            for variant in node.get(key, ()):
                if self._deref(variant).get("type") == "null":
                    return None
        return {}


class ShapeExamples:
    """Examples from a botocore shape: every member of a structure, one
    item per list, one variant per union (``prefer`` names members)."""

    def __init__(self, *, prefer: Iterable[str] = (), start: int = 1001, max_depth: int = 12) -> None:
        self.prefer = tuple(prefer)
        self.counter = _Counter(start)
        self.max_depth = max_depth

    def generate(self, shape: Any, path: Path = (), depth: int = 0) -> Any:
        kind = shape.type_name
        name = path[-1] if path else shape.name
        if depth > self.max_depth:
            return {} if kind == "structure" else [] if kind == "list" else None
        if kind == "structure":
            members = shape.members
            if shape.metadata.get("union") or shape.serialization.get("eventstream"):
                chosen = next((m for m in self.prefer if m in members), next(iter(members)))
                return {chosen: self.generate(members[chosen], path + (chosen,), depth + 1)}
            out = {}
            for member, child in members.items():
                if member.startswith("__"):
                    continue
                if child.type_name == "structure" and child.serialization.get("eventstream"):
                    continue
                out[member] = self.generate(child, path + (member,), depth + 1)
            return out
        if kind == "list":
            return [self.generate(shape.member, path + ("0",), depth + 1)]
        if kind == "map":
            return {f"{name}-key": self.generate(shape.value, path + ("value",), depth + 1)}
        if kind == "string":
            if shape.enum:
                return shape.enum[0]
            return f"{name}-{self.counter.next()}"
        if kind in ("integer", "long"):
            value = self.counter.next()
            maximum = shape.metadata.get("max")
            return min(value, maximum) if isinstance(maximum, int) else value
        if kind in ("float", "double"):
            return self.counter.next() + 0.5
        if kind == "boolean":
            return False
        if kind == "blob":
            return f"{name}-{self.counter.next()}".encode()
        if kind == "timestamp":
            return datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        if kind == "document":
            return {}
        return None


def int_leaves(value: Any, path: Path = ()) -> List[Tuple[Path, int]]:
    """``(path, int)`` for every integer (not bool) inside ``value``."""
    out: List[Tuple[Path, int]] = []
    if isinstance(value, bool):
        return out
    if isinstance(value, int):
        return [(path, value)]
    if isinstance(value, dict):
        for key, item in value.items():
            out.extend(int_leaves(item, path + (str(key),)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.extend(int_leaves(item, path + (str(index),)))
    return out
