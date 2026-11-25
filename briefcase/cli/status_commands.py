"""``briefcase-ai status``: one screen answering what is on, what is off, and what to do next.

    briefcase-ai status
    briefcase-ai status --json

The screen is made of rows. Four are built in: ``connect`` (coding tools set up by
``briefcase-ai connect``), ``receive`` (the local receiver), ``mcp`` (the MCP server
registration) and ``records`` (where records go). Every other row comes from a command
module's optional ``status()`` hook; the protocol is documented in
``briefcase/cli/_discovery.py`` and ``docs/features/README.md``.

The command only reads: the connect journal, the receiver's pidfile and ``/healthz``, and
the environment. One exception is inherited from the SDK: resolving
``BRIEFCASE_EXPORTER=<dir>/runs.jsonl`` creates ``<dir>`` if it is missing, exactly as the
first ``briefcase.observe()`` in a program would.

It ends with exactly one ``Next:`` line, picked by ``choose_next`` from a fixed priority
list. ``--json`` prints the same data (``items``, ``summary``, ``next``) and nothing else.
Exit code is 0 whatever the states are; use ``briefcase-ai doctor --connect --strict`` to gate
a script on problems.
"""
from __future__ import annotations

import argparse
import os
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from briefcase.cli import _discovery, _ux

GROUP = "start"
ORDER = 20

CORE_IDS = ("connect", "receive", "mcp", "records")
_TITLES = {"connect": "Coding tools", "receive": "Receiver", "mcp": "MCP server", "records": "Records"}
_GROUPS = {"connect": "connect", "receive": "connect", "mcp": "connect", "records": "capture"}
RECEIVER_EXTRA = "pip install 'briefcase-ai[receiver]'"


