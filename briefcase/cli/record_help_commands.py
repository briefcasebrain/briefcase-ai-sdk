"""Local, session-only guidance for finding a decision from an earlier run."""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass
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
    parser.add_argument("--answer", action="append", help="Answer to the guide's one clarification.")
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


def _source_text(table, text):
    """Opaque values can correlate observations. Their spelling cannot select a source."""
    spans = {(match.start(1), match.end(1)) for pattern in table["opaqueBindings"]
             for match in re.finditer(pattern, text, re.I)}
    file_spans = []
    for quoted in re.finditer(table["quoteSpans"], text):
        index = quoted.lastindex
        launch_literal = (re.search(table["launchLiteralContext"], text[:quoted.start()], re.I)
                          and re.fullmatch(table["launchLiteralValue"], quoted.group(index), re.I))
        if not launch_literal and not (
            re.search(table["sourceLiteralContext"], text[:quoted.start()], re.I)
            and re.fullmatch(table["sourceLiteralValue"], quoted.group(index), re.I)
        ):
            spans.add(quoted.span(index))
        elif quoted.group(index).lower().endswith(".jsonl") and "://" not in quoted.group(index):
            file_spans.append(quoted.span(index))
    for name in table["opaqueRoles"]:
        for tier in table["entities"][name]["tiers"]:
            for template in tier["patterns"]:
                pattern = re.sub(r"\{\{(\w+)\}\}", lambda m: table["entityGrammar"][m.group(1)], template)
                for found in re.finditer(pattern, text, re.I):
                    if not any(re.search(reject, found.group(1), re.I) for reject in table["entities"][name]["reject"]):
                        spans.add(found.span(1))
    for path in re.finditer(table["fileMentions"], text, re.I):
        start, end = path.span()
        clauses = re.split(table["split"], text[:start], flags=re.I)
        clause = clauses[-1]
        continued = (len(clauses) > 1 and re.search(table["filePathContinuation"], clause, re.I)
                     and re.fullmatch(table["filePathAntecedent"], clauses[-2], re.I))
        if not (continued or re.search(table["fileRoleBefore"], clause, re.I)
                or re.search(table["fileRoleAfter"], text[end:], re.I)):
            spans.add((start, end))
    chars = list(text)
    for start, end in spans:
        chars[start:end] = "_" * (end - start)
    for start, end in file_spans:
        qualifier = re.match(r"[a-zA-Z][a-zA-Z0-9_.-]*:", text[start:end])
        if qualifier:
            start += qualifier.end()
        chars[start:end - 6] = "_" * (end - start - 6)
    return "".join(chars)


def _positive_destinations(table, clauses):
    return {name for name, assertions in table["positiveDestinations"].items()
            if any(re.search(pattern, clause, re.I) for clause in clauses for pattern in assertions)}


def _collect_facts(table: Dict[str, Any], evidence: str) -> Set[str]:
    clauses = re.split(table["split"], evidence, flags=re.I)
    request_scopes = table["requestScopes"]
    requested = []
    for clause in clauses:
        for pattern in request_scopes["patterns"]:
            match = re.search(pattern, clause, re.I)
            if match and not re.search(request_scopes["negatedBefore"], clause[:match.start()], re.I):
                requested.append(re.split(request_scopes["cut"], match.group(1), flags=re.I, maxsplit=1)[0])
    destinations = [clause for clause in clauses if not re.search(table["destinationDenied"], clause, re.I)
                    and not re.search(table["nonOriginalCommand"], clause, re.I)]
    scopes = {"clause": clauses, "text": [evidence], "request": requested, "destination": destinations,
              "affirmative": _positive_destinations(table, destinations)}
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


def _node_register(inline, arguments, *, options=False):
    """Resolve package specifiers in Node runtime options; do not execute or expand arguments."""
    position = 0
    pending = ""
    state = ""
    while token := re.match(inline["optionsArgument" if options else "argument"], arguments[position:]):
        position += token.end()
        value = "".join(part.group(part.lastindex or 0)
                        for part in re.finditer(inline["optionsPart" if options else "argumentPart"], token.group(1)))
        if options:
            value = re.sub(r'\\(["\\])', r"\1", value)
        if pending:
            flag, operand, pending = pending, value, ""
        else:
            if not value.startswith("-") or value in ("-", "--"):
                break
            flag, equal, operand = value.partition("=")
            if flag not in inline["preloadFlags"] and flag not in inline["valueFlags"]:
                continue
            if not equal:
                pending = flag
                continue
        if flag in inline["loaderFlags"] and operand in inline["registerModules"] and not state:
            state = "loader"
        if flag in inline["preloadFlags"]:
            if options and operand.strip("\x27") in inline["registerModules"] and operand.startswith("\x27"):
                return "unexported"
            if operand.startswith(inline["package"] + "/") and operand not in inline["packageExports"]:
                return "unexported"
            if operand in inline["registerModules"]:
                state = "register"
    return state


def _command_register(inline, match):
    argv = _node_register(inline, match.string[match.end():])
    options = ""
    for assignment in re.finditer(inline["assignment"], match.group(1)):
        if assignment.group(1) == "NODE_OPTIONS":
            raw = next((v for v in assignment.groups()[1:] if v is not None), "")
            options = _node_register(inline, " " + raw, options=True)
    if "unexported" in (argv, options):
        return "unexported"
    if argv == "register" or options == "register":
        return "argv" if argv == "register" else "options"
    return "loader" if "loader" in (argv, options) else ""


def _before_capture(table, text):
    """Retain only application configuration that could precede the reported original capture."""
    spec = table["applicationSelection"]
    parts = re.split(f"({table['split']})", text, flags=re.I)
    captured = False
    pending = None
    for index in range(0, len(parts), 2):
        clause = parts[index]
        masked = re.sub(table["quoteSpans"], lambda m: "_" * len(m.group()), clause)
        calls = list(re.finditer(spec["callStart"], masked, re.I))
        after = re.search(spec["after"], masked, re.I)
        event = re.search(spec["captureEvent"], masked, re.I)
        if event and (_matches(table["excludeClauses"], masked)
                      or re.search(spec["none"], masked[:event.start()], re.I)):
            event = None
        continuation = bool(re.match(spec["orderedContinuation"], masked, re.I))
        if event and not after and pending is not None and (not calls or continuation):
            parts[pending] = "Before recording the decision, " + parts[pending]
        if calls:
            if re.search(spec["before"], masked, re.I):
                captured = False
            if captured:
                parts[index] = ""
            elif after or event:
                boundary = after or event
                earlier = [c for c in calls if c.start() < boundary.start()]
                later = [c for c in calls if c.start() > boundary.start()]
                # A trailing "after recording" qualifies the immediately preceding call. An
                # intervening sequence connector instead attaches it to the following call.
                connector = earlier and re.search(r"\b(?:then|next|afterwards?)\b",
                                                  masked[earlier[-1].end():boundary.start()], re.I)
                if after and earlier and not (later and connector):
                    earlier.pop()
                if earlier:
                    cutoff = calls[len(earlier)].start() if len(earlier) < len(calls) else boundary.start()
                    parts[index] = clause[:cutoff]
                    if event and not continuation and not re.search(spec["before"], masked, re.I):
                        parts[index] = "Before recording the decision, " + parts[index]
                else:
                    parts[index] = ""
        if clause.strip():
            if after or event or captured:
                pending = None
            elif calls and not (continuation and pending is not None):
                pending = index if re.search(spec["actor"], masked, re.I) else None
            elif not calls:
                pending = None
        if after or event:
            captured = True
    return "".join(parts)


