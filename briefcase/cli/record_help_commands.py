"""Local, session-only guidance for finding a decision from an earlier run."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

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
    alternatives = rule.get("any", [])
    return (all(re.search(pattern, evidence, re.I) for pattern in rule.get("all", []))
            and (not alternatives or any(re.search(pattern, evidence, re.I) for pattern in alternatives))
            and not any(re.search(pattern, evidence, re.I) for pattern in rule.get("none", [])))


def _collect_facts(table: Dict[str, Any], evidence: str) -> Set[str]:
    clauses = re.split(table["split"], evidence, flags=re.I)
    facts = set()
    for name, criterion in table["facts"].items():
        units = clauses if criterion["scope"] == "clause" else [evidence]
        if any(_matches(criterion, unit) for unit in units):
            facts.add(name)
    return facts


def _select(table: Dict[str, Any], facts: Set[str]) -> str:
    for decision in table["decisions"]:
        alternatives = decision.get("any", [])
        if (facts.issuperset(decision.get("all", [])) and facts.isdisjoint(decision.get("none", []))
                and (not alternatives or not facts.isdisjoint(alternatives))):
            return decision["action"]
    return table["defaultAction"]


def route(table: Dict[str, Any], question: str, answer: Optional[str] = None,
          shell: Optional[str] = None) -> Tuple[str, str]:
    def preprocess(value: str) -> str:
        for change in table["preprocess"]:
            value = re.sub(change["pattern"], change["replacement"], value, flags=re.I)
        return value.strip()

    evidence = preprocess(question)
    clauses = re.split(table["split"], evidence, flags=re.I)
    relevant = [clause for clause in clauses if not _matches(table["excludeClauses"], clause)]
    if len(relevant) != len(clauses):
        evidence = "\n".join(relevant)
    facts = _collect_facts(table, evidence)
    shell_path = None
    if shell and _matches(table["shell"], evidence):
        value = preprocess(shell)
        for kind in table["shellKinds"]:
            if _matches(kind, value):
                facts.update(kind["add"])
                if kind.get("path"):
                    shell_path = value
                break
    key = _select(table, facts)
    action = table["actions"][key]
    if action["kind"] == "ask" and answer is not None:
        reply = preprocess(answer)
        transitions = table["replies"][key]
        valid = next((step for step in transitions if _matches(step, reply)), None)
        if not reply or re.fullmatch(table["unknown"], reply, re.I) or valid is None:
            key = table["fallback"]
        else:
            if key in ("ask_path", "ask_exporter"):
                shell_path = None
            evidence = action["replyPrefix"] + reply + "\n" + evidence
            facts.difference_update(valid.get("drop", []))
            facts.update(valid.get("add", []))
            key = valid.get("action") or _select(table, facts)
            if table["actions"][key]["kind"] == "ask":
                key = table["fallback"]
    template = table["actions"][key]["text"]

    def substitute(placeholder: re.Match) -> str:
        name = placeholder.group(1)
        if name == "path" and shell_path:
            return shell_path
        for pattern in table["extract"][name]:
            match = re.search(pattern, evidence, re.I)
            if match:
                value = match.group(1)
                if any(quote + value + quote in match.group() for quote in ("'", '"', "`")):
                    return value
                return re.sub(table["trimCapture"], "", value).strip()
        return table["defaults"][name]

    return key, re.sub(r"\{(\w+)\}", substitute, template)


def _shell_exporter(question: str = "") -> str:
    """Describe only configuration shared by the plausible original runtimes."""
    env = os.environ
    value = env.get("BRIEFCASE_EXPORTER", "").strip()
    node = bool(re.search(r"\b(?:node(?:js)?|npm|npx|preload|javascript|typescript)\b|\.[cm]?js\b", question, re.I))
    python = bool(re.search(r"\b(?:python[23]?|pip)\b|\.py\b", question, re.I))
    if node and re.search(r"\bworker[_ ]threads?\b", question, re.I):
        return "check-launch-config"
    if value.lower().startswith(("http://", "https://")):
        return "HTTP"
    if value and value.lower() not in ("otel", "otlp"):
        if value in ("memory", "console"):
            return value
        if value.endswith(".jsonl") and not re.search(r"^[a-z][a-z0-9_.-]+:|[\x00-\x1f\x7f]", value, re.I):
            return value
        return "check-launch-config"
    endpoints = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
    if not value and not any(env.get(key, "").strip() for key in endpoints):
        return "console"

    from briefcase._observe import check_environment

    protocol = (env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or env.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "")
    node_target = "console" if protocol.strip().lower() == "grpc" else "otlp"
    if node and not python:
        return node_target
    python_target = "otlp"
    if check_environment(env):
        python_target = "check-launch-config"
    elif env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        python_target = "disabled"
    if python and not node or python_target == node_target:
        return python_target
    return "check-launch-config"


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
    shell = _shell_exporter(question) if _matches(table["shell"], question) else None
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
