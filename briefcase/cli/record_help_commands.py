"""Local, session-only guidance for finding a decision from an earlier run."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from briefcase.cli import _ux

GROUP = "start"
ORDER = 25
_TABLE = Path(__file__).with_name("record_guidance_v1.json")


def register(sub) -> None:
    parser = sub.add_parser(
        "record-help",
        help="Find an existing decision using evidence from the original run.",
        description="Describe the original run. Give a question as an argument, or pipe a question "
                    "and an optional clarification on separate lines. All input stays local.",
    )
    parser.add_argument("question", nargs="?", help="Question and known facts about the original run.")
    parser.add_argument("--answer", help="Answer to the guide's one clarification.")
    parser.add_argument("--static", action="store_true", help="Print the complete offline guide.")
    parser.set_defaults(func=cmd_record_help)


def _matches(rule: Dict[str, Any], evidence: str) -> bool:
    return (all(re.search(pattern, evidence, re.I) for pattern in rule.get("all", []))
            and not any(re.search(pattern, evidence, re.I) for pattern in rule.get("none", [])))


def _select(table: Dict[str, Any], evidence: str) -> str:
    return next((rule["action"] for rule in table["rules"] if _matches(rule, evidence)),
                table["defaultAction"])


def route(table: Dict[str, Any], question: str, answer: Optional[str] = None,
          shell: Optional[str] = None) -> Tuple[str, str]:
    evidence = question.strip()
    if shell and _matches(table["shell"], evidence):
        evidence += "\nInherited exporter: " + shell
    key = _select(table, evidence)
    action = table["actions"][key]
    if action["kind"] == "ask" and answer is not None:
        reply = answer.strip()
        if reply and not re.fullmatch(table["unknown"], reply, re.I):
            evidence = action["replyPrefix"] + reply + "\n" + evidence
            key = _select(table, evidence)
        if table["actions"][key]["kind"] == "ask":
            key = table["fallback"]
    template = table["actions"][key]["text"]

    def substitute(placeholder: re.Match) -> str:
        name = placeholder.group(1)
        for pattern in table["extract"][name]:
            match = re.search(pattern, evidence, re.I)
            if match:
                return match.group(1)
        return table["defaults"][name]

    return key, re.sub(r"\{(\w+)\}", substitute, template)


def _shell_exporter() -> str:
    value = os.environ.get("BRIEFCASE_EXPORTER", "").strip()
    if value.lower().startswith(("http://", "https://")):
        return "HTTP"  # The receiver lookup does not need credentials or query parameters.
    if value:
        return value
    if any(os.environ.get(key) for key in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")):
        return "otlp"
    return "console"


def _line(prompt: str, interactive: bool) -> str:
    if interactive:
        print(prompt, end="", flush=True)
    return sys.stdin.readline().strip()


def cmd_record_help(args, store, engine, stack) -> int:
    table = json.loads(_TABLE.read_text(encoding="utf-8"))
    if args.static:
        print(table["guide"])
        return 0
    interactive = _ux.is_interactive()
    piped = not sys.stdin.isatty()
    question = args.question
    if question is None and (interactive or piped):
        question = _line("What are you trying to find, and what do you know about the original run? ", interactive)
    if not question:
        print(table["guide"])
        return 0
    shell = _shell_exporter()
    key, text = route(table, question, args.answer, shell)
    if table["actions"][key]["kind"] == "ask" and args.answer is None:
        print(text, flush=True)
        if interactive or piped:
            reply = _line("Answer (or unknown): ", interactive)
            if interactive or reply:
                key, text = route(table, question, reply, shell)
            else:
                return 0
        else:
            return 0
    print(text)
    if key == table["fallback"]:
        print("\n" + table["guide"])
    elif key in ("fresh", "future", "memory_lost"):
        print("Open the example with: briefcase-ai record-help --static")
    return 0
