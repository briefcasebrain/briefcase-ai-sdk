"""What the nightly conformance run last learned about each tool.

``conformance-report.json`` ships with the package: for each tool, the level reached
(``docs-only`` < ``config-accepted`` < ``telemetry-verified``), the tool version it was
run against and the date. ``briefcase doctor --connect`` compares that with the version
installed here. The harness that produces the report lives in ``scripts/conformance``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from briefcase.connect.adapters import parse_version

REPORT_FILE = Path(__file__).with_name("conformance-report.json")
LEVELS = ("docs-only", "config-accepted", "telemetry-verified")


def load_report(path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        data = json.loads((path or REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tools": {}}
    return data if isinstance(data, dict) and isinstance(data.get("tools"), dict) else {"tools": {}}


def describe(name: str, installed: Optional[str], entry: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """``(severity, detail)`` for one tool: its last known level against the installed version."""
    if not entry or entry.get("level") not in LEVELS:
        return "ok", "no conformance data shipped for this tool"
    level, tested, date = entry["level"], entry.get("version"), entry.get("date") or "an unknown date"
    if level == "docs-only" or not tested:
        return "ok", f"{level}: built from documentation, never run against a real install"
    head = f"{name} {tested}: {level} on {date}"
    have, want = parse_version(installed), parse_version(tested)
    if have is None:
        return "warn", f"{head}; could not determine your installed version"
    if want is None:
        return "warn", f"{head}; the tested version is not a version number"
    if have == want:
        return "ok", f"{head}; your version {installed} matches"
    if have > want:
        return "warn", f"{head}; your version {installed} is newer, untested"
    return "ok", f"{head}; your version {installed} is older than the tested one"


def rows(installed: Iterable[Tuple[str, str, Optional[str]]], report: Optional[Dict[str, Any]] = None
         ) -> List[Tuple[str, str, str]]:
    """``(severity, label, detail)`` rows for ``(tool_id, display_name, installed_version)`` triples."""
    report = report if report is not None else load_report()
    out = []
    for tool_id, name, version in installed:
        sev, detail = describe(name, version, report.get("tools", {}).get(tool_id))
        out.append((sev, f"conformance {tool_id}", detail))
    return out
