"""Local, session-only guidance for finding a decision from an earlier run."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from briefcase.cli import _ux

GROUP = "start"
ORDER = 25
_TABLE = Path(__file__).with_name("record_guidance_v1.json")


def register(sub) -> None:
    parser = sub.add_parser(
        "record-help",
        help="Find an existing decision using evidence from the original run.",
        description="Describe the original run. Give a question as an argument, or pipe a question "
                    "and optional clarification on separate lines. A final source/result pair can confirm your check. "
                    "All input stays local.",
    )

    def private_syntax_error(_message: str) -> None:
        parser.exit(2, "Invalid record-help arguments. See record-help --help.\n")

    parser.error = private_syntax_error
    parser.add_argument("question", nargs="?", help="Question and known facts about the original run.")
    parser.add_argument("--answer", help="Answer to the guide's one clarification.")
    parser.add_argument("--static", action="store_true", help="Print the complete offline guide.")
    parser.add_argument("--source", metavar="{jsonl,receiver,storage}", action="append",
                        help="Source inspected for the original decision ID or run-unique marker. Pair with --result.")
    parser.add_argument("--result", metavar="{matched,no-match,unverified}", action="append",
                        help="Observed result at that source. Use matched only for an unambiguous original match.")
    parser.set_defaults(func=cmd_record_help)


def _matches(rule: Dict[str, Any], evidence: str) -> bool:
    alternatives = rule.get("any", [])
    flags = 0 if rule.get("caseSensitive") else re.I
    return (all(re.search(pattern, evidence, flags) for pattern in rule.get("all", []))
            and (not alternatives or any(re.search(pattern, evidence, flags) for pattern in alternatives))
            and not any(re.search(pattern, evidence, flags) for pattern in rule.get("none", [])))


def _collect_facts(table: Dict[str, Any], evidence: str) -> Set[str]:
    clauses = re.split(table["split"], evidence, flags=re.I)
    request_scopes = table["requestScopes"]
    requested = []
    for clause in clauses:
        for pattern in request_scopes["patterns"]:
            match = re.search(pattern, clause, re.I)
            if match and not re.search(request_scopes["negatedBefore"], clause[:match.start()], re.I):
                requested.append(re.split(request_scopes["cut"], match.group(1), flags=re.I, maxsplit=1)[0])
    destinations = [clause for clause in clauses if not re.search(table["destinationDenied"], clause, re.I)]
    scopes = {"clause": clauses, "text": [evidence], "request": requested, "destination": destinations}
    facts = set()
    for name, criterion in table["facts"].items():
        units = scopes[criterion["scope"]]
        if any(_matches(criterion, unit) for unit in units):
            facts.add(name)
    return facts


def _entity(spec: Dict[str, Any], text: str, grammar: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Use the most specific supported source. Conflicting values stay unknown."""
    def expand_fragment(key: re.Match[str]) -> str:
        return grammar[key.group(1)]

    for tier in spec["tiers"]:
        values = set()
        ambiguous = False
        for expression in tier["patterns"]:
            pattern = re.sub(r"\{\{(\w+)\}\}", expand_fragment, expression)
            for match in re.finditer(pattern, text, re.I):
                if re.search(spec["negatedBefore"], text[:match.start()], re.I):
                    continue
                value = match.group(1).strip()
                if not value or any(re.search(test, value, re.I) for test in spec["reject"]):
                    continue
                values.add(value)
                if re.match(spec["ambiguousTail"], text[match.end():], re.I):
                    ambiguous = True
        if values:
            if len(values) != 1 or ambiguous:
                return {}
            return {"value": values.pop(), "fact": tier.get("fact", ""),
                    "independent": tier.get("independent", False)}
    return None


