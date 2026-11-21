"""Scrub captured payloads before they are saved into the fixture corpus.

Removes e-mail addresses, absolute host paths, API keys and tokens, UUIDs (account, organisation
and session identifiers), IP addresses other than loopback, and the values of identity-looking
attributes. Replacements are stable inside one scrub run (the same UUID always becomes the same
placeholder) so records that refer to each other still line up.

``findings(text)`` is the independent check: it reports anything that still looks like
a secret or an identifier. ``scrub_json`` raises if findings remain.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
UNIX_PATH = re.compile(r"(?<![\w:/.\-])/(?:home|Users|root|tmp|var|opt|private|mnt|workspaces?|usr|etc|srv|run)"
                       r"(?:/[^\s\"'<>,;)\]}]*)?")
WIN_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\s\"'<>,;)\]}\\]+\\?)+")
TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),   # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{12,}=*"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),                                           # long hex (hashes, keys)
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),                                        # long opaque strings
]
IPV4 = re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b")
# attribute keys whose value is an identity, whatever it looks like
IDENTITY_KEY = re.compile(r"(?i)(email|account|organi[sz]ation|user\.id|device|host\.name|hostname|"
                          r"token|secret|password|api[_.-]?key|authorization|machine|workspace\.host_paths|cwd)")
# resource values such as ``service.version`` or ``os.type`` are not identities and are kept.


class Scrubber:
    def __init__(self) -> None:
        self._uuids: Dict[str, str] = {}
        self._emails: Dict[str, str] = {}
        self._traces: Dict[str, str] = {}

    def text(self, value: str) -> str:
        value = EMAIL.sub(lambda m: self._emails.setdefault(m.group(0).lower(),
                                                            f"user{len(self._emails) + 1}@example.invalid"), value)
        value = UUID.sub(lambda m: self._uuids.setdefault(
            m.group(0).lower(), f"00000000-0000-4000-8000-{len(self._uuids) + 1:012d}"), value)
        for pattern in TOKEN_PATTERNS:
            value = pattern.sub(lambda m: m.group(0) if m.group(0).startswith("0000000000000000")
                                else "<redacted-token>", value)
        value = UNIX_PATH.sub("<path>", value)
        value = WIN_PATH.sub("<path>", value)
        value = IPV4.sub("<ip>", value)
        return value

    def _trace_id(self, value: str) -> str:
        width = len(value)
        return self._traces.setdefault(value, f"{len(self._traces) + 1:0{width}x}")

    def obj(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            # OTLP trace/span ids are random, not identities; keep them linkable but not real
            for trace_key in ("traceId", "spanId", "parentSpanId"):
                if isinstance(value.get(trace_key), str) and value[trace_key]:
                    value = dict(value)
                    value[trace_key] = self._trace_id(value[trace_key])
            # OTLP attribute: {"key": "...", "value": {"stringValue": ...}}
            if "key" in value and "value" in value and isinstance(value["key"], str):
                name = value["key"]
                inner = value["value"]
                if IDENTITY_KEY.search(name):
                    return {"key": name, "value": self._blank(inner)}
                return {"key": name, "value": self.obj(inner, name)}
            return {k: self.obj(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.obj(v, key) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def _blank(self, inner: Any) -> Any:
        if not isinstance(inner, dict):
            return "<scrubbed>"
        out: Dict[str, Any] = {}
        for name, v in inner.items():
            if name == "arrayValue" and isinstance(v, dict):
                out[name] = {"values": [self._blank(x) for x in v.get("values", [])]}
            elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out[name] = "<scrubbed>"
            else:
                out[name] = v
        return out


def findings(text: str) -> List[Tuple[str, str]]:
    """Strings in ``text`` that still look like an e-mail, path, token, UUID or non-loopback IP."""
    found: List[Tuple[str, str]] = []
    for label, pattern in (("email", EMAIL), ("uuid", UUID), ("unix-path", UNIX_PATH), ("windows-path", WIN_PATH),
                           ("ip", IPV4)):
        for match in pattern.finditer(text):
            if label == "uuid" and match.group(0).startswith("00000000-0000-4000-8000-"):
                continue
            if label == "email" and match.group(0).endswith("@example.invalid"):
                continue
            found.append((label, match.group(0)))
    for pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            if match.group(0) != "<redacted-token>" and not match.group(0).startswith("0000000000000000"):
                found.append(("token", match.group(0)))
    return found


def scrub_json(data: Any) -> Any:
    cleaned = Scrubber().obj(data)
    left = findings(json.dumps(cleaned))
    if left:
        raise ValueError(f"scrubber left {len(left)} sensitive-looking string(s): {left[:3]}")
    return cleaned


def scrub_file(source: str, dest: str) -> None:
    with open(source, encoding="utf-8") as handle:
        data = json.load(handle)
    with open(dest, "w", encoding="utf-8") as handle:
        json.dump(scrub_json(data), handle, indent=2, sort_keys=True)
        handle.write("\n")