def _python_call_span(table, clause):
    """Locate one balanced configuration call without evaluating its arguments."""
    spec = table["applicationSelection"]
    masked = re.sub(table["quoteSpans"], lambda m: "_" * len(m.group()), clause)
    start = re.search(spec["callStart"], masked, re.I)
    if not start:
        return None
    depth, begin = 0, start.end()
    for i in range(begin, len(masked)):
        char = masked[i]
        if char in "([{":
            depth += 1
        elif depth == 0 and char in ",)":
            if char == ")":
                break
        elif char in ")]}":
            depth -= 1
    else:
        return None
    return start, i


def _python_choice(table, clause):
    """Bind the public call to reported arguments without resolving names or evaluating source."""
    span = _python_call_span(table, clause)
    unknown = ("inlineUnknown", None)
    if not span:
        return unknown
    start, end = span
    method = "setup" if start.group().endswith("setup(") else "observe"
    source = method + "(" + clause[start.end():end + 1]
    try:
        call = ast.parse(source, mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return unknown
    if not isinstance(call, ast.Call):
        return unknown
    spec = table["applicationSelection"]
    names = spec["setupParameters" if method == "setup" else "pythonParameters"]
    if len(call.args) > (len(names) if method == "setup" else 1):
        return unknown
    bound = dict(zip(names, call.args))
    for kw in call.keywords:
        if kw.arg not in names or kw.arg in bound:
            return unknown
        bound[kw.arg] = kw.value
    target = bound.get("exporter")
    if target is None:
        return None
    if "guardrail_packs" in bound and not isinstance(bound["guardrail_packs"], ast.Constant):
        return unknown
    if isinstance(target, ast.Constant):
        if target.value is None:
            return ("configOff" if method == "setup" else "pythonConsole", None)
        if method == "setup" or not isinstance(target.value, str):
            return unknown
        value = target.value
        if not value.lower().startswith(("http://", "https://")):
            for option in (bound[k] for k in ("token", "headers") if k in bound):
                if isinstance(option, ast.Constant) and option.value in (None, ""):
                    continue
                if not isinstance(option, ast.Dict) or option.keys:
                    return unknown
        if value in ("console", "memory"):
            return ("pythonConsole" if value == "console" else "inlineMemory", None)
        if value.lower().startswith(("http://", "https://")):
            return ("inlineHTTP" if _http_destination(value) else "invalidPythonWeb", None)
        if value.startswith("chain:") and value[6:].strip():
            return "chainStore", None
        if re.match(r"^[A-Za-z][A-Za-z0-9_.-]+:", value):
            return unknown
        return ("pythonFile", value) if value.endswith(".jsonl") else unknown
    if not isinstance(target, ast.Call) or len(target.args) > 1:
        return unknown
    constructor = ast.unparse(target.func)
    parts = constructor.split(".")
    name = parts[-1]
    module = spec["pythonModules"].get(name)
    if not module or ".".join(parts[:-1]) not in ("", "briefcase", "briefcase.exporters", module):
        return unknown
    parameters = spec["pythonConstructors"].get(name)
    if not parameters:
        return unknown
    args = dict(zip(parameters, target.args))
    for kw in target.keywords:
        if kw.arg not in parameters or kw.arg in args:
            return unknown
        args[kw.arg] = kw.value
    first = args.get(parameters[0])
    value = first.value if isinstance(first, ast.Constant) else None
    if name == "MemoryExporter":
        valid = first is None or isinstance(first, ast.Constant) and (value is None or type(value) is int and value > 0)
        return ("inlineMemory", None) if valid else unknown
    if name == "ConsoleExporter":
        stream = ast.unparse(first) if first is not None else "None"
        if stream in ("None", "sys.stderr"):
            return ("pythonConsole", None)
        return ("consoleStdout", None) if stream == "sys.stdout" else unknown
    if not isinstance(value, str) or not value:
        return unknown
    if name == "JSONLFileExporter":
        return ("pythonFile", value)
    insecure = args.get("allow_insecure")
    if insecure is not None and not (isinstance(insecure, ast.Constant) and isinstance(insecure.value, bool)):
        return unknown
    accepted = _http_destination(value, allow_insecure=bool(insecure is not None and insecure.value))
    return ("inlineHTTP" if accepted else "invalidPythonWeb", None)


def _python_exporter_call(table, clause):
    """Normalize a credential-bearing call for replay, using only its argument role."""
    span = _python_call_span(table, clause)
    if not span:
        return clause
    choice = _python_choice(table, clause)
    if choice is None:
        return clause
    kind, path = choice
    canonical = {"inlineMemory": 'observe("memory")', "pythonConsole": 'observe("console")',
                 "configOff": "setup(None)", "consoleStdout": "setup(ConsoleExporter(sys.stdout))",
                 "inlineHTTP": 'observe("https://example.invalid")',
                 "invalidPythonWeb": 'observe("http://invalid.test")'}
    if kind == "pythonFile":
        call = "setup(JSONLFileExporter(" + json.dumps(path) + "))"
    else:
        call = canonical.get(kind, "observe(unverifiedExporterArgument)")
    start, end = span
    return clause[:start.start()] + "briefcase." + call + clause[end + 1:]


def _original_commands(table: Dict[str, Any], text: str) -> str:
    """Keep an original launch and its arguments, without attributing later shell commands to it."""
    text = _before_capture(table, text)
    inherited_register = _shell_matches(table, text)
    lines = []
    fence = ""
    exclude = False
    example_heading = None
    for line in text.splitlines():
        marker = re.fullmatch(table["fenceBoundary"], line)
        if fence:
            # A closer must use the opener's character, be at least as long and have no info string.
            if marker and marker.group(1).startswith(fence) and not marker.group(2).strip():
                fence, exclude = "", False
            elif not exclude and not re.match(table["fenceComment"], line):
                lines.append(line)
            continue
        if marker:
            fence = marker.group(1)
            exclude = example_heading is not None
            if example_heading is not None:
                index, start = example_heading
                lines[index] = lines[index][:start]
                example_heading = None
            continue
        if example_heading is not None and re.match(table["fenceComment"], line):
            continue
        if line.strip():
            cue = re.search(table["nonOriginalCommand"], line, re.I)
            example_heading = (len(lines), cue.start()) if cue else None
        lines.append(line)
    text = "\n".join(lines)
    parts = re.split(f"({table['commandClauseSplit']})", text, flags=re.I)
    previous = None
    for index in range(0, len(parts), 2):
        clause = parts[index]
        if not clause.strip():
            continue
        if previous is not None and re.search(table["disownPreviousCommand"], clause, re.I):
            parts[previous] = " original exporter unknown "
        previous = index if re.search(table["launchCommand"], clause, re.I) else None
    example_next = False
    for index in range(0, len(parts), 2):
        clause = parts[index]
        launch = re.search(table["launchCommand"], clause, re.I)
        if launch:
            boundary = next((match for match in re.finditer(table["commandToken"], clause[launch.start():])
                             if match.group(1)), None)
            if boundary:
                parts[index] = clause[:launch.start() + boundary.start()]
        hypothetical = re.search(table["nonOriginalCommand"], clause, re.I)
        candidate = re.search(table["inlineNode"]["wrapperCommand"], clause) or re.search(
            table["inlineNode"]["command"], clause)
        prefix = clause[:candidate.start()] if candidate else ""
        original = re.fullmatch(table["commandContext"], prefix, re.I) and re.search(
            table["launchAttribution"], prefix, re.I)
        if launch and example_next and not original:
            parts[index] = " original exporter unknown "
            example_next = False
            continue
        if clause.strip():
            example_next = bool((hypothetical and not launch) or
                                (example_next and re.match(table["exampleContinuation"], clause, re.I)))
        if hypothetical and launch:
            parts[index] = clause[:hypothetical.start()]
        else:
            wrapper = re.search(table["inlineNode"]["wrapperCommand"], parts[index])
            if wrapper and re.fullmatch(table["commandContext"], parts[index][:wrapper.start()], re.I):
                continue
            command = re.search(table["inlineNode"]["command"], parts[index])
            if command and (not re.fullmatch(table["commandContext"], parts[index][:command.start()], re.I)
                            or not (_command_register(table["inlineNode"], command) or inherited_register)):
                parts[index] = parts[index][:command.start()]
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


def _runtime_names(table, clauses):
    return {name for name, pattern in table["runtimeSource"].items() for clause in clauses
            if not re.search(table["runtimeInspection"], clause, re.I)
            and not re.search(table["runtimeDenied"], clause, re.I)
            and re.search(pattern, clause, re.I)}


def _execution_evidence(table, source, inherited=None):
    """Resolve launch and explicit application evidence identically for a question or its clarification."""
    inline = table["inlineNode"]
    clauses = re.split(table["split"], _scoped(table, source), flags=re.I)
    runtime = _runtime_names(table, clauses) & {"Python", "Node"}
    commands = []
    wrapper_choices = set()
    wrapper_facts = set()
    ambient = (inherited or {}).get("REGISTER_FROM_NODE_OPTIONS")
    for clause in clauses:
        wrapper = re.search(inline["wrapperCommand"], clause)
        if wrapper and re.fullmatch(table["commandContext"], clause[:wrapper.start()], re.I):
            prefix, flags, child = wrapper.groups()
            settings = {match.group(1): next((v for v in match.groups()[1:] if v is not None), "")
                        for match in re.finditer(inline["assignment"], prefix)}
            settings = {**(inherited or {}), **settings}
            spec = inline["wrapperSettings"]
            destination = None
            allow_uninstrumented = False
            invalid_flag = False
            for option in re.finditer(inline["wrapperOption"], flags):
                name, operand = option.groups()
                if name == "--allow-uninstrumented":
                    allow_uninstrumented = True
                if name in spec["flags"] and operand is not None:
                    value = operand.strip("\"'")
                    if name in ("--capture-content", "--dedupe"):
                        invalid_flag |= value not in spec["content" if name == "--capture-content" else "dedupe"]
                    if value:
                        settings[spec["flags"][name]] = value
                        if name == "--to":
                            destination = operand
            from briefcase._observe import _plugin_name
            from briefcase.cli.exec_commands import _capture_configuration_problem

            if invalid_flag or _capture_configuration_problem(settings):
                wrapper_facts.add("wrapperRejected")
            choice = settings.get("BRIEFCASE_EXPORTER", "").strip()
            endpoint = next((settings[k].strip() for k in inline["endpoints"] if settings.get(k, "").strip()), "")
            protocol = (settings.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
                        or settings.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "").strip().lower()
            reserved = choice.lower() in table["wrapperReserved"]
            if reserved and choice not in ("memory", "console") and choice.lower() not in ("otlp", "otel"):
                wrapper_facts.add("wrapperRejected")
            otlp = choice.lower() in ("otel", "otlp") or (not choice and bool(endpoint))
            if otlp and ((protocol and protocol not in inline["wrapperProtocols"])
                         or (endpoint and not _http_destination(endpoint, allow_insecure=True))):
                wrapper_facts.add("wrapperRejected")
            direct_url = choice.lower().startswith(("http://", "https://"))
            if direct_url and not _http_destination(choice, allow_insecure=True):
                wrapper_facts.add("wrapperRejected")
            builtin = (reserved or otlp or direct_url or choice.endswith(".jsonl")
                       or (choice.startswith("chain:") and choice[6:].strip()))
            if choice and not builtin and _plugin_name(choice) is None:
                wrapper_facts.add("wrapperRejected")
            if child.startswith("python"):
                from briefcase.cli.exec_commands import blocking_python_flags

                args = [part.group(1).strip("\"'") for part in re.finditer(inline["argument"], clause[wrapper.end():])]
                if blocking_python_flags([child, *args]):
                    wrapper_facts.add("pythonUninstrumented" if allow_uninstrumented else "wrapperRejected")
                runtime.add("Python")
                kind = {"console": "pythonConsole", "memory": "inlineMemory"}.get(choice)
                if otlp:
                    disabled = settings.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"
                    kind = "disabled" if disabled else "inlineOTLP"
                elif direct_url:
                    kind = "inlineHTTP" if _http_destination(choice) else "invalidPythonWeb"
                elif choice.startswith("chain:") and choice[6:].strip():
                    kind = "chainStore"
                elif re.match(r"^[A-Za-z][A-Za-z0-9_.-]+:", choice):
                    kind = "inlineUnknown"
                elif choice.endswith(".jsonl"):
                    kind = "pythonFile"
                wrapper_choices.add((kind or "inlineUnknown", choice if kind == "pythonFile" else None))
                continue
            runtime.add("Node")
            assigned = "BRIEFCASE_EXPORTER=" + destination + " " if destination else ""
            clause = prefix + "BRIEFCASE_AUTO=1 " + assigned + child + clause[wrapper.end():]
        for match in re.finditer(inline["command"], clause):
            if (_command_register(inline, match) or ambient) and re.fullmatch(
                table["commandContext"], clause[:match.start()], re.I
            ):
                commands.append(match)
    selection = table["applicationSelection"]
    choices: Set[Tuple[str, Optional[str]]] = set()
    application_clauses: List[str] = []
    for clause in clauses:
        continuation = re.match(selection["orderedContinuation"], clause, re.I)
        if continuation and application_clauses and re.search(selection["callStart"], application_clauses[-1], re.I):
            application_clauses[-1] += " and then " + clause[continuation.end():]
        elif clause.strip():
            application_clauses.append(clause.strip())
    for clause in application_clauses:
        worker_call = re.search(selection["workerActor"], clause, re.I)
        before_capture = re.search(selection["before"], clause, re.I) and re.search(selection["actor"], clause, re.I)
        assertion = re.sub(table["quoteSpans"], " ", clause)
        if (worker_call or before_capture) and not re.search(selection["none"], assertion, re.I):
            calls = list(re.finditer(selection["callStart"], re.sub(
                table["quoteSpans"], lambda m: "_" * len(m.group()), clause), re.I))
            if len(calls) > 1:
                if not all(re.search(selection["orderedNext"], clause[prior.end():later.start()], re.I)
                           for prior, later in zip(calls, calls[1:])):
                    choices.add(("inlineUnknown", None))
                    continue
            qualified = re.search(r"\bbriefcase\.(?:observe|setup)\(", clause)
            python = runtime == {"Python"} or (runtime != {"Node"} and qualified)
            if python:
                selected_python = None
                for call in calls:
                    candidate = _python_choice(table, clause[call.start():])
                    if candidate is not None:
                        selected_python = candidate
                    elif call.group().endswith("observe("):
                        selected_python = None
                if selected_python:
                    choices.add(selected_python)
                continue
            if len(calls) > 1:
                clause = clause[:calls[0].start()] + clause[calls[-1].start():]
            literal = re.search(selection["value"], clause, re.I)
            instance = re.search(selection["instance"], clause, re.I)
            path = None
            if literal:
                value = next(x for x in literal.groups() if x is not None)
                kind = {"memory": "inlineMemory", "console": "inlineConsole"}.get(value, "inlineUnknown")
                if value.endswith(".jsonl") and "://" not in value:
                    kind, path = "inlineFile", value
                elif value.lower().startswith(("http://", "https://")):
                    python = runtime == {"Python"}
                    python_valid = _http_destination(value)
                    node_valid = _node_endpoint(value, allow_insecure=False)
                    if len(runtime) != 1 and python_valid != node_valid:
                        kind = "inlineUnknown"
                    else:
                        accepted = python_valid if python else node_valid
                        kind = "inlineHTTP" if accepted else ("invalidPythonWeb" if python else "inlineStartup")
                if runtime == {"Python"}:
                    kind = {"inlineConsole": "pythonConsole", "inlineFile": "pythonFile"}.get(kind, kind)
            elif instance:
                kind = selection["constructors"].get(instance.group(1), "unknownWorkerExporter")
                value = next((part for part in instance.groups()[1:] if part is not None), None)
                if kind == "inlineFile":
                    path = value
                elif kind in ("inlineHTTP", "inlineOTLP") and value is None:
                    kind = "inlineStartup" if clause[instance.end():].lstrip().startswith(")") else "inlineUnknown"
                elif kind in ("inlineHTTP", "inlineOTLP") and value is not None:
                    insecure = re.search(selection["insecureOption"], clause[instance.end():])
                    if not _node_endpoint(value, allow_insecure=bool(insecure)):
                        kind = "inlineStartup"
            elif "observe(unverifiedExporterArgument)" in clause:
                kind = "inlineUnknown"
            elif worker_call and re.search(selection["argument"], clause, re.I):
                kind = "unknownWorkerExporter"
            else:
                continue
            choices.add((kind, path))
    if wrapper_choices or any(re.search(inline["wrapperCommand"], clause) for clause in clauses):
        if any(re.search(table["wrapperFailed"], clause, re.I) for clause in clauses):
            wrapper_facts.add("wrapperRejected")
    if choices:
        wrapper_facts.discard("pythonUninstrumented")
    return commands, choices or wrapper_choices, wrapper_facts


@dataclass
class _SourceCheck:
    original_target: bool = False
    foreign_target: bool = False
    unchecked: bool = False
    ambiguous: bool = False
    correct_file: Optional[bool] = None
    outcome: Optional[str] = None

    def compare(self, result, action, facts):
        if self.foreign_target:
            return "confirmation_other_run"
        if result == "unverified":
            return None
        if self.unchecked:
            return "confirmation_unchecked"
        if self.correct_file is False:
            return "confirmation_other_file"
        if action == "ask_id" or "unresolvedOriginal" in facts:
            return "ask_id"
        if self.ambiguous:
            return "confirmation_ambiguous"
        if self.outcome and result != self.outcome:
            return "confirmation_inconsistent"
        return None


def _report_roles(spec, clause, quoted_values):
    """Compare complete declared identifiers, retaining the surrounding source and observation language."""
    scope = list(clause)
    roles = {}
    for role in ("declaredIdentifier", "lookupIdentifier", "returnedIdentifier"):
        values = []
        for first in re.finditer(spec[role], clause, re.I):
            match = first
            offset = 0
            while match:
                start, end = match.span(1)
                value = match.group(1)
                values.append(quoted_values.get(value, value))
                scope[offset + start:offset + end] = " " * (end - start)
                offset += match.end()
                match = re.match(spec["additionalIdentifier"], clause[offset:], re.I)
        roles[role] = values
    return roles, "".join(scope)


def _report_files(table, clause, quoted_values):
    values = (next(v for v in match.groups() if v is not None)
              for match in re.finditer(table["fileMentions"], clause, re.I))
    return {quoted_values.get(value, value) for value in values}


def _confirmation_conflict(table, source, result, turns, action, facts):
    """Bind an observation to its source and target before interpreting a structured outcome."""
    spec = table["confirmation"]["attribution"]
    checks = {name: _SourceCheck() for name in spec["sources"]}
    if "readDifferentPath" in facts:
        checks["jsonl"].correct_file = False
    active = set()
    original_marker = False
    original_ids = set()
    original_files = set()
    quoted_ids = {}
    quoted_values = {}
    prefix = "quoted:"
    while any(prefix in (turn or "") for turn in turns):
        prefix += "q:"

    def quoted_id(match):
        value = match.group(match.lastindex)
        if value not in quoted_ids:
            alias = prefix + str(len(quoted_ids)) + (".jsonl" if value.lower().endswith(".jsonl") else "")
            quoted_ids[value] = alias
            quoted_values[alias] = value
        return quoted_ids[value]

    reports = [re.split(spec["split"], re.sub(table["quoteSpans"], quoted_id, turn or ""), flags=re.I)
               for turn in turns]
    for clauses in reports:
        for clause in clauses:
            roles, scope = _report_roles(spec, clause, quoted_values)
            if re.search(spec["fileContext"], scope, re.I) and not re.search(spec["lookupPerformed"], scope, re.I):
                original_files.update(_report_files(table, scope, quoted_values))
            if re.search(spec["originalMarker"], scope, re.I) and not re.search(spec["uncertain"], scope, re.I):
                original_ids.update(roles["declaredIdentifier"])
                identity = _entities(table, clause).get("correlation", {})
                if identity.get("independent") and identity.get("value"):
                    original_ids.add(quoted_values.get(identity["value"], identity["value"]))
    for clauses in reports:
        for clause in clauses:
            roles, source_scope = _report_roles(spec, clause, quoted_values)
            checked = re.search(spec["lookupPerformed"], source_scope, re.I)
            original = re.search(spec["original"], source_scope, re.I)
            qualified = not re.search(spec["uncertain"], source_scope, re.I)
            if re.search(spec["originalMarker"], source_scope, re.I) and qualified:
                original_marker = True
            named = {key for key, pattern in spec["sources"].items() if re.search(pattern, source_scope, re.I)}
            clause = source_scope
            inspected = _report_files(table, source_scope, quoted_values) if checked else set()
            if checked and qualified and "jsonl" in named:
                if inspected and original_files:
                    checks["jsonl"].correct_file = inspected == original_files and len(inspected) == 1
                elif re.search(spec["originalFile"], clause, re.I):
                    checks["jsonl"].correct_file = True
            if named and re.search(spec["unchecked"], clause, re.I):
                if inspected and original_files and inspected.isdisjoint(original_files):
                    named.discard("jsonl")
                active = named
                for target in named:
                    checks[target].unchecked = True
                    checks[target].original_target = False
                    checks[target].outcome = None
                continue
            if checked and qualified and named:
                first = min(re.search(spec["sources"][key], source_scope, re.I).start() for key in named)
                if re.search(spec["exclusiveSource"], clause[:first], re.I):
                    for target in checks.keys() - named:
                        checks[target].unchecked = True
            if named and (checked or re.search(spec["observation"], clause, re.I)):
                active = named
            targets = named or active or set(spec["sources"])
            if not named and re.search(spec["runlessReport"], clause, re.I):
                targets = set(spec["sources"])
            if checked and qualified and (named or re.search(spec["reportRef"], clause, re.I)):
                for target in targets:
                    checks[target].unchecked = False
            attribution = re.sub(spec["readerContext"], "", source_scope, flags=re.I)
            attribution = re.sub(spec["notForeign"], "", attribution, flags=re.I)
            foreign = re.search(spec["foreign"], attribution, re.I)
            explicit = (re.search(spec["reportedForeign"], clause, re.I)
                        or re.search(spec["foreignLookup"], clause, re.I))
            query_ids = set(roles["lookupIdentifier"])
            row_ids = set(roles["returnedIdentifier"])
            comparable = original_ids and query_ids
            if comparable and original_ids != query_ids:
                foreign, explicit = True, True
            original_target = original or original_marker or (comparable and original_ids == query_ids)
            if checked and qualified and original_target and not (foreign and explicit):
                for target in targets:
                    checks[target].original_target = True
            ambiguous_rows = re.search(spec["ambiguousRows"], clause, re.I)
            if not (checked or ambiguous_rows or re.search(spec["observation"], clause, re.I)
                    or re.search(spec["exclusiveOther"], clause, re.I)):
                continue
            for target in targets:
                check = checks[target]
                # Foreign rows can be returned by a search for the original identifier.
                contents = check.original_target and (re.search(spec["returnedContents"], clause, re.I)
                                                      or re.search(spec["reportedContents"], clause, re.I))
                if foreign and (explicit or not contents):
                    check.foreign_target = True
                    check.original_target = False
                    check.outcome = None
                    continue
                same_id = query_ids and original_ids == query_ids
                identified = original or same_id
                correction = (qualified and identified and (named or re.search(spec["reportRef"], clause, re.I))
                              and (checked or re.search(spec["identifiesOriginal"], clause, re.I)))
                if correction:
                    check.foreign_target = False
                    check.original_target = True
                if correction and checked:
                    check.ambiguous = False
                if ambiguous_rows or (original_ids and len(original_ids) != 1 and (query_ids or row_ids)):
                    check.ambiguous = True
                if not check.foreign_target and (check.original_target or (row_ids and original_ids)):
                    if re.search(spec["miss"], clause, re.I):
                        check.outcome = "no-match"
                    elif qualified and len(original_ids) == 1 and row_ids:
                        count = sum(value in original_ids for value in roles["returnedIdentifier"])
                        if count > 1:
                            check.ambiguous = True
                        check.outcome = "matched" if count else "no-match"
                    elif qualified and re.search(spec["originalMatch"], clause, re.I):
                        check.outcome = "matched"
                    elif re.search(spec["exclusiveOther"], clause, re.I):
                        check.outcome = "no-match"
                    elif qualified and not foreign and re.search(spec["match"], clause, re.I):
                        check.outcome = "matched"
    return checks[source].compare(result, action, facts)


def route(table: Dict[str, Any], question: str, answer: Optional[str] = None,
          shell: Optional[str] = None, inherited: Optional[Dict[str, str]] = None,
          confirmation: Optional[Tuple[str, str]] = None, privacy_notes: Tuple[str, ...] = ()) -> Tuple[str, str]:
    key, facts = _next_step(table, question, answer, shell, inherited, privacy_notes)
    if confirmation:
        source, result = confirmation
        outcome = table["confirmation"]["outcomes"].get(source, {}).get(result)
        if outcome:
            key = _confirmation_conflict(table, source, result, (question, answer), key, facts) or outcome
    return key, table["actions"][key]["text"]


def _next_step(table, question, answer=None, shell=None, inherited=None, privacy_notes=()):
    def preprocess(value: str, private: Optional[bool] = None) -> str:
        sensitive = table["sensitiveInput"]
        if private is None:
            private = _sensitive(table, value)

        value = re.sub(table["destinationAlternative"], " ", value, flags=re.I)
        if private:
            value = re.sub(sensitive["credentialBinding"], " ", value, flags=re.I)

        def web_label(match: re.Match[str]) -> str:
            url = match.group(1)
            if re.search(table["uncertainExplicitUrl"], url, re.I):
                return " explicit unverified web exporter "
            node = re.search(table["runtimeSource"]["Node"], value, re.I)
            valid = _node_endpoint(url, allow_insecure=False) if node else _http_destination(url)
            return " explicit valid web exporter " if valid else " explicit rejected web exporter "

        for direct in table["explicitUrls"]:
            value = re.sub(direct["pattern"], web_label, value, flags=re.I)
        if private:
            clauses = re.split(table["split"], value, flags=re.I)
            relevant = " ".join(c for c in clauses if not _matches(table["excludeClauses"], c)
                                and not re.search(sensitive["unconfirmed"], c, re.I))
            fixed = next((item["replacement"] for item in sensitive["destinations"]
                         if re.search(item["pattern"], relevant, re.I)), None)
            kinds = _positive_destinations(table, [_source_text(table, relevant)])
            return fixed or ("original run used " + kinds.pop() if len(kinds) == 1 else "original exporter unknown")
        for change in table["preprocess"]:
            value = re.sub(change["pattern"], change["replacement"], value, flags=re.I)
        return value.strip()

    source_question = _original_commands(table, _source_text(table, question))
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
    facts = _collect_facts(table, evidence)
    if source_question != question:
        # Normalize URLs and filenames only after opaque argument spans have been removed.
        source = preprocess(source_question, private=_sensitive(table, question))
        disowned = {rule["fact"] for rule in table["disownedObservations"] if re.search(rule["pattern"], source, re.I)}
        facts = (facts & set(table["rawFacts"])) | _collect_facts(table, _scoped(table, source))
    facts.update(disowned)
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
    commands, choices, wrapper_facts = _execution_evidence(table, source_question, inherited)
    facts.update(wrapper_facts)
    selected = None
    inherited_preload = inherited is not None and bool(re.search(inline["preloadSource"], evidence, re.I))
    if len(commands) == 1 or (not commands and inherited_preload):
        # Parse literal assignment tokens as data. Last assignment to a key wins.
        settings = {}
        for match in re.finditer(inline["assignment"], commands[0].group(1) if commands else ""):
            key = match.group(1)
            if key not in inline["keys"]:
                continue
            raw = next((v for v in match.groups()[1:] if v is not None), "")
            if key == "NODE_OPTIONS":
                settings["REGISTER_FROM_NODE_OPTIONS"] = _node_register(inline, " " + raw, options=True)
            elif key in ("BRIEFCASE_EXPORTER", "BRIEFCASE_AUTO") or key in inline["endpoints"]:
                settings[key] = raw.strip()
            else:
                # Node selects the first nonempty raw protocol before trimming it.
                settings[key] = "grpc" if raw.strip().lower() == "grpc" else ("http/json" if raw else "")
        if inherited is not None:
            settings = {**inherited, **settings}
        launch_kind = _command_register(inline, commands[0]) if commands else ""
        option_known = "REGISTER_FROM_NODE_OPTIONS" in settings
        option_kind = settings.pop("REGISTER_FROM_NODE_OPTIONS", "")
        if "loader" in (launch_kind, option_kind) and not ({launch_kind, option_kind} & {"argv", "register"}):
            facts.add("loaderOnly")
        if "unexported" in (launch_kind, option_kind):
            facts.add("preloadImportError")
        explicit_preload = launch_kind == "argv" or re.search(inline["explicitPreload"], evidence, re.I)
        from_options = not explicit_preload and (option_kind == "register"
                                                 or re.search(inline["nodeOptionsOnly"], evidence, re.I))
        if commands and not explicit_preload and option_known and not option_kind:
            settings["BRIEFCASE_AUTO"] = "0"
        if from_options and settings.get("BRIEFCASE_AUTO", "").lower() not in ("1", "true"):
            settings["BRIEFCASE_AUTO"] = "0"
        choice = settings.get("BRIEFCASE_EXPORTER", "")
        if choice.lower().startswith(("http://", "https://")):
            safe_direct = _node_endpoint(choice, allow_insecure=False)
            settings["DIRECT_WEB"] = "valid" if safe_direct else "unverified"
        endpoint = next((settings[k].strip() for k in inline["endpoints"] if settings.get(k, "").strip()), "")
        if endpoint:
            settings["EFFECTIVE_ENDPOINT"] = "valid" if _node_endpoint(endpoint) else "unknown"
            if not settings.get("BRIEFCASE_EXPORTER"):
                settings["BRIEFCASE_EXPORTER"] = "otlp"
        elif "BRIEFCASE_EXPORTER" in settings and not settings["BRIEFCASE_EXPORTER"]:
            settings["BRIEFCASE_EXPORTER"] = "console" if inherited is not None else "unresolved"
        lines = "\n".join(
            f"{key}={value}" for key, value in settings.items() if "\n" not in value and "\r" not in value
        )
        selected = next((rule for rule in inline["rules"] if _matches(rule, lines)), None)
        if "loaderOnly" in facts:
            selected = None
        if selected:
            facts.add(selected["fact"])
            if selected["fact"] == "inlineFile":
                inline_path = None if _sensitive(table, question) else settings["BRIEFCASE_EXPORTER"]
    elif commands:
        facts.add("inlineUnknown")
    if inherited_preload and re.search(r"\bworker thread\b", evidence, re.I):
        if not facts.intersection(("workerForward", "workerCreatedFirst")):
            facts.add("workerUnknown")
    if choices:
        facts.discard("loaderOnly")
        facts.difference_update(("workerUnknown", "workerForward"))
        facts.difference_update(rule["fact"] for rule in inline["rules"])
        kind, inline_path = next(iter(choices)) if len(choices) == 1 else ("inlineUnknown", None)
        facts.add(kind)
        selected = {"fact": kind}
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
        reply_source = answer
        if _sensitive(table, answer) or re.search(table["privateWebAddress"], answer, re.I):
            aside = re.sub(table["sensitiveAsideSplit"], "\n", answer, flags=re.I)
            reply_source = "\n".join(c for c in re.split(table["split"], aside, flags=re.I)
                                     if not (_sensitive(table, c) or re.search(table["privateWebAddress"], c, re.I)))
        if key in ("ask_path", "ask_exporter"):
            path_lines = [line.strip() for line in reply_source.splitlines() if line.strip()]
            if len(path_lines) > 1 and _matches(table["replies"]["ask_path"][0], path_lines[0]) and all(
                re.search(table["confirmation"]["attribution"]["foreign"], line, re.I) for line in path_lines[1:]
            ):
                reply_source = path_lines[0]
        safe_choice = key in ("ask_exporter", "ask_protocol") and any(
            re.fullmatch(pattern, reply_source.strip(), re.I) for pattern in table["exporterReplies"].values())
        safe_path = key in ("ask_exporter", "ask_path") and _matches(
            table["replies"]["ask_path"][0], reply_source.strip()
        )
        projected = _source_text(table, answer)
        selection = table["applicationSelection"]
        pieces = re.split(f"({table['split']})", projected, flags=re.I)
        reply_runtime = _runtime_names(table, pieces[::2]) & {"Python", "Node"}
        for index in range(0, len(pieces), 2):
            clause = pieces[index]
            if (re.search(table["sensitiveInput"]["credentialBinding"], clause, re.I)
                    and re.search(selection["before"], clause, re.I)
                    and re.search(selection["actor"], clause, re.I)
                    and (reply_runtime == {"Python"} or re.search(r"\bbriefcase\.observe\(", clause, re.I))):
                clause = _python_exporter_call(table, clause)
            pieces[index] = re.sub(table["sensitiveInput"]["credentialBinding"], " ", clause, flags=re.I)
        launch_reply = _original_commands(table, "".join(pieces))
        launches, calls, preflight = _execution_evidence(table, launch_reply, inherited)
        asserted = any(
            re.search(table["executionAssertionSubject"], clause, re.I)
            and not _collect_facts(table, clause).isdisjoint(("console", "memory", "http", "otlp",
                                                              "rejectedWeb", "explicitWeb"))
            for clause in re.split(table["split"], _scoped(table, preprocess(launch_reply)), flags=re.I)
        )
        shell_reply = (shell is not None and not (safe_choice or safe_path)
                       and _shell_matches(table, _scoped(table, question + "\n" + launch_reply))
                       and (re.search(selection["callStart"], launch_reply, re.I)
                            or _shell_matches(table, _scoped(table, launch_reply))))
        safe_launch = (key in ("ask_exporter", "ask_protocol") and
                       (len(launches) == 1 or bool(calls or preflight) or shell_reply
                        or (asserted and not safe_choice)))
        unsafe_reply = ((_sensitive(table, question) and not (safe_choice or safe_path or safe_launch))
                        or any(value in reply_source for value in raw_private))
        if safe_launch and not unsafe_reply:
            # Reuse the first-turn routing path with the accepted original launch as the clarification.
            # The combined session has no pending answer, so this cannot trigger another clarification.
            choice, combined_facts = _next_step(table, question + "\n" + launch_reply, shell=shell, inherited=inherited,
                                                privacy_notes=(*privacy_notes, answer))
            if table["actions"][choice]["kind"] == "ask":
                choice = table["fallback"]
            return choice, combined_facts
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
        if key == "ask_id" and not valid_id:
            facts.add("unresolvedOriginal")
        if not reply or re.fullmatch(table["unknown"], reply, re.I) or valid is None or not valid_id:
            key = table["fallback"]
        else:
            if key in ("ask_path", "ask_exporter"):
                shell_path = None
            entities.update(reply_entities)
            evidence = contextual_reply + "\n" + evidence
            if key in ("ask_subject", "ask_run") and not valid.get("action"):
                facts.update(_collect_facts(table, _scoped(table, preprocess(projected))))
                facts.update(preflight)
                if len(launches) == 1 or calls or not facts.isdisjoint(("file", "jsonl")):
                    combined, combined_facts = _next_step(table, question + "\n" + launch_reply, shell=shell,
                                                          inherited=inherited)
                    if table["actions"][combined]["kind"] != "ask":
                        return combined, combined_facts
            facts.difference_update(valid.get("drop", []))
            facts.update(valid.get("add", []))
            key = valid.get("action") or _select(table, facts)
            unresolved_path = key in table["requiresPath"] and not (shell_path or entities.get("path", {}).get("value"))
            if table["actions"][key]["kind"] == "ask" or unresolved_path:
                key = table["fallback"]
            if key == "reply":
                facts.update(_collect_facts(table, _source_text(table, reply_source))
                             & {"replyOnStdout", "replyOnStderr"})
    if key == "reply" and "replyOnStderr" in facts:
        key = "reply_streams" if "replyOnStdout" in facts else "reply_stderr"
    return key, facts


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


def _node_ipv4(host: str) -> Optional[str]:
    """The WHATWG IPv4 host grammar accepts one to four decimal, octal or hexadecimal parts."""
    from ipaddress import IPv4Address

    labels = host.removesuffix(".").split(".")
    if not re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]*)", labels[-1]):
        return host
    if len(labels) > 4:
        return None
    numbers = []
    for label in labels:
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]*|0[0-7]*|[1-9][0-9]*)", label):
            return None
        base = 16 if label.lower().startswith("0x") else (8 if label.startswith("0") else 10)
        digits = label[2:] if base == 16 else label
        # A URL cannot represent an IPv4 part larger than 32 bits.
        if len(digits.lstrip("0")) > 12:
            return None
        numbers.append(int(digits.lstrip("0") or "0", base))
    if any(value > 255 for value in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    value = numbers[-1] + sum(number << (8 * (3 - i)) for i, number in enumerate(numbers[:-1]))
    return str(IPv4Address(value))


def _node_endpoint(endpoint: str, *, allow_insecure: bool = True) -> bool:
    """Classify the reported HTTP transport using its normalized host, independently of the URL path/query."""
    from ipaddress import IPv6Address
    from urllib.parse import unquote, urlsplit

    value = endpoint.strip().replace("\t", "").replace("\r", "").replace("\n", "")
    try:
        parts = urlsplit(value.replace("\\", "/"))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        _ = parts.port  # Validate port syntax and range without resolving the name.
        host = unquote(parts.hostname, errors="strict").lower()
        normalized: Optional[str]
        if ":" in parts.hostname:
            if "%" in parts.hostname:
                return False
            normalized = str(IPv6Address(host))
        else:
            if any(ord(char) <= 32 or ord(char) == 127 or char in "#%/:<>?@[\\]^|" for char in host):
                return False
            ascii_host = host.encode("idna").decode("ascii")
            for label in ascii_host.split("."):
                if label.startswith("xn--"):
                    label.encode("ascii").decode("idna")
            normalized = _node_ipv4(ascii_host)
            if normalized is None:
                return False
    except (UnicodeError, ValueError):
        return False
    loopback = normalized in ("localhost", "::1") or bool(re.fullmatch(r"127(?:\.[0-9]{1,3}){3}", normalized))
    return parts.scheme == "https" or allow_insecure or loopback


def _shell_exporter(question: str = "") -> str:
    """Describe only configuration shared by the plausible original runtimes."""
    env = os.environ
    value = env.get("BRIEFCASE_EXPORTER", "").strip()
    node = bool(re.search(r"\b(?:node(?:js)?|npm|npx|preload|javascript|typescript)\b|\.[cm]?js\b", question, re.I))
    python = bool(re.search(r"\b(?:python[23]?|pip)\b|\.py\b", question, re.I))
    if node and re.search(r"\bworker[_ ]threads?\b", question, re.I):
        return "check-launch-config"
    if value.lower().startswith(("http://", "https://")):
        direct = _node_endpoint(value, allow_insecure=False) if node and not python else _http_destination(value)
        return "HTTP" if direct else "check-launch-config"
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
    if node_target == "otlp" and endpoint and not _node_endpoint(endpoint):
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


class _SessionError(ValueError):
    """All messages are public constant diagnostics."""


def _session(table, args, turns):
    if bool(args.source) != bool(args.result):
        raise _SessionError("Confirmation needs both --source and --result.")
    if args.answer and len(args.answer) != 1:
        raise _SessionError("Supply one clarification answer, once.")
    answer = args.answer[0] if args.answer else None
    pair = None
    if args.source:
        if (len(args.source) != 1 or len(args.result) != 1
                or args.result[0] not in table["confirmation"]["outcomes"].get(args.source[0], {})):
            raise _SessionError("Choose exactly one supported source and one observed result. See --help.")
        pair = (args.source[0], args.result[0])
    lines = ([args.question] if args.question is not None else []) + list(turns)
    while lines and not lines[-1].strip():
        lines.pop()
    reports = [(index, item) for index, line in enumerate(lines) if (item := _confirmation(table, line))]
    if ((answer and _confirmation(table, answer)) or
            (reports and (pair or len(reports) != 1 or reports[0][0] != len(lines) - 1))):
        raise _SessionError("Supply one source and result together, once, at the end of the session.")
    if reports:
        pair = reports[0][1]
        lines.pop()
    if len(lines) > (1 if answer is not None else 2):
        raise _SessionError("Supply one clarification answer, once.")
    return (lines[0] if lines else ""), (answer if answer is not None else next(iter(lines[1:]), None)), pair


def cmd_record_help(args, store, engine, stack) -> int:
    try:
        return _guide_session(args)
    except _SessionError as error:
        print(str(error), file=sys.stderr)
        return 2


def _session_environment(table, question):
    launch = _scoped(table, _original_commands(table, _source_text(table, question)))
    runtime = " ".join(_runtime_names(table, re.split(table["split"], launch, flags=re.I)))
    shell = _shell_exporter(runtime) if _shell_matches(table, launch) else None
    inherited = None
    if shell is not None:
        # Node overlays command assignments on the launch environment. Only exporter vocabulary is needed here.
        inherited = {}
        auto_flag = os.environ.get("BRIEFCASE_AUTO", "").strip().lower()
        if auto_flag in ("0", "false", "1", "true"):
            inherited["BRIEFCASE_AUTO"] = auto_flag
        options = _node_register(table["inlineNode"], " " + os.environ.get("NODE_OPTIONS", ""), options=True)
        if options:
            inherited["REGISTER_FROM_NODE_OPTIONS"] = options
        for key in ("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"):
            protocol = os.environ.get(key, "")
            if protocol:
                inherited[key] = protocol.strip().lower() or "unspecified"
        for key in table["inlineNode"]["endpoints"]:
            if os.environ.get(key):
                inherited[key] = os.environ[key]
        for key in ("OTEL_SDK_DISABLED", "BRIEFCASE_AUTO_SKIP", "BRIEFCASE_CAPTURE_CONTENT", "BRIEFCASE_AUTO_DEDUPE"):
            if key in os.environ:
                inherited[key] = os.environ[key]
        choice = os.environ.get("BRIEFCASE_EXPORTER", "").strip()
        if choice:
            inherited["BRIEFCASE_EXPORTER"] = choice
    return shell, inherited


def _guide_session(args) -> int:
    table = json.loads(_TABLE.read_text(encoding="utf-8"))
    interactive = _ux.is_interactive() and sys.stdin.isatty()
    turns: List[str] = []

    def read_turn(prompt: str):
        line = _line(prompt, True)
        turns.append(line)
        if _confirmation(table, line):
            end_key = "Ctrl+Z, then Enter" if os.name == "nt" else "Ctrl+D"
            print(f"Press {end_key} on an empty line to finish this report: ", end="", flush=True)
            # A blank line can occur inside a paste. Only EOF delimits the full report.
            for remainder in sys.stdin:
                if remainder.strip():
                    turns.append(remainder.strip())
                    break
        return _session(table, args, turns)

    if not sys.stdin.isatty():
        try:
            turns = sys.stdin.read().splitlines()
        except OSError:
            pass
    question, answer, confirmation = _session(table, args, turns)
    if not args.static and interactive and not question and not confirmation:
        question, answer, confirmation = read_turn(
            "What are you trying to find, and what do you know about the original run? "
        )
    if args.static or (not question and not confirmation):
        print(table["guide"])
        return 0
    shell, inherited = _session_environment(table, question)
    key, text = route(table, question, None, shell, inherited)
    if not confirmation and not args.answer and table["actions"][key]["kind"] == "ask":
        print(text, flush=True)
        if answer is None:
            if not interactive:
                return 0
            question, answer, confirmation = read_turn("Answer (or unknown): ")
    if answer is not None and table["actions"][key]["kind"] == "ask":
        shell, inherited = _session_environment(table, question + "\n" + answer)
    key, text = route(table, question, answer, shell, inherited, confirmation, tuple(turns))
    prompted = not confirmation and interactive and key in table["confirmation"]["next"]
    if prompted:
        print(text, flush=True)
        print(table["confirmation"]["instruction"])
        question, answer, confirmation = read_turn("Optional source and result (Enter to skip): ")
        key, text = route(table, question, answer, shell, inherited, confirmation, tuple(turns))
    if not prompted or confirmation:
        print(text)
    if not prompted and key in table["confirmation"]["next"]:
        print(table["confirmation"]["instruction"])
    if key == table["fallback"]:
        print("\n" + table["guide"])
    elif key in ("fresh", "future", "memory_lost"):
        print("Open the example with: briefcase-ai record-help --static")
    return 0