def _entities(table: Dict[str, Any], text: str) -> Dict[str, Dict[str, Any]]:
    values = {}
    for name, spec in table["entities"].items():
        source = text
        if spec.get("scope") == "clause":
            clauses = re.split(table["split"], text, flags=re.I)
            source = "\n".join(clause for clause in clauses if not re.search(spec["exclude"], clause, re.I))
        entity = None
        if spec.get("preferOutside"):
            clauses = re.split(table["split"], source, flags=re.I)
            direct = "\n".join(c for c in clauses if not re.search(spec["preferOutside"], c, re.I))
            if len(clauses) > 1:
                entity = _entity(spec, direct, table["entityGrammar"])
        if entity is None:
            entity = _entity(spec, source, table["entityGrammar"])
        if entity is not None:
            values[name] = entity
    return values


def _select(table: Dict[str, Any], facts: Set[str]) -> str:
    for decision in table["decisions"]:
        alternatives = decision.get("any", [])
        if (facts.issuperset(decision.get("all", [])) and facts.isdisjoint(decision.get("none", []))
                and (not alternatives or not facts.isdisjoint(alternatives))):
            return decision["action"]
    return table["defaultAction"]


def _original_commands(table: Dict[str, Any], text: str) -> str:
    """Keep an original launch and its arguments, without attributing later shell commands to it."""
    parts = re.split(f"({table['commandClauseSplit']})", text, flags=re.I)
    for index in range(0, len(parts), 2):
        clause = parts[index]
        launch = re.search(table["launchCommand"], clause, re.I)
        if launch:
            boundary = next((match for match in re.finditer(table["commandToken"], clause[launch.start():])
                             if match.group(1)), None)
            if boundary:
                parts[index] = clause[:launch.start() + boundary.start()]
    return "".join(parts)


def _scoped(table: Dict[str, Any], text: str) -> str:
    clauses = re.split(table["split"], text, flags=re.I)
    relevant = [clause for clause in clauses if not _matches(table["excludeClauses"], clause)]
    return text if len(relevant) == len(clauses) else "\n".join(relevant)


def _sensitive(table: Dict[str, Any], text: str) -> bool:
    spec = table["sensitiveInput"]
    general = re.sub(table["privateJobMarker"]["declaration"], "", text, flags=re.I)
    return bool(re.search(spec["pattern"], text, re.I) or re.search(spec["privateLabel"], general, re.I))


def _shell_matches(table: Dict[str, Any], launch: str) -> bool:
    return _matches(table["shell"], launch) and any(
        _matches(table["shellContinuity"], clause) for clause in re.split(table["split"], launch, flags=re.I)
    )


