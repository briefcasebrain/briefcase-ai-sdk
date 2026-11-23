"""Edit a JSON document as text, so every byte we do not own stays exactly as it was.

``json.dumps`` of a loaded document reformats everything (indentation, escapes, number spelling); this module finds
the span of each value in the original text and inserts or removes only our member, with its comma. Used by
:mod:`briefcase.connect.edits` for every JSON write and undo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

_WS = " \t\r\n"
_SCALAR_END = re.compile(r'[,\]}\s]')


@dataclass
class Member:
    key: str
    start: int  # offset of the key's opening quote
    value: "Node"


@dataclass
class Node:
    kind: str  # "object", "array" or "scalar"
    start: int
    end: int  # one past the closing brace, bracket or last character
    members: List[Member] = field(default_factory=list)
    items: List["Node"] = field(default_factory=list)


class _Parser:
    def __init__(self, text: str) -> None:
        self.t = text
        self.i = 0

    def ws(self) -> None:
        while self.i < len(self.t) and self.t[self.i] in _WS:
            self.i += 1

    def string(self) -> str:
        start = self.i
        self.i += 1
        while self.i < len(self.t):
            c = self.t[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == '"':
                self.i += 1
                return json.loads(self.t[start:self.i])
            self.i += 1
        raise ValueError("unterminated string")

    def value(self) -> Node:
        self.ws()
        if self.i >= len(self.t):
            raise ValueError("unexpected end")
        c = self.t[self.i]
        if c == "{":
            node = Node("object", self.i, self.i)
            self.i += 1
            self.ws()
            if self.t[self.i:self.i + 1] == "}":
                self.i += 1
                node.end = self.i
                return node
            while True:
                self.ws()
                if self.t[self.i:self.i + 1] != '"':
                    raise ValueError("expected a key")
                kstart = self.i
                key = self.string()
                self.ws()
                if self.t[self.i:self.i + 1] != ":":
                    raise ValueError("expected a colon")
                self.i += 1
                node.members.append(Member(key, kstart, self.value()))
                self.ws()
                c = self.t[self.i:self.i + 1]
                self.i += 1
                if c == "}":
                    node.end = self.i
                    return node
                if c != ",":
                    raise ValueError("expected a comma")
        if c == "[":
            node = Node("array", self.i, self.i)
            self.i += 1
            self.ws()
            if self.t[self.i:self.i + 1] == "]":
                self.i += 1
                node.end = self.i
                return node
            while True:
                node.items.append(self.value())
                self.ws()
                c = self.t[self.i:self.i + 1]
                self.i += 1
                if c == "]":
                    node.end = self.i
                    return node
                if c != ",":
                    raise ValueError("expected a comma")
        start = self.i
        if c == '"':
            self.string()
        else:
            match = _SCALAR_END.search(self.t, self.i)
            self.i = match.start() if match else len(self.t)
            if self.i == start:
                raise ValueError("unexpected character")
        return Node("scalar", start, self.i)


def parse(text: str) -> Node:
    parser = _Parser(text)
    root = parser.value()
    parser.ws()
    if parser.i != len(text):
        raise ValueError("trailing data")
    return root


def find(root: Node, path: Sequence[str]) -> Optional[Node]:
    node: Optional[Node] = root
    for key in path:
        if node is None or node.kind != "object":
            return None
        node = next((m.value for m in node.members if m.key == key), None)
    return node


def _line_indent(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[start:end]


def _unit(text: str) -> Optional[int]:
    """The indentation step of ``text`` (``None`` for compact, single-line JSON)."""
    match = re.search(r"\n([ \t]+)\S", text)
    if not match:
        return None
    return 1 if match.group(1).startswith("\t") else len(match.group(1))


def _render(value: Any, unit: Optional[int], indent: str) -> str:
    text = json.dumps(value, indent=unit, ensure_ascii=False)
    return text.replace("\n", "\n" + indent) if unit else text


def _multiline(text: str, node: Node, first: int) -> bool:
    return "\n" in text[node.start:first]


def insert_member(text: str, obj: Node, key: str, value: Any) -> str:
    unit = _unit(text)
    base = _line_indent(text, obj.start)
    pad = base + (("\t" if "\t" in base else " " * unit) if unit else "")
    member = f"{json.dumps(key, ensure_ascii=False)}: {_render(value, unit, pad)}"
    if not obj.members:
        if unit and "\n" in text[:obj.start] + text[obj.end:]:
            return text[:obj.start + 1] + "\n" + pad + member + "\n" + base + text[obj.end - 1:]
        return text[:obj.start + 1] + member + text[obj.end - 1:]
    last = obj.members[-1]
    if _multiline(text, obj, obj.members[0].start):
        pad = _line_indent(text, obj.members[0].start)
        member = f"{json.dumps(key, ensure_ascii=False)}: {_render(value, unit, pad)}"
        return text[:last.value.end] + ",\n" + pad + member + text[last.value.end:]
    return text[:last.value.end] + ", " + member + text[last.value.end:]


def append_item(text: str, arr: Node, value: Any) -> str:
    unit = _unit(text)
    base = _line_indent(text, arr.start)
    pad = base + (("\t" if "\t" in base else " " * unit) if unit else "")
    if not arr.items:
        item = _render(value, unit, pad)
        if unit and "\n" in text[:arr.start] + text[arr.end:]:
            return text[:arr.start + 1] + "\n" + pad + item + "\n" + base + text[arr.end - 1:]
        return text[:arr.start + 1] + item + text[arr.end - 1:]
    last = arr.items[-1]
    if _multiline(text, arr, arr.items[0].start):
        pad = _line_indent(text, arr.items[0].start)
        return text[:last.end] + ",\n" + pad + _render(value, unit, pad) + text[last.end:]
    return text[:last.end] + ", " + _render(value, unit, pad) + text[last.end:]


def _remove_span(text: str, container: Node, index: int, spans: List[tuple]) -> str:
    """Remove the ``index``-th child (``spans`` = [(start, end)] of every child) with its comma."""
    start, end = spans[index]
    if index + 1 < len(spans):
        return text[:start] + text[spans[index + 1][0]:]
    if index > 0:
        return text[:spans[index - 1][1]] + text[end:]
    return text[:container.start + 1] + text[container.end - 1:]


def remove_member(text: str, obj: Node, index: int) -> str:
    return _remove_span(text, obj, index, [(m.start, m.value.end) for m in obj.members])


def remove_item(text: str, arr: Node, index: int) -> str:
    return _remove_span(text, arr, index, [(n.start, n.end) for n in arr.items])
