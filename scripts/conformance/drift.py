"""Docs-drift check: does each adapter's ``docs_url`` still mention the strings we depend on?

Expectations live in ``briefcase/connect/conformance.json`` keyed by adapter id. A page that
cannot be fetched (network, proxy, HTTP error) yields ``unknown``, never a failure. Only a page
that WAS fetched and no longer contains an expected string reports ``vanished``.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .core import REPO_ROOT, today

CONFORMANCE_JSON = REPO_ROOT / "briefcase" / "connect" / "conformance.json"
ADAPTERS_JSON = REPO_ROOT / "briefcase" / "connect" / "adapters.json"
USER_AGENT = "briefcase-conformance/1 (+docs drift check)"

Fetcher = Callable[[str], Optional[str]]


def fetch(url: str, timeout: float = 30.0) -> Optional[str]:
    """Page text, or ``None`` when it cannot be fetched."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, ValueError):
        return None


def normalise(text: str) -> str:
    """Undo HTML/JSON escaping so `&quot;`, `\\u003c` and `&lt;` forms of a string still match."""
    text = html.unescape(text)
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    return text.replace("\\/", "/").replace('\\"', '"')


def check_adapter(adapter_id: str, entry: Dict[str, Any], url: str, fetcher: Fetcher) -> Dict[str, Any]:
    expected: List[str] = list(entry.get("expect", []))
    result: Dict[str, Any] = {"adapter": adapter_id, "url": url, "checked": today(), "expected": expected}
    if not expected:
        result.update(status="no-expectations", missing=[], found=[], note=entry.get("note", ""))
        return result
    page = fetcher(url)
    if page is None:
        result.update(status="unknown", missing=[], found=[], note="page could not be fetched; not a failure")
        return result
    page = normalise(page)
    found = [s for s in expected if s in page]
    missing = [s for s in expected if s not in page]
    result.update(status="vanished" if missing else "ok", found=found, missing=missing)
    return result


def check_all(only: Optional[List[str]] = None, fetcher: Optional[Fetcher] = None,
              offline_dir: Optional[Path] = None, expectations: Optional[Dict[str, Any]] = None,
              adapters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    expectations = expectations if expectations is not None else json.loads(CONFORMANCE_JSON.read_text())
    adapters = adapters if adapters is not None else json.loads(ADAPTERS_JSON.read_text())["tools"]
    pages = {p.stem: p.read_text(encoding="utf-8") for p in offline_dir.glob("*.html")} if offline_dir else None
    fetcher = fetcher or fetch
    results = []
    for adapter_id, data in sorted(adapters.items()):
        if only and adapter_id not in only:
            continue
        entry = expectations.get("adapters", {}).get(adapter_id, {})
        url = entry.get("url") or data["docs_url"]
        page_fetcher = (lambda _u, _i=adapter_id: pages.get(_i)) if pages is not None else fetcher
        results.append(check_adapter(adapter_id, entry, url, page_fetcher))
    summary = {key: sum(1 for r in results if r["status"] == key)
               for key in ("ok", "vanished", "unknown", "no-expectations")}
    return {"schema_version": 1, "checked": today(), "results": results, "summary": summary}


def render(report: Dict[str, Any]) -> str:
    lines = ["Docs drift report (" + report["checked"] + ")", ""]
    for r in report["results"]:
        marks = {"ok": "ok      ", "vanished": "VANISHED", "unknown": "unknown ", "no-expectations": "-       "}
        mark = marks[r["status"]]
        lines.append(f"{mark} {r['adapter']:<16} {r['url']}")
        for missing in r.get("missing", []):
            lines.append(f"           no longer on the page: {missing!r}")
        if r["status"] == "unknown":
            lines.append("           " + r["note"])
        if r["status"] == "no-expectations" and r.get("note"):
            lines.append("           " + r["note"])
    s = report["summary"]
    lines += ["", f"{s['ok']} ok, {s['vanished']} with vanished expectations, {s['unknown']} unknown (network), "
                  f"{s['no-expectations']} without expectations"]
    return "\n".join(lines)