def route(table: Dict[str, Any], question: str, answer: Optional[str] = None,
          shell: Optional[str] = None, inherited: Optional[Dict[str, str]] = None,
          confirmation: Optional[Tuple[str, str]] = None, privacy_notes: Tuple[str, ...] = ()) -> Tuple[str, str]:
    def preprocess(value: str) -> str:
        sensitive = table["sensitiveInput"]
        private = _sensitive(table, value)

        def web_label(match: re.Match[str]) -> str:
            url = match.group(1)
            if re.search(table["uncertainExplicitUrl"], url, re.I):
                return " explicit unverified web exporter "
            return " explicit valid web exporter " if _http_destination(url) else " explicit rejected web exporter "

        for direct in table["explicitUrls"]:
            value = re.sub(direct["pattern"], web_label, value, flags=re.I)
        if private:
            clauses = re.split(table["split"], value, flags=re.I)
            relevant = " ".join(c for c in clauses if not _matches(table["excludeClauses"], c)
                                and not re.search(sensitive["unconfirmed"], c, re.I))
            return next((item["replacement"] for item in sensitive["destinations"]
                         if re.search(item["pattern"], relevant, re.I)), "original exporter unknown")
        for change in table["preprocess"]:
            value = re.sub(change["pattern"], change["replacement"], value, flags=re.I)
        return value.strip()

    question = _original_commands(table, question)
    evidence = preprocess(question)
    disowned = {rule["fact"] for rule in table["disownedObservations"] if re.search(rule["pattern"], evidence, re.I)}
    evidence = _scoped(table, evidence)
    marker = table["privateJobMarker"]
    evidence = re.sub(re.escape(marker["replacement"]), "redacted", evidence, flags=re.I)
    raw_private = {m.group(1) for raw in (question, answer or "", *privacy_notes)
                   for m in re.finditer(marker["declaration"], raw, re.I)}
    private_values = {match.group(1) for match in re.finditer(marker["declaration"], evidence, re.I)}
    if private_values:
        # Opaque local matching retains only equality, never the job token as a renderable slot.
        def hide_token(match: re.Match[str]) -> str:
            if match.group() in private_values:
                return marker["replacement"] if len(private_values) == 1 else "redacted"
            return match.group()

        evidence = re.sub(marker["token"], hide_token, evidence)
    facts = _collect_facts(table, evidence) | disowned
    if "privateJobMarker" in facts:
        witness = table["fileWitness"]
        previous_read = False
        for clause in re.split(table["split"], evidence, flags=re.I):
            clause = clause.strip()
            if not clause:
                continue
            if re.search(witness["foreign"], clause, re.I):
                previous_read = False
                continue
            file_read = bool(re.search(witness["read"], clause, re.I)) and _matches(table["facts"]["fileRead"], clause)
            linked = file_read or (previous_read and re.search(witness["continuation"], clause, re.I))
            if linked and _matches(table["facts"]["privateFileWitness"], clause):
                facts.add("linkedFileWitness")
            negative = file_read or (previous_read and re.search(witness["negativeContinuation"], clause, re.I))
            if negative and any(_matches(table["facts"][name], clause) for name in ("staleRows", "readMiss")):
                facts.add("linkedFileMiss")
            previous_read = file_read
    inline_path = None
    inline = table["inlineNode"]
    commands = [match for clause in re.split(table["split"], _scoped(table, question), flags=re.I)
                if not re.search(table["sensitiveInput"]["unconfirmed"], clause, re.I)
                for match in re.finditer(inline["command"], clause)]
    selected = None
    if len(commands) == 1:
        # Parse literal assignment tokens as data. Last assignment to a key wins.
        settings = {}
        for match in re.finditer(inline["assignment"], commands[0].group(1)):
            key = match.group(1)
            if key not in inline["keys"]:
                continue
            raw = next((v for v in match.groups()[1:] if v is not None), "")
            if key in ("BRIEFCASE_EXPORTER", "BRIEFCASE_AUTO"):
                settings[key] = raw.strip()
            else:
                # Node selects the first nonempty raw protocol before trimming it.
                settings[key] = "grpc" if raw.strip().lower() == "grpc" else ("http/json" if raw else "")
        if inherited is not None:
            settings = {**inherited, **settings}
        lines = "\n".join(f"{key}={value}" for key, value in settings.items())
        selected = next((rule for rule in inline["rules"] if _matches(rule, lines)), None)
        if selected:
            facts.add(selected["fact"])
            if selected["fact"] == "inlineFile":
                inline_path = None if _sensitive(table, question) else settings["BRIEFCASE_EXPORTER"]
    elif commands:
        facts.add("inlineUnknown")
    files = {next(value for value in match.groups() if value is not None)
             for match in re.finditer(table["fileMentions"], evidence, re.I)}
    if len(files) > 1:
        facts.add("ambiguousFileWitness")
    entities = _entities(table, evidence)
    facts.update(value["fact"] for value in entities.values() if value.get("fact"))
    for comparison in table["comparisons"]:
        original = entities.get(comparison["left"], {})
        observed = entities.get(comparison["right"], {}).get("value")
        eligible = original.get("independent") or not comparison.get("requiresIndependent", True)
        if eligible and original.get("value") and observed:
            facts.add(comparison["equal"] if original["value"] == observed else comparison["different"])
    shell_path = inline_path
    if shell and len(commands) <= 1 and selected is None and _shell_matches(table, evidence):
        value = preprocess(shell)
        for kind in table["shellKinds"]:
            if _matches(kind, value):
                facts.difference_update(kind.get("drop", []))
                facts.update(kind["add"])
                if kind.get("path"):
                    shell_path = value
                break
    key = _select(table, facts)
    if key in table["requiresPath"] and not (shell_path or entities.get("path", {}).get("value")):
        key = "ask_path"
    action = table["actions"][key]
    if action["kind"] == "ask" and answer is not None:
        safe_choice = key in ("ask_exporter", "ask_protocol") and any(
            re.fullmatch(pattern, answer.strip(), re.I) for pattern in table["exporterReplies"].values())
        unsafe_reply = ((_sensitive(table, question) and not safe_choice)
                        or any(value in answer for value in raw_private))
        reply_source = answer
        if _sensitive(table, answer) or re.search(table["privateWebAddress"], answer, re.I):
            reply_source = "\n".join(c for c in re.split(table["split"], answer, flags=re.I)
                                     if not (_sensitive(table, c) or re.search(table["privateWebAddress"], c, re.I)))
        reply = "" if unsafe_reply else _scoped(table, preprocess(reply_source))
        transitions = table["replies"][key]
        valid = next((step for step in transitions if _matches(step, reply) and
                      ("affirmative" not in step or re.fullmatch(table["exporterReplies"][step["affirmative"]],
                                                                 reply, re.I))), None)
        contextual_reply = action["replyPrefix"] + reply
        reply_entities = _entities(table, contextual_reply)
        reply_id = reply_entities.get("correlation", {})
        repeated = not facts.isdisjoint(("sharedInput", "multiple"))
        shared_value = (entities.get("businessTicket") or entities.get("correlation") or {}).get("value")
        valid_id = key != "ask_id" or (reply_id.get("fact") == "hasID"
                                       and not (repeated and reply_id.get("value") == shared_value))
        if not reply or re.fullmatch(table["unknown"], reply, re.I) or valid is None or not valid_id:
            key = table["fallback"]
        else:
            if key in ("ask_path", "ask_exporter"):
                shell_path = None
            entities.update(reply_entities)
            evidence = contextual_reply + "\n" + evidence
            facts.difference_update(valid.get("drop", []))
            facts.update(valid.get("add", []))
            key = valid.get("action") or _select(table, facts)
            unresolved_path = key in table["requiresPath"] and not (shell_path or entities.get("path", {}).get("value"))
            if table["actions"][key]["kind"] == "ask" or unresolved_path:
                key = table["fallback"]
    if confirmation:
        source, result = confirmation
        key = table["confirmation"]["outcomes"].get(source, {}).get(result, key)
    return key, table["actions"][key]["text"]