def register(sub) -> None:
    p = sub.add_parser(
        "status",
        help="What is on, what is off, and what to do next.",
        description="One screen: connected coding tools, the receiver, where records go, the MCP "
                    "server, and any feature that reports its own state. Ends with one suggested "
                    "next command. Read-only.",
        epilog="example:\n  briefcase-ai status --json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ux.add_json_flag(p)
    p.set_defaults(func=cmd_status)


# --- helpers ------------------------------------------------------------------

def briefcase_home() -> Path:
    override = os.environ.get("BRIEFCASE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".briefcase"


def _short(path: Any) -> str:
    """A path with the home directory written as ``~``."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if home not in ("", "/") and text.startswith(home) else text


def _item(identifier: str, state: str, detail: str, next_command: Optional[str] = None,
          data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"id": identifier, "title": _TITLES[identifier], "group": _GROUPS[identifier],
            "state": state, "detail": detail, "next": next_command, "source": "core",
            "data": data or {}}


def _names(rows: List[Dict[str, Any]]) -> str:
    return ", ".join(row["name"] for row in rows)


def _is_mcp(evidence: str) -> bool:
    return "mcp" in evidence.lower()


def _split_checks(row: Dict[str, Any]):
    """``(telemetry_checks, mcp_checks)`` of one ``briefcase.connect.engine.status`` row.
    The journal records the keys connect wrote, not which half they belong to, so the MCP
    ones are told apart by name (``mcp.json``, ``mcpServers``, ``claude mcp add``)."""
    telemetry = [(ok, what) for ok, what in row["checks"] if not _is_mcp(what)]
    mcp = [(ok, what) for ok, what in row["checks"] if _is_mcp(what)]
    return telemetry, mcp


# --- built-in rows ------------------------------------------------------------

def connect_rows() -> List[Dict[str, Any]]:
    from briefcase.connect import edits, engine
    from briefcase.connect.adapters import registry

    adapters = registry()
    try:
        rows = engine.status(adapters)
    except edits.LegacyJournal:
        return []
    for row in rows:
        row["needs_protobuf"] = bool(getattr(adapters[row["tool"]], "needs_protobuf", False))
    return rows


def tools_item(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    connected, drifted = [], []
    for row in rows:
        telemetry, mcp = _split_checks(row)
        commands = [c for c in row["commands"] if not _is_mcp(" ".join(c.get("argv", [])))]
        mcp_only = bool(mcp or row["commands"]) and not telemetry and not commands
        if not row["connected"] or mcp_only:
            continue
        connected.append(row)
        if any(not ok for ok, _ in telemetry):
            drifted.append(row)
    detected = [row for row in rows if row["detected"] and row not in connected]
    content = [row for row in connected if row["settings"].get("capture_content")]
    data = {
        "connected": [row["tool"] for row in connected],
        "drifted": [row["tool"] for row in drifted],
        "detected_not_connected": [row["tool"] for row in detected],
        "capture_content": [row["tool"] for row in content],
        "endpoints": sorted({row["settings"].get("endpoint") for row in connected} - {None}),
    }
    if drifted:
        return _item("connect", "attention",
                     f"{_names(drifted)}: settings written by connect are no longer in place",
                     "briefcase-ai connect " + " ".join(row["tool"] for row in drifted), data)
    if connected:
        detail = f"{_names(connected)} connected"
        if content:
            detail += f"; prompt and tool content export is ON for {_names(content)}"
        if detected:
            detail += f"; detected but not connected: {_names(detected)}"
        return _item("connect", "on", detail, None, data)
    if detected:
        return _item("connect", "off", f"none connected; detected on this machine: {_names(detected)}",
                     "briefcase-ai connect", data)
    return _item("connect", "off", "none connected; no supported coding tool found on this machine", None, data)


def receiver_item(rows: List[Dict[str, Any]], tools: Dict[str, Any]) -> Dict[str, Any]:
    from briefcase.connect.adapters import DEFAULT_ENDPOINT
    from briefcase.receiver import daemon

    state = daemon.status()
    endpoints = list(tools["data"].get("endpoints") or [])
    health, endpoint, pid, sink = None, None, None, None
    if state.get("running"):
        health, endpoint = state.get("health") or {}, state.get("endpoint")
        pid, sink = state.get("pid"), state.get("to")
    else:
        # A receiver started in the foreground (or by another user) has no pidfile.
        for candidate in endpoints or [DEFAULT_ENDPOINT]:
            health = daemon.probe(candidate)
            if health is not None:
                endpoint = candidate
                break
    data: Dict[str, Any] = {"running": health is not None, "endpoint": endpoint, "pid": pid, "to": sink}
    if health is None:
        remote = [e for e in endpoints if e != DEFAULT_ENDPOINT]
        if remote and len(remote) == len(endpoints):
            return _item("receive", "off", "not running here; connected tools report to " + ", ".join(remote),
                         None, data)
        detail = "not running"
        if tools["state"] != "off":
            detail += "; connected tools have nowhere to send"
        return _item("receive", "off", detail, "briefcase-ai receive --detach", data)
    stats = health.get("stats") or {}
    data.update(stats={key: stats.get(key, 0) for key in ("requests", "records", "rejected")},
                redaction=health.get("redaction"), protobuf=bool(health.get("protobuf")))
    detail = f"running on {endpoint}" + (f" (pid {pid})" if pid else " (not started with --detach)")
    detail += (f"; {data['stats']['requests']} requests, {data['stats']['records']} records, "
               f"{data['stats']['rejected']} rejected")
    needy = [row for row in rows if row["tool"] in tools["data"]["connected"] and row.get("needs_protobuf")]
    if needy and not data["protobuf"]:
        return _item("receive", "attention",
                     detail + f"; {_names(needy)} send protobuf, which this receiver cannot decode "
                              "(restart it after installing the extra)",
                     RECEIVER_EXTRA, data)
    return _item("receive", "on", detail, None, data)


def mcp_item(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    registered, drifted = [], []
    for row in rows:
        _, mcp = _split_checks(row)
        commands = [c for c in row["commands"] if _is_mcp(" ".join(c.get("argv", [])))]
        if not row["connected"] or not row["settings"].get("mcp", bool(mcp or commands)):
            continue
        registered.append(row)
        if any(not ok for ok, _ in mcp):
            drifted.append(row)
    detected = [row for row in rows if row["detected"]]
    data = {"registered": [row["tool"] for row in registered], "drifted": [row["tool"] for row in drifted]}
    if drifted:
        return _item("mcp", "attention", f"{_names(drifted)}: the briefcase MCP entry is no longer in place",
                     "briefcase-ai mcp install " + " ".join(row["tool"] for row in drifted), data)
    if registered:
        return _item("mcp", "on", f"registered with {_names(registered)}", None, data)
    return _item("mcp", "off", "not registered with any tool", "briefcase-ai mcp install" if detected else None, data)


def sdk_destination(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Where ``briefcase.observe()`` with no argument (and so ``briefcase-ai exec``) sends records
    in this environment: ``{"kind", "target", "kept", "problem"}``.

    The answer comes from the SDK's own resolver in ``briefcase/_observe.py``; this function
    only names the exporter it returns. ``kept`` is false for the console and for memory,
    where nothing outlives the process.
    """
    import asyncio

    from briefcase import _observe

    result: Dict[str, Any] = {"kind": "unknown", "target": None, "kept": False, "problem": None}
    env = os.environ if env is None else env
    check = getattr(_observe, "check_environment", None)
    if check is not None:
        # The SDK's side-effect-free validation, where this version has it: a bad value is
        # reported without building an exporter.
        problem = check(env)
        if problem:
            result["problem"] = " ".join(str(problem).split())
            return result
    resolver = getattr(_observe, "_exporter_from_env", None)
    if resolver is None:  # pragma: no cover - the resolver was renamed
        result["problem"] = "this version of briefcase does not expose its destination resolver"
        return result
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            exporter = resolver(env)
        except Exception as exc:  # noqa: BLE001 - e.g. an unknown BRIEFCASE_EXPORTER value
            result["problem"] = " ".join(str(exc).split())
            return result
    name = type(exporter).__name__
    if name == "ConsoleExporter":
        result.update(kind="console", target="stderr")
    elif name == "MemoryExporter":
        result.update(kind="memory", target="in-process only")
    elif name == "_DisabledExporter":
        result.update(kind="disabled", target="OTEL_SDK_DISABLED is set")
    elif name == "JSONLFileExporter":
        result.update(kind="file", target=_short(getattr(exporter, "_path", "?")), kept=True)
    elif name == "HTTPExporter":
        result.update(kind="https", target=getattr(exporter, "_log_url", "?"), kept=True)
    elif name == "OTelExporter":
        result.update(kind="otlp", kept=True,
                      target=f"{getattr(exporter, '_endpoint', '?')} ({getattr(exporter, '_protocol', '?')})")
    else:
        result.update(kind=name, kept=True)
    if caught:
        result["problem"] = " ".join(str(caught[0].message).split())
    if result["kind"] not in ("console", "memory", "disabled"):
        try:  # release sockets and worker threads the exporter may have opened
            asyncio.run(exporter.close())
        except Exception:  # noqa: BLE001
            pass
    return result


def _file_stats(path: Path) -> Optional[Dict[str, Any]]:
    try:
        info = path.stat()
    except OSError:
        return None
    return {"bytes": info.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(info.st_mtime))}


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    size = float(count)
    for unit in ("KB", "MB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} GB"


def records_item(receiver: Dict[str, Any], env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    from briefcase.receiver import daemon

    parts: List[str] = []
    data: Dict[str, Any] = {"receiver": None, "sdk": None}
    kept = False
    sink = receiver["data"].get("to")
    stale = False
    if not sink and not receiver["data"].get("running") and daemon.default_sink().exists():
        sink, stale = str(daemon.default_sink()), True
    if sink or receiver["data"].get("running"):
        sink = sink or "console"
        stats = _file_stats(Path(sink)) if sink.endswith(".jsonl") else None
        data["receiver"] = {"to": sink, "file": stats, "receiving": not stale}
        text = f"coding tools -> {_short(sink)}"
        if stats:
            text += f" ({_size(stats['bytes'])}, last written {stats['modified'][:16].replace('T', ' ')})"
        if stale:
            text += ", receiver stopped"
        else:
            kept = sink != "console"
        parts.append(text)
    sdk = sdk_destination(env)
    data["sdk"] = sdk
    if sdk["kind"] == "unknown":
        parts.append("Python apps -> not resolved")
    else:
        label = {"console": "the console (stderr)", "memory": "memory (in-process only)",
                 "file": str(sdk["target"]), "https": str(sdk["target"]),
                 "disabled": "nowhere (OTEL_SDK_DISABLED is set)",
                 "otlp": f"OTLP {sdk['target']}"}.get(sdk["kind"], sdk["kind"])
        text = f"Python apps -> {label}"
        if sdk["kind"] == "file":
            stats = _file_stats(Path(os.path.expanduser(sdk["target"])))
            data["sdk"]["file"] = stats
            if stats:
                text += f" ({_size(stats['bytes'])})"
        parts.append(text if sdk["kept"] else text + ", not kept")
        kept = kept or sdk["kept"]
    if sdk["problem"]:
        return _item("records", "attention", "; ".join(parts) + f". {sdk['problem']}", None, data)
    return _item("records", "on" if kept else "off", "; ".join(parts), None, data)


# --- the report ---------------------------------------------------------------

def _guarded(identifier: str, build: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - one unreadable piece must not hide the rest
        if _ux.debug_enabled():
            raise
        return _item(identifier, "attention", f"could not be checked ({type(exc).__name__}: {exc})",
                     "briefcase-ai --debug status", {})


def collect(modules: Optional[List[_discovery.CommandModule]] = None,
            env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Build the status report. ``modules`` are the discovered command modules whose
    ``status()`` hooks should be run (default: discover them now)."""
    try:
        rows = connect_rows()
        rows_error: Optional[Exception] = None
    except Exception as exc:  # noqa: BLE001
        if _ux.debug_enabled():
            raise
        rows, rows_error = [], exc

    def tools() -> Dict[str, Any]:
        if rows_error is not None:
            raise rows_error
        return tools_item(rows)

    connect = _guarded("connect", tools)
    connect["data"].setdefault("connected", [])
    receive = _guarded("receive", lambda: receiver_item(rows, connect))
    mcp = _guarded("mcp", lambda: mcp_item(rows))
    records = _guarded("records", lambda: records_item(receive, env))
    items = [connect, receive, mcp, records]

    modules = _discovery.discover() if modules is None else modules
    hooks, problems = _discovery.run_status_hooks(modules)
    for row in hooks:
        if row["id"] in CORE_IDS:
            problems.append(f"warning: {row['source']}.status() uses the reserved id {row['id']!r}")
            continue
        items.append(row)

    order = {key: index for index, key in enumerate(_discovery.GROUP_KEYS)}
    items.sort(key=lambda item: order.get(item["group"], len(order)))
    summary = {state: sum(1 for item in items if item["state"] == state)
               for state in ("on", "off", "attention")}
    import briefcase

    return {
        "schema_version": 1,
        "version": briefcase.__version__,
        "home": str(briefcase_home()),
        "items": items,
        "summary": summary,
        "next": choose_next(items),
        "problems": problems,
    }


def choose_next(items: List[Dict[str, Any]]) -> Dict[str, str]:
    """The single suggestion `briefcase-ai status` ends with. First match wins:

    1. a connected tool lost its settings          -> ``briefcase-ai connect <tool>``
    2. nothing connected, a tool is installed      -> ``briefcase-ai connect``
    3. tools connected, the local receiver is down -> ``briefcase-ai receive --detach``
    4. the receiver cannot decode what tools send  -> install the ``receiver`` extra
    5. the record destination is misconfigured     -> ``briefcase-ai features exporters``
    6. any other row that needs attention          -> that row's ``next``
    7. tools connected, MCP server not registered  -> ``briefcase-ai mcp install``
    8. no tool on this machine, nothing kept       -> ``briefcase-ai exec -- python your_app.py``
    9. a feature that is off and names a command   -> that row's ``next``
    10. everything is on                           -> ``briefcase-ai features``
    """
    by_id = {item["id"]: item for item in items}
    connect, receive = by_id.get("connect", {}), by_id.get("receive", {})
    mcp, records = by_id.get("mcp", {}), by_id.get("records", {})

    def suggestion(command: str, reason: str) -> Dict[str, str]:
        return {"command": command, "reason": reason}

    if connect.get("state") == "attention" and connect.get("next"):
        return suggestion(connect["next"], "a connected tool lost the settings connect wrote")
    if connect.get("state") == "off" and connect.get("next"):
        return suggestion(connect["next"], "a coding tool is installed but not connected")
    if connect.get("state") == "on" and receive.get("state") == "off" and receive.get("next"):
        return suggestion(receive["next"], "tools are connected but nothing is listening")
    if receive.get("state") == "attention" and receive.get("next"):
        return suggestion(receive["next"], "then restart it: briefcase-ai receive --stop && briefcase-ai receive --detach")  # noqa: E501
    if records.get("state") == "attention":
        return suggestion("briefcase-ai features exporters", "the record destination is misconfigured, see Records above")  # noqa: E501
    for item in items:
        if item["state"] == "attention" and item.get("next") and item["id"] not in CORE_IDS:
            return suggestion(item["next"], f"{item['title']} needs attention")
    if connect.get("state") == "on" and mcp.get("state") in ("off", "attention") and mcp.get("next"):
        return suggestion(mcp["next"], "let your coding tools query briefcase")
    if connect.get("state") == "off" and records.get("state") == "off":
        return suggestion("briefcase-ai exec -- python your_app.py",
                          "no coding tool found here; capture a Python app instead")
    for item in items:
        if item["state"] == "off" and item.get("next") and item["id"] not in CORE_IDS:
            return suggestion(item["next"], f"{item['title']} is off")
    return suggestion("briefcase-ai features", "see what else briefcase can do")


def render(report: Dict[str, Any], out: Callable[[str], Any] = print) -> None:
    """Print the human screen for a report from ``collect``."""
    out(_ux.style("briefcase-ai status", "bold") + f"  (briefcase-ai {report['version']}, state in "
        f"{_short(report['home'])})")
    headings = {key: heading for key, heading, _ in _discovery.GROUPS}
    width = max((len(item["title"]) for item in report["items"]), default=0)
    current = None
    for item in report["items"]:
        if item["group"] != current:
            current = item["group"]
            out("")
            out(_ux.style(headings.get(current, "Other"), "bold"))
        out(f"  {_ux.state_label(item['state'])} {item['title']:<{width}}  {item['detail']}")
    summary = report["summary"]
    out("")
    out(f"{summary['on']} on · {summary['off']} off · {summary['attention']} "
        f"need{'s' if summary['attention'] == 1 else ''} attention")
    _ux.print_next(report["next"]["command"], report["next"]["reason"], out=out)


def print_status(modules: Optional[List[_discovery.CommandModule]] = None, as_json: bool = False,
                 out: Callable[[str], Any] = print) -> Dict[str, Any]:
    report = collect(modules)
    if _ux.debug_enabled():
        import sys

        for problem in report["problems"]:
            print(problem, file=sys.stderr)
    if as_json:
        _ux.print_json(report, out=out)
    else:
        render(report, out)
    return report


def cmd_status(args, store=None, engine=None, stack=None) -> int:
    print_status(as_json=getattr(args, "json", False))
    return 0
