"""``briefcase setup``: the guided setup, also what ``briefcase`` with no arguments runs the
first time on a terminal.

    briefcase            # first run on a terminal: the guide; afterwards: status and help
    briefcase setup      # run the guide again

One question (what are you setting up?), then two or three steps for that answer. Each step
calls the same function the matching command calls (``briefcase connect``,
``briefcase receive --detach``, ``briefcase exec``), so nothing here behaves differently
from running the commands by hand, and nothing is written before the command's own
confirmation. It finishes with the ``briefcase status`` screen.

Completion is remembered in ``~/.briefcase/setup.json`` (``$BRIEFCASE_HOME`` moves it), and
only once the guide was finished or skipped, never when it was interrupted. Delete the file
to see the guide again on a bare ``briefcase``.

Without a terminal, or with ``--no-input`` / ``BRIEFCASE_NO_INPUT=1``, no question is asked:
the status screen and the command list are printed and the exit code is 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from argparse import Namespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.cli import _discovery, _ux, status_commands

GROUP = "start"
ORDER = 10

SETUP_FILE = "setup.json"
PERSONAS = (
    ("coding-tools", "I use AI coding tools (Claude Code, Cursor, Codex…)"),
    ("app", "I'm building an app that calls LLMs"),
    ("team", "I run a team or platform"),
    ("skip", "Skip for now"),
)

# What the app-builder dry run executes. It defines one decorated function and calls it.
DEMO_PROGRAM = (
    "import briefcase\n"
    "\n"
    "@briefcase.capture\n"
    "def greet(name):\n"
    "    return 'hello ' + name\n"
    "\n"
    "greet('world')\n"
)

# Team path. `commands` are the command names that, once a package providing one is
# installed, replace the "coming" note; `today` is what already works.
TEAM_STEPS: Tuple[Dict[str, Any], ...] = (
    {"title": "Roll out to every developer machine",
     "commands": ("managed", "rollout"),
     "today": "briefcase connect --dry-run",
     "today_note": "prints the exact settings to distribute with your device management"},
    {"title": "Decide what coding tools may run",
     "commands": ("policy",),
     "today": None, "today_note": None},
    {"title": "Cap spend",
     "commands": ("budget",),
     "today": "briefcase features controls-gateway",
     "today_note": "the in-app spend and quota gate that ships today"},
)


def register(sub) -> None:
    p = sub.add_parser(
        "setup",
        help="Guided setup: say what you are doing, get the two or three steps for it.",
        description="Asks one question (coding tools, an LLM app, or a team rollout) and walks "
                    "through the steps for it. Every write is shown first and needs your "
                    "confirmation. Running `briefcase` with no arguments starts this guide the "
                    "first time.",
        epilog="example:\n  briefcase setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(func=cmd_setup)


# --- remembered completion ------------------------------------------------------

def setup_path():
    return status_commands.briefcase_home() / SETUP_FILE


def setup_state() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(setup_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("outcome") in ("finished", "skipped") else None


def mark_done(persona: Optional[str], outcome: str) -> None:
    """Record that the guide was finished or skipped. Failing to write is not an error: the
    guide is simply offered again next time."""
    path = setup_path()
    data = {"version": 1, "outcome": outcome, "persona": persona,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".setup-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


# --- the three paths ------------------------------------------------------------

def _step(index: int, total: int, title: str, out: Callable[[str], Any]) -> None:
    out("")
    out(_ux.style(f"Step {index} of {total} · {title}", "bold"))


def _connect_args() -> Namespace:
    # The defaults of `briefcase connect` with no flags.
    return Namespace(tools=[], all=False, scope="user", endpoint=None, headers=[], mcp=True,
                     capture_content=False, connect_dry_run=False, dry_run=False, undo=False,
                     yes=False, force=False, start=False, telemetry=True)


def _receive_args() -> Namespace:
    # The defaults of `briefcase receive --detach`.
    return Namespace(port=4318, host="127.0.0.1", to=None, keep_content=False, metrics=False,
                     token=None, max_body_mb=8.0)


def path_coding_tools(out: Callable[[str], Any] = print) -> None:
    from briefcase.cli import connect_commands, receive_commands
    from briefcase.connect.adapters import registry

    _step(1, 2, "Connect your coding tools", out)
    out("This is `briefcase connect`: it lists every file it would change, shows the diff and")
    out("asks once. Prompt and tool content stays off. Undo any time: briefcase connect --undo")
    connect_commands.run_connect(_connect_args(), registry(), out=out, offer_receiver=False, ask=input)

    rows = status_commands.connect_rows()
    tools = status_commands.tools_item(rows)
    _step(2, 2, "Start the receiver", out)
    if tools["state"] == "off":
        out("No tool is connected, so there is nothing to receive yet.")
        out("Supported tools and what was detected: briefcase connect list")
        return
    receiver = status_commands.receiver_item(rows, tools)
    if receiver["state"] != "off":
        out(f"Already {receiver['detail']}.")
        return
    if not receiver["next"]:
        out(receiver["detail"][0].upper() + receiver["detail"][1:] + ", so no local receiver is needed.")
        return
    out("The receiver listens on 127.0.0.1:4318 and turns what the tools send into records in")
    out("~/.briefcase/receiver/records.jsonl. Prompts, responses and paths are dropped.")
    if _ux.confirm("Start it in the background now?", default=True):
        out("")
        receive_commands.start_detached(_receive_args(), out)
        out("Restart the connected tools so they pick up the new settings.")
    else:
        out("Start it when you are ready: briefcase receive --detach")


def path_app(out: Callable[[str], Any] = print) -> None:
    from briefcase.cli import exec_commands

    _step(1, 2, "Capture a Python app without changing it", out)
    out("  briefcase exec -- python your_app.py")
    out("Each OpenAI, Anthropic, Gemini and Bedrock call, and each LangChain, LlamaIndex, CrewAI")
    out("or AutoGen run, becomes one record: model, tokens, latency, status. Prompt and response")
    out("text is not recorded unless you add --capture-content.")
    out("")
    out("A dry run starts this 5-line program through `briefcase exec` and prints its one record")
    out("to the terminal. Nothing is written and nothing leaves this machine:")
    for line in DEMO_PROGRAM.splitlines():
        out(f"    {line}" if line else "")
    if _ux.confirm("Run it now?", default=True):
        out("")
        code = exec_commands.run([sys.executable, "-c", DEMO_PROGRAM], to="console", replace_process=False)
        out("")
        if code == 0:
            out("The JSON line above is the decision record for greet('world').")
        else:
            _ux.fail(f"the dry run exited with code {code}", fix="briefcase --debug exec -- python your_app.py")

    _step(2, 2, "Keep the records", out)
    out("Records print to stderr until you choose a destination:")
    out("  briefcase exec --to runs.jsonl -- python your_app.py     # a file")
    out("  export BRIEFCASE_EXPORTER=https://collector.example.com/v1/decisions   # your endpoint")
    out("  export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  # an OTLP collector")
    out("To record your own functions as well, see: briefcase features capture-decorator")


def _team_command(step: Dict[str, Any], commands: Dict[str, Any], catalog: Any) -> Optional[Dict[str, str]]:
    """The installed command for a team step, with its docs when a manifest names it."""
    for name in step["commands"]:
        if name not in commands:
            continue
        for feature in catalog.features:
            words = feature.command.split()
            if len(words) > 1 and words[0] == "briefcase" and words[1] == name:
                return {"command": feature.command, "docs": feature.docs_file}
        return {"command": f"briefcase {name} --help", "docs": ""}
    return None


def path_team(modules: Optional[List[_discovery.CommandModule]] = None,
              out: Callable[[str], Any] = print) -> None:
    from briefcase.features import load_catalog

    commands = _discovery.command_index(modules)
    catalog = load_catalog()
    out("")
    out("Nothing is changed on this machine for a team setup; these are the steps to plan.")
    total = len(TEAM_STEPS) + 1
    _step(1, total, "Collect records in one place", out)
    out("  On a shared host:   briefcase receive --host 0.0.0.0 --token <secret> --to <destination>")
    out("  On each machine:    briefcase connect --endpoint https://<your-receiver-host> \\")
    out("                          --headers \"Authorization=Bearer <secret>\"")
    out("  Docs: docs/integrations/README.md (The receiver)")
    for index, step in enumerate(TEAM_STEPS, 2):
        _step(index, total, step["title"], out)
        found = _team_command(step, commands, catalog)
        if found:
            out(f"  {found['command']}")
            if found["docs"]:
                out(f"  Docs: {found['docs']}")
            continue
        out("  Coming: there is no command for this in the version installed here.")
        if step["today"]:
            out(f"  Today:  {step['today']}   # {step['today_note']}")
    out("")
    out("Everything that limits or stops things: briefcase features --verb enforce")


# --- the guide ------------------------------------------------------------------

def run_setup(modules: Optional[List[_discovery.CommandModule]] = None,
              out: Callable[[str], Any] = print) -> int:
    """Ask the persona question and walk its path. Returns 0 when the guide was finished or
    skipped, 1 when input ended before an answer."""
    out(_ux.style("briefcase setup", "bold"))
    out("One question, then two or three steps. Nothing is written without asking first.")
    out("")
    persona = _ux.choose("What are you setting up?", PERSONAS)
    if persona is None:
        out("Setup was not finished. Come back to it with: briefcase setup")
        return 1
    if persona == "skip":
        mark_done(None, "skipped")
        out("")
        out("Skipped. `briefcase setup` runs this guide again whenever you want it.")
    else:
        try:
            if persona == "coding-tools":
                path_coding_tools(out)
            elif persona == "app":
                path_app(out)
            else:
                path_team(modules, out)
        except KeyboardInterrupt:
            out("")
            out("Setup was interrupted. Come back to it with: briefcase setup")
            return 1
        mark_done(persona, "finished")
    out("")
    status_commands.print_status(modules, out=out)
    return 0


def front_door(help_text: str, modules: Optional[List[_discovery.CommandModule]] = None,
               out: Callable[[str], Any] = print) -> int:
    """``briefcase`` with no arguments: the guide on a first interactive run, otherwise the
    command list followed by the status screen, so the last line is the ``Next:`` suggestion.
    Always exits 0 when it does not prompt."""
    if _ux.is_interactive() and setup_state() is None:
        return run_setup(modules, out)
    out(help_text.rstrip("\n"))
    out("")
    status_commands.print_status(modules, out=out)
    return 0


def cmd_setup(args, store=None, engine=None, stack=None) -> int:
    if not _ux.is_interactive():
        print("briefcase setup asks questions, so it needs a terminal (and no --no-input). "
              "Here is where things stand:")
        print()
        status_commands.print_status()
        return 0
    return run_setup()