def _http_destination(url: str, *, allow_insecure: bool = False) -> bool:
    from urllib.parse import urlsplit

    from briefcase._observe import _check_url
    from briefcase.exporters.http import _is_loopback_host

    try:
        _check_url(url, "destination")
        parts = urlsplit(url)
    except ValueError:
        return False
    return (not any(character.isspace() for character in url)
            and (parts.scheme == "https" or allow_insecure or _is_loopback_host(parts.hostname or "")))


def _shell_exporter(question: str = "") -> str:
    """Describe only configuration shared by the plausible original runtimes."""
    env = os.environ
    value = env.get("BRIEFCASE_EXPORTER", "").strip()
    node = bool(re.search(r"\b(?:node(?:js)?|npm|npx|preload|javascript|typescript)\b|\.[cm]?js\b", question, re.I))
    python = bool(re.search(r"\b(?:python[23]?|pip)\b|\.py\b", question, re.I))
    if node and re.search(r"\bworker[_ ]threads?\b", question, re.I):
        return "check-launch-config"
    if value.lower().startswith(("http://", "https://")):
        return "HTTP" if _http_destination(value) else "check-launch-config"
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
    endpoint = next((env[key].strip() for key in endpoints if env.get(key, "").strip()), "")
    if node_target == "otlp" and endpoint and not _http_destination(endpoint, allow_insecure=True):
        node_target = "check-launch-config"
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


def _confirmation(table: Dict[str, Any], line: str) -> Optional[Tuple[str, str]]:
    parts = line.split()
    if len(parts) == 2 and parts[1] in table["confirmation"]["outcomes"].get(parts[0], {}):
        return parts[0], parts[1]
    return None


def cmd_record_help(args, store, engine, stack) -> int:
    table = json.loads(_TABLE.read_text(encoding="utf-8"))
    if bool(args.source) != bool(args.result):
        print("Confirmation needs both --source and --result.", file=sys.stderr)
        return 2
    confirmation = None
    if args.source and args.result:
        if (len(args.source) != 1 or len(args.result) != 1
                or args.result[0] not in table["confirmation"]["outcomes"].get(args.source[0], {})):
            print("Choose exactly one supported source and one observed result. See --help.", file=sys.stderr)
            return 2
        confirmation = (args.source[0], args.result[0])
    interactive = _ux.is_interactive()
    piped = not sys.stdin.isatty()
    lines: List[str] = []
    if piped:
        try:
            lines = sys.stdin.read().splitlines()
        except OSError:
            # Some embedded callers supply a disabled stdin rather than an empty pipe.
            pass
    notes = tuple(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    question = args.question
    if question is not None and _confirmation(table, question):
        lines.insert(0, question)
        question = None
    reports = [(i, pair) for i, line in enumerate(lines) if (pair := _confirmation(table, line)) is not None]
    if ((args.answer and _confirmation(table, args.answer)) or
            (reports and (confirmation or len(reports) != 1 or reports[0][0] != len(lines) - 1))):
        print("Supply one source and result together, once, at the end of the session.", file=sys.stderr)
        return 2
    if args.static:
        print(table["guide"])
        return 0
    if reports:
        confirmation = reports[0][1]
        lines.pop()
    if question is None and lines:
        question = lines.pop(0)
    if question is None and not confirmation and interactive:
        question = _line("What are you trying to find, and what do you know about the original run? ", True)
    if not question and not confirmation:
        print(table["guide"])
        return 0
    question = question or ""
    launch = _scoped(table, _original_commands(table, question))
    launch_clauses = [clause for clause in re.split(table["split"], launch, flags=re.I)
                      if not (re.search(table["runtimeInspection"], clause, re.I)
                              or re.search(table["runtimeDenied"], clause, re.I))]
    runtime = " ".join(name for name, pattern in table["runtimeSource"].items()
                       if any(re.search(pattern, clause, re.I) for clause in launch_clauses))
    shell = _shell_exporter(runtime) if _shell_matches(table, launch) else None
    inherited = None
    if shell is not None:
        # Node overlays command assignments on the launch environment. Only exporter vocabulary is needed here.
        inherited = {}
        if os.environ.get("BRIEFCASE_AUTO", "").strip().lower() in ("0", "false"):
            inherited["BRIEFCASE_AUTO"] = "0"
        for key in ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"):
            protocol = os.environ.get(key, "")
            if protocol:
                inherited[key] = "grpc" if protocol.strip().lower() == "grpc" else "http/json"
        choice = os.environ.get("BRIEFCASE_EXPORTER", "").strip()
        if choice in ("memory", "console") or choice.lower() in ("otel", "otlp"):
            inherited["BRIEFCASE_EXPORTER"] = choice
    answer = args.answer
    key, text = route(table, question, answer, shell, inherited, confirmation, notes)
    ask = not confirmation and answer is None and table["actions"][key]["kind"] == "ask"
    if ask:
        print(text, flush=True)
        if lines:
            reply = lines.pop(0)
        elif interactive:
            reply = _line("Answer (or unknown): ", True)
        else:
            return 0
        confirmation = _confirmation(table, reply)
        answer = None if confirmation else reply
        key, text = route(table, question, answer, shell, inherited, confirmation, notes)
    if not confirmation and interactive and key in table["confirmation"]["next"]:
        print(table["confirmation"]["instruction"])
        response = _line("Optional source and result (Enter to skip): ", True)
        notes += (response,)
        confirmation = _confirmation(table, response)
    key, text = route(table, question, answer, shell, inherited, confirmation, notes)
    print(text)
    if key in table["confirmation"]["next"]:
        print(table["confirmation"]["instruction"])
    if key == table["fallback"]:
        print("\n" + table["guide"])
    elif key in ("fresh", "future", "memory_lost"):
        print("Open the example with: briefcase-ai record-help --static")
    return 0
