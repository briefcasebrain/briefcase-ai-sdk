"""Local, session-only guidance for finding a decision from an earlier run."""
from __future__ import annotations

import ast
from functools import lru_cache
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from briefcase.cli import _ux
from briefcase.cli._record_observation import SourceCheck as _SourceCheck

GROUP = "start"
ORDER = 25
_TABLE = Path(__file__).with_name("record_guidance_v1.json")


def register(sub) -> None:
    parser = sub.add_parser(
        "record-help",
        help="Find an existing decision using evidence from the original run.",
        description="Describe what you want to find and what you know about the original run. Pass a question "
                    "as an argument or pipe the question and any answer on separate lines. Input stays local.",
        epilog="To report a lookup in a terminal or pipe, put SOURCE RESULT READER SOURCE_MATCH TARGET_MATCH "
               "[COVERAGE] on one line. For a complete JSONL search with no match, enter jsonl no-match me "
               "original original complete. If the command asks a follow-up about that lookup, add evidence "
               "VALUE on a final line. Use yes, no or unsure for VALUE.",
    )

    def private_syntax_error(_message: str) -> None:
        parser.exit(2, "Invalid record-help arguments. See record-help --help.\n")

    parser.error = private_syntax_error
    parser.add_argument("question", nargs="?", help="Question and known facts about the original run.")
    parser.add_argument("--answer", action="append", help="Answer to the guide's one clarification.")
    parser.add_argument("--evidence-reply", action="append", metavar="{yes,no,unsure}",
                        help="Answer to the follow-up about your reported lookup. Use only if the command asks.")
    parser.add_argument("--verify-jsonl", metavar="FILE", action="append",
                        help="Check a local JSONL file for the original decision ID. Enter the ID at the hidden prompt "
                             "or send it as one line on stdin.")
    parser.add_argument("--static", action="store_true", help="Print the complete offline guide.")
    parser.add_argument("--source", metavar="{jsonl,receiver,storage}", action="append",
                        help="Source inspected for the original decision ID or run-unique marker. Pair with --result.")
    parser.add_argument("--result", metavar="{matched,no-match,unverified}", action="append",
                        help="Observed result at that source. Use matched only for an unambiguous original match.")
    for flag, values, meaning in (
        ("reader", "me,another,unverified", "Who performed the lookup."),
        ("source-match", "original,different,unverified", "Whether you checked the source used by the original run."),
        ("target-match", "original,different,unverified",
         "Whether you looked up the original decision ID or unique marker."),
        ("coverage", "complete,partial,unverified",
         "How much of the source you searched. Report complete to establish no match."),
    ):
        parser.add_argument("--" + flag, metavar="{" + values + "}", action="append", help=meaning)
    parser.set_defaults(func=cmd_record_help)


def _matches(rule: Dict[str, Any], evidence: str) -> bool:
    alternatives = rule.get("any", [])
    flags = 0 if rule.get("caseSensitive") else re.I
    return (all(re.search(pattern, evidence, flags) for pattern in rule.get("all", []))
            and (not alternatives or any(re.search(pattern, evidence, flags) for pattern in alternatives))
            and not any(re.search(pattern, evidence, flags) for pattern in rule.get("none", [])))


def _file_read_operand(table, before):
    """The filename occupies the read verb's object span and is not a stream or a label."""
    spec = table["lookupEvidence"]
    return any(re.fullmatch(spec["operandWords"], before[verb.end():], re.I)
               for verb in re.finditer(spec["fileVerb"], before, re.I))


def _source_text(table, text, path_aliases=None):
    """Opaque values can correlate observations. Their spelling cannot select a source."""
    spans = {(match.start(1), match.end(1)) for pattern in table["opaqueBindings"]
             for match in re.finditer(pattern, text, re.I)}
    file_spans = []
    # A literal --to value belongs to the launch wrapper. Child program arguments do not.
    wrapper_paths = set()
    wrapper_children = set()
    for wrapper in re.finditer(table["inlineNode"]["wrapperCommand"], text):
        wrapper_children.add(wrapper.span(3))
        for option in re.finditer(table["inlineNode"]["wrapperOption"], wrapper.group(2)):
            if option.group(1) == "--to" and option.group(2):
                start, end = option.span(2)
                wrapper_paths.add((wrapper.start(2) + start, wrapper.start(2) + end))
    for quoted in re.finditer(table["quoteSpans"], text):
        index = quoted.lastindex
        if quoted.span() in wrapper_children:
            component = re.search(r"[^/\\]+$", quoted.group(index))
            if component:
                spans.add((quoted.start(index), quoted.start(index) + max(0, component.start() - 1)))
            continue
        if quoted.span() in wrapper_paths and quoted.group(index).endswith(".jsonl"):
            file_spans.append(quoted.span(index))
            continue
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
        if not ((start, end) in wrapper_paths or continued or _file_read_operand(table, clause)
                or re.search(table["fileRoleBefore"], clause, re.I)
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
        if path_aliases is not None:
            path_aliases.setdefault("".join(chars[start:end]), set()).add(text[start:end])
    return "".join(chars)


def _positive_destinations(table, clauses):
    return {name for name, assertions in table["positiveDestinations"].items()
            if any(re.search(pattern, clause, re.I) for clause in clauses for pattern in assertions)}


def _stream_artifacts(spec, clause):
    """Bind each artifact to its stream operand, including when two streams are described together."""
    bindings = set()
    mentions = sorted((token.start(), token.end(), name) for name, pattern in spec["streams"].items()
                      for token in re.finditer(pattern, clause, re.I))
    for i, (start, end, stream) in enumerate(mentions):
        left = clause[mentions[i - 1][1] if i else 0:start]
        right = clause[end:mentions[i + 1][0] if i + 1 < len(mentions) else len(clause)]
        forward = re.match(spec["forward"], right, re.I)
        backward = re.search(spec["backward"], left, re.I)
        operands = []
        if forward:
            operands.append(re.split(spec["cut"], forward.group(1), maxsplit=1, flags=re.I)[0])
        if backward:
            operands.append(re.split(spec["cut"], left[:backward.start()], flags=re.I)[-1])
        for artifact, pattern in spec["artifacts"].items():
            if any(re.search(pattern, operand, re.I) for operand in operands):
                bindings.add(artifact + "On" + stream)
    return bindings


def _receiver_readback(spec, clauses):
    """Bind a receiver inspection and matching entry, allowing either reporting order."""
    roles = []
    for clause in clauses:
        invalid = (re.search(spec["denied"], clause, re.I) or re.search(spec["observationDenial"], clause, re.I)
                   or re.search(spec["otherObservationSource"], clause, re.I)
                   or re.search(spec["question"], clause, re.I))
        inspected = not invalid and _matches(spec["receiverRead"], clause)
        identity = (re.search(spec["idRole"], clause, re.I) and re.search(spec["originReceipt"], clause, re.I)
                    and re.search(spec["receiverRecordRelation"], clause, re.I))
        record = (re.search(spec["receiverRecordNoun"], clause, re.I)
                  or re.match(spec["receiverReference"], clause, re.I))
        entry = not invalid and (_matches(spec["receiverRecord"], clause) or (identity and record))
        original_query = inspected and re.search(spec["receiverQueryTarget"], clause, re.I)
        matching = _matches(spec["matchingResult"], clause) or re.search(spec["matchingIncoming"], clause, re.I)
        same_result = not invalid and record and matching
        roles.append((inspected, original_query, entry, same_result))
    for index, (inspected, original_query, _, _) in enumerate(roles):
        if not inspected:
            continue
        for _, _, original_entry, same_result in roles[max(0, index - 1):index + 2]:
            if original_entry or (original_query and same_result):
                return True
    return False


def _lookup_evidence(table, evidence):
    """Extract source, actor and action separately before asking whether a lookup still lacks information."""
    spec = table["lookupEvidence"]
    facts = set()
    original_event = False
    for statement in re.split(r"[.;!?]\s+|\n", evidence):
        retention = re.search(spec["discardContrast"], statement, re.I)
        if retention and not re.search(spec["denied"], statement[:retention.start()] + statement[retention.end():],
                                       re.I):
            facts.add("discardedStderr")
    clauses = [c for c in re.split(table["split"], evidence, flags=re.I) if c.strip()]
    want_first = any(re.search(spec["question"], c, re.I) and re.search(spec["wantFirst"], c, re.I) for c in clauses)
    projected = [_source_text(table, c) for c in clauses]
    if _receiver_readback(spec, projected):
        attribution = table["confirmation"]["attribution"]
        outside = any(re.search(attribution["outsideInstance"], clause, re.I)
                      and re.search(attribution["sources"]["receiver"], clause, re.I) for clause in projected)
        facts.add("foreignReceiverSource" if outside else "retainedReceiverEntry")
    for clause, source in zip(clauses, projected):
        if re.search(spec["denied"], source, re.I) or re.search(spec["question"], source, re.I):
            original_event = False
            continue
        reference = spec["referencedSearch"]
        attribution = table["confirmation"]["attribution"]
        if (re.search(reference["reader"], source, re.I) and re.search(attribution["sourceByReference"], source, re.I)
                and re.search(attribution["lookupTargetRole"], source, re.I)):
            facts.update(fact for kind, fact in reference["sources"].items()
                         if re.search(attribution["sources"][kind], source, re.I))
        facts.update(_stream_artifacts(spec["sourceSlots"], source))
        receipt = re.search(spec["originReceipt"], source, re.I)
        for observation in spec["observations"]:
            if _matches(observation, source):
                facts.update(observation["add"])
        if re.search(spec["exportedDecisionToConsole"], source, re.I):
            facts.add("consoleDecisionExport")
        roles = {name for name in ("originalActor", "emission", "idRole", "runtimeActor", "configure",
                                   "consoleRole", "reader") if re.search(spec[name], source, re.I)}
        linked_event = original_event and re.search(spec["eventLink"], source, re.I)
        actor = "originalActor" in roles or receipt or linked_event or (want_first and re.search(
            spec["earlierActor"], source, re.I))
        declared = re.search(spec["declaredReceiptID"], source, re.I)
        if actor and ("idRole" in roles or declared) and ("emission" in roles or receipt or declared):
            identity = _entities(table, clause).get("correlation", {})
            if identity.get("value"):
                facts.update(("originalEmittedID", "hasID"))
        original_event = "originalActor" in roles or bool(receipt)
        if {"runtimeActor", "configure", "consoleRole"} <= roles and not re.search(spec["packageOnly"], source, re.I):
            facts.update(("consoleInstalled", "console"))
        if "reader" in roles and any(_file_read_operand(table, clause[:match.start()])
                                     for match in re.finditer(table["fileMentions"], source, re.I)):
            facts.add("readFileOperand")
    return facts


def _other_reader(spec, clause, event):
    actor = clause[:event.start()] if event else ""
    return (re.search(spec["foreignReader"], clause, re.I)
            or (re.search(spec["possessedReader"], actor, re.I)
                and not re.search(spec["firstPersonActor"], actor, re.I)
                and not re.search(spec["independentDevice"], actor, re.I)))


def _storage_lookup(table, evidence):
    """Relate a completed store query, the identifier searched and the returned full record."""
    spec = table["readbackPair"]
    attribution = table["confirmation"]["attribution"]
    evidence_spec = table["lookupEvidence"]
    reports, quoted = _report_clauses(table, [evidence])
    statements = [_report_roles(attribution, clause, quoted) for clause in reports[0] if clause.strip()]
    originals = {value for roles, _ in statements for value in roles["declaredIdentifier"]}
    events = [(roles, scoped) for roles, scoped in statements
              if not re.search(evidence_spec["question"], scoped, re.I)
              and (not roles["declaredIdentifier"] or re.search(spec["query"], scoped, re.I)
                   or re.search(spec["returned"], scoped, re.I))]
    lookups = {}
    records = {}
    compared_original = set()
    durable_queries = set()
    foreign_comparisons = set()
    pending_at = -1
    intervening_status = set()
    foreign_reader = False
    for index, (roles, scoped) in enumerate(events):
        clause = _source_text(table, scoped)
        event = re.search(attribution["lookupPerformed"], clause, re.I) or re.search(
            attribution["resultActor"], clause, re.I)
        if _other_reader(attribution, clause, event):
            foreign_reader = True
            intervening_status.add(index)
            continue
        if re.search(attribution["detailSelf"], clause, re.I):
            foreign_reader = False
        if foreign_reader:
            continue
        if re.fullmatch(attribution["completedStatus"], clause, re.I):
            intervening_status.add(index)
            continue
        if re.search(attribution["detailPending"], clause, re.I):
            pending_at = index
            intervening_status.add(index)
            continue
        stages = attribution["rowStages"]
        if lookups and (re.match(stages["resultReference"], clause.strip(), re.I)
                        or re.search(stages["objectReference"], clause, re.I)):
            named = {kind for kind, pattern in attribution["sources"].items() if re.search(pattern, clause, re.I)}
            if named and "storage" not in named:
                records.clear()
                continue
        comparison = re.search(spec["identityComparison"], clause, re.I)
        compared_to = clause[comparison.end():] if comparison else ""
        if lookups and comparison and re.search(spec["foreignComparison"], compared_to, re.I):
            if not re.search(spec["otherSource"], clause, re.I):
                lookups[index] = (False, set())
                foreign_comparisons.add(index)
            continue
        if re.search(attribution["foreign"], clause, re.I) and re.search(attribution["reportedForeign"], clause, re.I):
            if lookups and not re.search(spec["otherSource"], clause, re.I):
                lookups[index] = (False, set())
            continue
        if _returned_result(attribution, clause.strip(), roles) in ("match", "miss", "mixed_match"):
            intervening_status.add(index)
        if re.search(spec["denied"], clause, re.I) or re.search(spec["otherSource"], clause, re.I):
            continue
        if (comparison and re.search(spec["originalComparison"], compared_to, re.I)
                and re.search(spec["equalComparison"], clause[comparison.start():], re.I)):
            compared_original.add(index)
        read = (re.search(spec["query"], clause, re.I) or re.search(evidence_spec["reportedRead"], clause, re.I)
                or (re.search(evidence_spec["reader"], clause, re.I)
                    and re.search(evidence_spec["readEvent"], clause, re.I)
                    and re.search(evidence_spec["independentReader"], clause, re.I)
                    and re.search(evidence_spec["actualRead"], clause, re.I)))
        if read and re.search(spec["source"], clause, re.I):
            if re.search(spec["durableSource"], clause, re.I):
                durable_queries.add(index)
            target = set(roles["lookupIdentifier"])
            if target and originals:
                lookups[index] = (target == originals and len(target) == 1, target)
            elif re.search(spec["original"], clause, re.I):
                lookups[index] = (True, target or originals)
            else:
                lookups[index] = (None, target)
        returned_record = (re.search(spec["returned"], clause, re.I)
                           or re.search(evidence_spec["returnedContainer"], clause, re.I))
        if re.search(spec["record"], clause, re.I) and returned_record:
            returned = set(roles["returnedIdentifier"])
            original = bool(re.search(evidence_spec["originalRow"], clause, re.I))
            records[index] = (returned, original, bool(re.search(spec["same"], clause, re.I)))
    if lookups:
        latest = max(lookups)
        valid, target = lookups[latest]
        if latest in foreign_comparisons:
            return "foreignStoreComparison"
        if valid is False:
            return "differentStoreLookup"
        different_row = False
        for position, (returned, original, same) in records.items():
            original = original or position in compared_original or (
                position + 1 in compared_original and position + 1 not in lookups)
            if not (returned or original or same):
                continue
            # Prefer the preceding lookup if a record sits between two separate reads.
            owner = next((i for i in (position, position - 1, position + 1) if i in lookups), None)
            # A pending status updates this attempt without starting another one.
            if (owner is None and position > latest
                    and all(i in intervening_status for i in range(latest + 1, position))):
                owner = latest
            if owner == latest and pending_at <= position:
                if valid is None and not original:
                    continue
                if returned and not target and not original:
                    continue
                if target and returned and returned != target:
                    different_row = True
                else:
                    return "linkedStoreReadback"
        if different_row:
            return "storageIDMismatch"
        if valid is not None:
            return "pendingStoreReadback"
        if latest in durable_queries and not any(
            re.search(spec["unknownTarget"], scope, re.I) for _, scope in statements
        ):
            return "durableStoreQuery"
    return None


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
        if name == "sharedInput":
            equality = table["confirmation"]["attribution"]["rowStages"]["same"]
            units = [unit for unit in units
                     if not any(re.fullmatch(pattern, unit.strip(), re.I) for pattern in equality)]
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


def _complete_check(table, facts):
    # Each source may have several possible checks. A source question still matters
    # when the same evidence supports checks at two distinct destinations.
    candidates = {}
    for step in table["completeChecks"]:
        if facts.issuperset(step["all"]) and facts.isdisjoint(step["none"]):
            candidates.setdefault(step["source"], step["action"])
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _select(table: Dict[str, Any], facts: Set[str]) -> str:
    stream = table["requestedDemoStream"]
    goals = {name for fact, name in stream["goals"].items() if fact in facts or "asked" + name.title() in facts}
    if "uncertainArtifact" in facts and {"local", "hosted"} <= facts:
        return "ask_subject"
    if len(goals) == 1 and not facts.isdisjoint(stream["launch"]) and facts.isdisjoint(stream["incompatible"]):
        original = next(iter(goals))
        sink = original
        if stream["merges"][original] in facts:
            sink = "stderr" if original == "stdout" else "stdout"
        if stream["discarded"][sink] in facts:
            sink = "discarded"
        return stream["actions"][original][sink]
    for decision in table["decisions"]:
        alternatives = decision.get("any", [])
        if (facts.issuperset(decision.get("all", [])) and facts.isdisjoint(decision.get("none", []))
                and (not alternatives or not facts.isdisjoint(alternatives))):
            action = decision["action"]
            if action in table["skipClarificationForCheck"]:
                return _complete_check(table, facts) or action
            return action
    return _complete_check(table, facts) or table["defaultAction"]


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
            return ("chainMemory" if value[6:] == ":memory:" else "chainStore"), None
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
        owner = table["launchOwner"]
        if candidate and re.search(owner["other"], prefix, re.I):
            parts[index] = " original exporter unknown "
            continue
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
                prefix = parts[index][:command.start()]
                if re.fullmatch(table["commandContext"], prefix, re.I):
                    # Shell redirections apply even when SDK preload was absent.
                    streams = {}
                    for sink in re.finditer(table["streamSinkToken"], parts[index][command.end():]):
                        if sink.group(2) is not None:
                            streams[sink.group(1) or "1"] = sink.group(2)
                    for descriptor, name in (("1", "stdout"), ("2", "stderr")):
                        if streams.get(descriptor) == "/dev/null":
                            prefix += " discarded " + name + ". "
                parts[index] = prefix
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


def _execution_evidence(table, source, inherited=None, guide_origin=None):
    """Resolve launch and explicit application evidence identically for a question or its clarification."""
    inline = table["inlineNode"]
    clauses = re.split(table["split"], _scoped(table, source), flags=re.I)
    runtime = _runtime_names(table, clauses) & {"Python", "Node"}
    commands = []
    wrapper_choices = set()
    wrapper_receivers = set()
    wrapper_facts = set()
    ambient = (inherited or {}).get("REGISTER_FROM_NODE_OPTIONS")
    for clause in clauses:
        wrapper = re.search(inline["wrapperCommand"], clause)
        if wrapper and not re.fullmatch(table["commandContext"], clause[:wrapper.start()], re.I):
            wrapper = None
        if wrapper:
            prefix, flags, child_path = wrapper.groups()
            child = re.split(r"[/\\]", child_path.strip("\"'"))[-1].lower()
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
            direct_valid = (_http_destination(choice) if child.startswith(("python", "pypy"))
                            else _node_endpoint(choice, allow_insecure=False)) if direct_url else False
            if direct_valid:
                wrapper_receivers.add(choice)
            if direct_url and not _http_destination(choice, allow_insecure=True):
                wrapper_facts.add("wrapperRejected")
            builtin = (reserved or otlp or direct_url or choice.endswith(".jsonl")
                       or (choice.startswith("chain:") and choice[6:].strip()))
            if choice and not builtin and _plugin_name(choice) is None:
                wrapper_facts.add("wrapperRejected")
            if child.startswith(("python", "pypy")):
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
                    kind = "chainMemory" if choice[6:] == ":memory:" else "chainStore"
                elif re.match(r"^[A-Za-z][A-Za-z0-9_.-]+:", choice):
                    kind = "inlineUnknown"
                elif choice.endswith(".jsonl"):
                    kind = "pythonFile"
                wrapper_choices.add((kind or "inlineUnknown", choice if kind == "pythonFile" else None))
                continue
            runtime.add("Node")
            assigned = "BRIEFCASE_EXPORTER=" + destination + " " if destination else ""
            clause = prefix + "BRIEFCASE_AUTO=1 " + assigned + child + clause[wrapper.end():]
        previous_count = len(commands)
        for match in re.finditer(inline["command"], clause):
            if (_command_register(inline, match) or ambient) and re.fullmatch(
                table["commandContext"], clause[:match.start()], re.I
            ):
                commands.append(match)
        if wrapper and len(commands) == previous_count:
            wrapper_choices.add(("inlineUnknown", None))
    selection = table["applicationSelection"]
    choices: Set[Tuple[str, Optional[str]]] = set()
    application_clauses: List[str] = []
    for clause in clauses:
        continuation = re.match(selection["orderedContinuation"], clause, re.I)
        if continuation and application_clauses and re.search(selection["callStart"], application_clauses[-1], re.I):
            application_clauses[-1] += " and then " + clause[continuation.end():]
        elif clause.strip():
            application_clauses.append(clause.strip())
    python_choice = None
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
                for call in calls:
                    candidate = _python_choice(table, clause[call.start():])
                    if candidate is not None:
                        python_choice = candidate
                    elif call.group().endswith("observe("):
                        empty_environment = any(re.fullmatch(selection["noExporterEnvironment"], item, re.I)
                                                for item in clauses)
                        python_choice = ("pythonConsole", None) if empty_environment and not wrapper_choices else None
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
    if python_choice:
        choices.add(python_choice)
    if guide_origin is not None and not choices and len(wrapper_receivers) == 1 and not wrapper_facts:
        guide_origin.receivers.update(wrapper_receivers)
    if wrapper_choices or any(re.search(inline["wrapperCommand"], clause) for clause in clauses):
        if any(re.search(table["wrapperFailed"], clause, re.I) for clause in clauses):
            wrapper_facts.add("wrapperRejected")
    if choices:
        wrapper_facts.add("applicationExporterSelected")
        wrapper_facts.discard("pythonUninstrumented")
    return commands, choices or wrapper_choices, wrapper_facts


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
    values = set()
    for match in re.finditer(table["fileMentions"], clause, re.I):
        token = next(v for v in match.groups() if v is not None)
        path = quoted_values.get(token, token)
        directory = re.match(table["confirmation"]["attribution"]["operandDirectory"], clause[match.end():], re.I)
        if directory and not re.match(r"^(?:[/\\]|[A-Za-z]:)", path):
            base = quoted_values.get(directory.group(1), directory.group(1)).rstrip("/\\")
            path = base + "/" + path.removeprefix("./")
        values.add(path)
    return values


def _source_instances(table, clause, quoted):
    spec = table["confirmation"]["attribution"]
    instances = {"jsonl": _report_files(table, clause, quoted)}
    for source, pattern in spec["namedInstances"].items():
        instances[source] = {quoted.get(m.group(1), m.group(1)) for m in re.finditer(pattern, clause, re.I)
                             if not re.fullmatch(spec["nonInstance"], m.group(1), re.I)}
    return instances


def _report_clauses(table, turns):
    quoted_ids = {}
    quoted_values = {}
    prefix = "quoted:"
    while any(prefix in (turn or "") for turn in turns):
        prefix += "q:"

    def alias(value):
        if value not in quoted_ids:
            local_file = value.lower().endswith(".jsonl") and "://" not in value
            token = prefix + str(len(quoted_ids)) + (".jsonl" if local_file else "")
            quoted_ids[value] = token
            quoted_values[token] = value
        return quoted_ids[value]

    def url_alias(match):
        raw = match.group()
        value = raw.rstrip(".,;!?")
        return alias(value) + raw[len(value):]

    spec = table["confirmation"]["attribution"]
    reports = []
    for turn in turns:
        text = re.sub(table["quoteSpans"], lambda match: alias(match.group(match.lastindex)), turn or "")
        text = re.sub(spec["reportURL"], url_alias, text, flags=re.I)
        text = re.sub(spec["identifierField"], lambda match: match.group(1) + " ID", text, flags=re.I)
        clauses = re.split(spec["split"], text, flags=re.I)
        clauses = [part for clause in clauses for part in (
            re.split(spec["reportCoordination"], clause, flags=re.I)
            if re.search(spec["declaredIdentifier"], clause, re.I) else [clause]
        )]
        separated = []
        for clause in clauses:
            identity = re.search(spec["declaredIdentifier"], clause, re.I)
            tail = re.match(spec["declarationTail"], clause[identity.end():], re.I) if identity else None
            suffix = clause[identity.end():] if identity else ""
            independent = identity and re.fullmatch(
                spec["independentDeclarationPrefix"], clause[:identity.start()], re.I
            )
            remainder = suffix.strip() and not re.fullmatch(spec["roleTail"], suffix, re.I)
            if identity and remainder and (tail or independent):
                separated.extend([clause[:identity.end()], suffix[tail.end():] if tail else suffix])
            else:
                separated.append(clause)
        clauses = separated
        for index, clause in enumerate(clauses):
            for production in spec["reportProductions"]:
                parsed = re.fullmatch(production["pattern"], clause, re.I)
                if parsed:
                    clauses[index] = re.sub(r"\$(\d+)", lambda m: parsed[int(m[1])], production["canonical"])
                    break
        reports.append(clauses)
    return reports, quoted_values


@dataclass
class _GuideOrigin:
    receivers: Set[str] = field(default_factory=set)
    files: Set[str] = field(default_factory=set)
    identifier: Optional[str] = None


def _report_identities(table, reports, quoted_values):
    spec = table["confirmation"]["attribution"]
    original_ids = set()
    shared_ids = set()
    last_declared = set()
    shared_unspecified = False
    for clauses in reports:
        for clause in clauses:
            roles, scope = _report_roles(spec, clause, quoted_values)
            if roles["declaredIdentifier"]:
                last_declared = set(roles["declaredIdentifier"])
            if re.search(spec["sharedIdentifier"], clause, re.I):
                literal = re.search(spec["sharedValue"], clause, re.I)
                if literal:
                    shared_ids.add(quoted_values.get(literal.group(1), literal.group(1)))
                elif not re.search(r"\bticket\b", scope, re.I):
                    shared_ids.update(last_declared)
                    shared_unspecified |= not bool(last_declared)
            if re.search(spec["originalMarker"], scope, re.I) and not re.search(spec["uncertain"], scope, re.I):
                original_ids.update(roles["declaredIdentifier"])
                identity = _entities(table, clause).get("correlation", {})
                if identity.get("independent") and identity.get("value"):
                    original_ids.add(quoted_values.get(identity["value"], identity["value"]))
    return original_ids, shared_ids, shared_unspecified


def _permitted_continuation(spec, clause):
    """Require a complete supported statement; an arbitrary trailing qualifier is material."""
    patterns = _continuation_patterns(tuple(spec["continuationGrammar"].items()), tuple(spec["permittedContinuation"]))
    pieces = re.split(spec["resultClauseSplit"], clause.strip(), flags=re.I)
    return all(any(pattern.fullmatch(piece.strip()) for pattern in patterns) for piece in pieces)


@lru_cache(maxsize=24)
def _continuation_patterns(fragments, expressions):
    # The rules use the same named grammar fragments as the browser, in declaration order.
    symbols = {}
    for key, expression in fragments:
        symbols[key] = "(?:" + re.sub(r"\{\{(\w+)\}\}", lambda m: symbols[m[1]], expression) + ")"
    return [re.compile(re.sub(r"\{\{(\w+)\}\}", lambda m: symbols[m[1]], pattern), re.I)
            for pattern in expressions]


def _located_results(table, reports, quoted):
    """Associate a reported record with its location independently of the requested source."""
    spec = table["confirmation"]["attribution"]
    language = table["lookupLanguage"]["location"]
    # Both positive and negative observations belong to a source. An exclusive
    # match at one source can coexist with a separately established miss at another.
    at, unresolved = set(), set()
    exclusive = {"matched": set(), "no-match": set()}
    linked = {"matched": set(), "no-match": set()}
    completed = {"matched": set(), "no-match": set()}
    owner = set()
    for report in reports:
        for clause in report:
            roles, scope = _report_roles(spec, clause, quoted)
            named = {key for key, pattern in spec["sources"].items() if re.search(pattern, scope, re.I)}
            if any(re.fullmatch(table["lookupLanguage"]["phraseRoles"][condition], scope.strip(), re.I)
                   for condition in ("inactiveInspection", "unreadDownstream")):
                continue
            if named and re.search(language["phase"], scope, re.I):
                owner = named
            acquisition = re.search(language["phase"], scope, re.I) and _accounted_read(
                table, clause, roles["lookupIdentifier"]
            )
            disowned = re.search(spec["reportedForeign"], scope, re.I) and re.search(spec["foreign"], scope, re.I)
            locator = re.fullmatch(table["fileMentions"], clause.strip(), re.I)
            neutral = (acquisition or disowned or locator or _context_statement(table, clause)
                       or re.fullmatch(language["sourceQuestion"], scope.strip(), re.I)
                       or re.fullmatch(language["neutralSource"], scope.strip(), re.I)
                       or re.fullmatch(spec["reportIntent"], scope.strip(), re.I))
            negative = bool(re.search(language["negative"], scope, re.I))
            polarity = "no-match" if negative else "matched"
            restriction = re.sub(language["quantity"], " ", scope, flags=re.I)
            if named and not neutral:
                # An unclassified competing source cannot supply the selected source's observation.
                unresolved.update(named)
                if re.search(language["exclusive"], restriction, re.I):
                    exclusive[polarity].update(named)
            if re.search(language["exclude"], scope, re.I):
                continue
            if not negative and not (re.search(language["object"], scope, re.I)
                                     and re.search(language["result"], scope, re.I)):
                continue
            located = named or owner
            if len(located) != 1:
                continue
            at.update(located)
            if re.search(language["completedRead"], scope, re.I):
                completed[polarity].update(located)
            owner = located
            if negative or re.search(language["link"], scope, re.I):
                linked[polarity].update(located)
            if named and re.search(language["exclusive"], restriction, re.I):
                exclusive[polarity].update(named)
    return at, exclusive, linked, unresolved, completed


@dataclass(frozen=True)
class _LookupAccount:
    source: str
    result: str
    accounted: bool
    disputed: bool
    unknown_clause: bool


@lru_cache(maxsize=2)
def _role_lexicon(lexemes):
    return re.compile(r"(?<![\w-])(?:" + "|".join(f"(?P<{key}>{pattern})" for key, pattern in lexemes)
                      + r")(?![\w-])", re.I)


def _phrase_observation(table, turns):
    """Attribute only complete clauses whose noun and verb roles have a defined relation."""
    spec = table["lookupLanguage"]["phraseRoles"]
    lex = _role_lexicon(tuple(tuple(item) for item in spec["lexemes"]))
    facts = set()
    ids = set()
    files = set()
    outcomes = []
    unknown = False
    for turn in turns:
        for clause in re.split(spec["split"], turn or "", flags=re.I):
            if not clause.strip():
                continue
            tokens = []
            cursor = 0
            values = {}
            for match in lex.finditer(clause):
                if clause[cursor:match.start()].strip(" ,"):
                    tokens.append("?")
                key = match.lastgroup
                if key != "IGNORE":
                    tokens.append(key)
                    value = match.group()
                    if key == "RECEIPTIDVALUE":
                        declaration = re.fullmatch(spec["receiptValue"], value, re.I)
                        if not declaration or re.fullmatch(spec["unsupportedValue"], declaration[1].strip('"“”'), re.I):
                            tokens[-1] = "?"
                    if key in ("TARGET", "MARKER"):
                        value = re.sub(spec["valuePrefix"], "", value, flags=re.I).strip('"“”')
                        if re.fullmatch(spec["unsupportedValue"], value, re.I):
                            tokens[-1] = "?"
                    values.setdefault(key, []).append(value)
                cursor = match.end()
            if clause[cursor:].strip(" ,"):
                tokens.append("?")
            encoded = " ".join(tokens)
            roles = next((facts for shape, facts in spec["shapes"].items() if re.fullmatch(shape, encoded)), None)
            if roles is None:
                unknown = True
                attribution = re.sub(spec["candidatePopulation"], "", clause, flags=re.I)
                if re.search(spec["denial"], attribution, re.I) and re.search(spec["sourceNoun"], attribution, re.I):
                    facts.add("unresolvedProvenance")
                continue
            facts.update(roles)
            if ("consoleDemo" in roles and ("BIND" in values or "AUTHORRAN" in values)
                    and re.match(spec["possessiveSubject"], clause, re.I)):
                facts.add("personalConsoleDemo")
            if any("jsonl" in value.lower() for value in values.get("ORIGINALFILE", [])):
                facts.add("jsonl")
            if "source" in roles:
                facts.add("receiver" if "RECEIVER" in values else "storage")
            if "target" in roles:
                ids.update(values.get("TARGET", []))
                for declaration in values.get("RECEIPTIDVALUE", []):
                    label = re.fullmatch(spec["receiptValue"], declaration, re.I)
                    if label:
                        ids.add(label[1].strip('"“”'))
                for apposition in values.get("RECEIPTAPP", []):
                    target = re.match(spec["appositiveValue"], apposition)
                    if target:
                        ids.add(target[1].strip('"“”'))
            files.update(values.get("FILE", []))
            outcomes.extend(role for role in roles if role in ("match", "miss", "unlinked"))
    if "corroborates" in facts and outcomes == ["unlinked"]:
        outcomes = ["match"]
    return facts, ids, files, outcomes, unknown


def _lookup_account(table, turns):
    """Consume a firsthand lookup as source, search target, coverage and result roles."""
    grammar = table["lookupLanguage"]
    productions = grammar["productions"]
    patterns = _continuation_patterns(tuple(grammar["fragments"].items()), tuple(r["pattern"] for r in productions))
    facts = set()
    unknown = False
    seen_results = []
    identifiers = set()
    files = set()
    values, paths = _continuation_patterns(tuple(grammar["fragments"].items()), (grammar["targetValue"], "{{name}}"))
    for turn in turns:
        for sentence in re.split(grammar["sentences"], turn or ""):
            if not sentence.strip():
                continue
            for index, clause in enumerate(re.split(grammar["clause"], sentence.strip(), flags=re.I)):
                if index and re.match(grammar["implicit"], clause, re.I):
                    clause = "I " + clause
                if index:
                    clause = re.sub(grammar["investigator"], "I investigated ", clause, flags=re.I)
                role = next((r for r, pattern in zip(productions, patterns) if pattern.fullmatch(clause.strip())), None)
                if role is None:
                    unknown = True
                    # A comma adjunct may leave a source or target recognizable for routing,
                    # while the complete sentence remains ineligible to authorize a claim.
                    segments = clause.split(",")
                    role = next((r for segment in segments for r, pattern in zip(productions, patterns)
                                 if len(segments) > 1 and ("reader" in r["facts"] or "target" in r["facts"])
                                 and pattern.fullmatch(segment.strip())), None)
                    if role is None:
                        continue
                if "target" in role["facts"] and not re.search(grammar["originalTarget"], clause, re.I):
                    unknown = True
                    continue
                facts.update(role["facts"])
                if "target" in role["facts"]:
                    identifiers.update(next(value for value in match.groups() if value is not None)
                                       for match in values.finditer(clause))
                if "jsonl" in role["facts"]:
                    files.update(match.group() for match in paths.finditer(clause))
                seen_results.extend(set(role["facts"]).intersection(("match", "miss", "unlinked")))
    sources = facts.intersection(("jsonl", "receiver", "storage"))
    if len(sources) != 1 or not {"reader", "origin", "target"} <= facts:
        return None
    source = sources.pop()
    invalid_target = any(re.search(rule, value, re.I) for value in identifiers
                         for rule in table["entities"]["correlation"]["reject"])
    disputed = (invalid_target or len(identifiers) > 1 or len(files) > 1 or len(seen_results) > 1
                or (source == "storage" and "unreadStorage" in facts))
    result = seen_results[0] if len(seen_results) == 1 else "unknown"
    accounted = not unknown and not disputed and (result != "miss" or "complete" in facts)
    return _LookupAccount(source, result, accounted, disputed, unknown)


def _returned_result(spec, clause, roles):
    """Parse the returned object independently from the reader's lookup target."""
    grammar = spec["resultOperand"]
    if re.fullmatch(spec["rowStages"]["unlinkedIdentifier"], clause, re.I):
        return "unlinked_record"
    head = re.match(grammar["head"], clause, re.I)
    if not head:
        return None
    fragments = tuple(spec["continuationGrammar"].items())
    operands = re.split(grammar["compare"], clause[head.end():].strip(), maxsplit=1, flags=re.I)
    if len(operands) == 2 and not re.search(grammar["comparisonComplete"], operands[1], re.I):
        return "unknown"
    body = operands[0]
    if re.fullmatch(grammar["attributeHead"], head.group(), re.I) and any(
        token.fullmatch(body) for token in _continuation_patterns(fragments, tuple(grammar["identityAttribute"]))
    ):
        body = "record with " + body
    kinds = []
    for operand in re.split(grammar["objects"], body, flags=re.I):
        kind = next((name for name in ("miss", "match", "named", "neutral") if any(
            pattern.fullmatch(operand.strip()) for pattern in _continuation_patterns(fragments, tuple(grammar[name]))
        )), "unknown")
        if kind == "miss" and re.match(grammar["zero"], operand.strip(), re.I):
            kind = "zero"
        if kind == "neutral" and any(p.fullmatch(operand.strip()) for p in _continuation_patterns(
            fragments, ("{{singular}}{{recordKind}}",)
        )) and len(operands) == 1:
            kind = "unlinked_record"
        if kind == "named" and not roles["returnedIdentifier"]:
            kind = "neutral"
        kinds.append(kind)
    if "unknown" in kinds:
        return "unknown"
    if "zero" in kinds:
        if "match" in kinds:
            return "conflict"
        return "unknown" if set(kinds).intersection(("unlinked_record", "neutral")) else "zero"
    if kinds == ["unlinked_record"]:
        return "unlinked_record"
    if "unlinked_record" in kinds:
        return "ambiguous"
    if kinds.count("match") > 1:
        return "ambiguous"
    if "match" in kinds:
        return "mixed_match" if "miss" in kinds else "match"
    if "named" in kinds:
        return "named"
    return "neutral" if "neutral" in kinds else "miss"


def _accounted_read(table, clause, identifiers):
    """Check every operand of the acquisition before allowing its structured outcome."""
    spec = table["confirmation"]["attribution"]
    grammar = spec["readOperand"]
    clause = re.sub(grammar["ageResult"], "", clause, flags=re.I)
    clause = re.sub(table["fileMentions"], " sourcevalue ", clause, flags=re.I)
    clause = re.sub(grammar["identifier"], r"\1identityvalue", clause, flags=re.I)
    for pattern in spec["namedInstances"].values():
        for match in reversed(list(re.finditer(pattern, clause, re.I))):
            if not re.fullmatch(spec["nonInstance"], match.group(1), re.I):
                start, end = match.span(1)
                clause = clause[:start] + "sourcevalue" + clause[end:]
    for value in identifiers:
        if value:
            target = r"(\b(?:for|by|using|with|against)\s+)" + re.escape(value) + r"(?!\w)"
            clause = re.sub(target, r"\1identityvalue", clause, flags=re.I)
    clause = re.sub(r"\bID\s*=\s*(?=\s|$|and\b)", "ID ", clause, flags=re.I)
    clause = re.sub(r"['’]s\b", "", clause, flags=re.I)
    clause = re.sub(grammar["externalProcess"], " process ", clause, flags=re.I)
    parts = re.split(spec["coordinateDetail"], clause, flags=re.I)
    for index, part in enumerate(parts):
        if index and _permitted_continuation(spec, part):
            continue
        words = re.findall(r"[\w-]+|[^\w\s,.:;]", part)
        if not all(re.fullmatch(grammar["word"], word, re.I) for word in words):
            return False
    return True


def _context_statement(table, clause):
    spec = table["confirmation"]["attribution"]
    declaration = re.search(spec["contextDeclaration"], clause, re.I)
    if not declaration:
        return False
    remaining = clause[declaration.end():].strip()
    if not remaining:
        return True
    wrapper = re.search(table["inlineNode"]["wrapperCommand"], clause)
    command = wrapper or re.search(table["inlineNode"]["command"], clause)
    if command:
        return bool(re.fullmatch(table["commandContext"], clause[:command.start()], re.I))
    remaining = re.sub(table["fileMentions"], " file ", remaining, flags=re.I)
    for pattern in spec["namedInstances"].values():
        for match in reversed(list(re.finditer(pattern, remaining, re.I))):
            if not re.fullmatch(spec["nonInstance"], match.group(1), re.I):
                start, end = match.span(1)
                remaining = remaining[:start] + remaining[end:]
    if re.fullmatch(spec["contextValue"], remaining.strip(), re.I):
        return True
    # An identifier value is data only when the declaration itself labels it as such.
    return (bool(re.search(r"\b(?:marker|ID|identifier)\s+(?:is|was)$", declaration.group(), re.I))
            and bool(re.fullmatch(r"[\w.:-]+" + spec["roleTail"], remaining, re.I)))


def _stage_row_detail(spec, clause, named, previous, current, attempts, original_ids, quoted_values):
    """Refine the previous returned object or describe an additional object of that lookup."""
    stages = spec["rowStages"]
    owners = [attempts[current[kind]] for kind in previous]
    provider = re.fullmatch(stages["providerResult"], clause, re.I)
    exclusive = re.fullmatch(stages["exclusiveResultOwner"], clause, re.I)
    ordinary_provider = (provider and not re.search(stages["definiteObject"], clause, re.I)
                         and not re.search(stages["objectReference"], clause, re.I))
    if named and previous - named and (exclusive or ordinary_provider):
        for kind in previous - named:
            check = attempts[current[kind]]
            if check.latest_object or check.zero_matches:
                check.relocated = True
    if re.fullmatch(stages["delegatedRead"], clause, re.I):
        for check in owners:
            check.foreign_reader_only = True
        return True
    group = re.fullmatch(stages["groupSize"], clause, re.I)
    if group:
        quantity = group[1].lower()
        size = int(quantity) if quantity.isdigit() else stages["cardinals"][quantity]
        for check in owners:
            if check.group_size is not None or check.latest_object or size < 1:
                check.unresolved_detail = True
            check.group_size = size
        return True
    member = re.fullmatch(stages["memberIdentity"], clause, re.I)
    if member and owners and all(check.group_size is not None for check in owners):
        for check in owners:
            word = member[1].lower()
            index = 2 if word == "other" else max(1, stages["ordinals"].index(word))
            check.classify_member(index, member[2].lower(), last_of_two=word == "other")
        return True
    single = re.fullmatch(stages["singleNamed"], clause, re.I)
    if single and original_ids and len(original_ids) == 1:
        value = quoted_values.get(single[1], single[1])
        for check in owners:
            matched = value in original_ids
            check.add_object("match" if matched else "miss")
            check.outcome = "matched" if matched else "no-match"
        return True
    if re.search(stages["personalGap"], clause, re.I):
        for check in owners:
            check.personal_gap = True
    multiple = re.match(stages["multiplicityHead"], clause, re.I)
    if multiple:
        count = re.search(r"\d+", multiple.group())
        if not count or count.group().lstrip("0") not in ("", "1"):
            identity = clause[multiple.end():]
            original = re.fullmatch(stages["multipleOriginal"], identity, re.I)
            explicit = re.fullmatch(stages["multipleNamed"], identity, re.I)
            value = quoted_values.get(explicit[1], explicit[1]) if explicit else None
            prior = re.fullmatch(stages["priorID"], identity, re.I)
            for check in owners:
                if original or (value and value in original_ids) or (prior and check.latest_object == "match"):
                    check.matched_objects = max(check.matched_objects, 2)
                    check.ambiguous = True
                elif (value and original_ids) or (prior and check.latest_object == "miss"):
                    check.other_objects += 2
                else:
                    check.unresolved_detail = True
            return True
    reference = re.match(stages["resultReference"], clause, re.I)
    inverse = re.search(stages["objectReference"], clause, re.I)
    if not inverse and named:
        definite = re.search(stages["definiteObject"], clause, re.I)
        if definite and any(re.search(spec["sources"][kind], clause[:definite.start()], re.I) for kind in named):
            # A source as grammatical subject can identify the prior result without a demonstrative.
            if re.match(stages["sourceSubject"], clause[:definite.start()], re.I):
                inverse = definite
    if (reference or inverse) and named:
        # A back-reference describes the immediately preceding observation. A
        # second source's mention is not a second independent lookup.
        cited = named
        if inverse and inverse.start():
            subject = clause[:inverse.start()]
            cited = {kind for kind in named if re.search(spec["sources"][kind], subject, re.I)} or named
        for kind in previous - cited:
            check = attempts[current[kind]]
            if check.latest_object or check.zero_matches:
                attributed = ((reference and re.match(stages["originPredicate"], clause[reference.end():], re.I))
                              or re.search(stages["sourceOfResult"], clause, re.I))
                if attributed:
                    check.relocated = True
                else:
                    check.uncertain_origin = True
        if previous - cited:
            return False
    equality = next((kind for kind in ("different", "same") if any(
        re.fullmatch(pattern, clause, re.I) for pattern in stages[kind]
    )), None)
    additional = re.fullmatch(stages["additional"], clause, re.I)
    if re.fullmatch(stages["secondaryExcludesOriginal"], clause, re.I):
        for check in owners:
            check.add_object("miss")
        return True
    if not (equality or additional):
        if re.match(stages["secondarySubject"], clause, re.I):
            for check in owners:
                check.unresolved_detail = True
        return False
    applied = False
    for check in owners:
        comparison = ("match" if equality == "same" else "miss") if equality else additional[1].lower()
        if not check.latest_object:
            if additional and comparison in ("original", "different"):
                check.add_object("match" if comparison == "original" else "miss")
                applied = True
            if equality:
                check.pending_relation = comparison
                check.outcome = "matched" if comparison == "match" else "no-match"
                applied = True
            continue
        if additional:
            comparison = {"original": "match", "different": "miss", "same": check.latest_object}[comparison]
            check.add_object(comparison)
        else:
            if check.latest_object in ("match", "miss") and comparison != check.latest_object:
                check.conflicting_results = True
            check.matched_objects -= check.latest_object == "match"
            check.other_objects -= check.latest_object == "miss"
            check.add_object(comparison)
        if comparison in ("match", "miss"):
            check.outcome = "matched" if check.matched_objects else "no-match"
            check.unlinked_record = False
            check.returned_unbound = False
            applied = True
    return applied


def _confirmation_conflict(table, source, result, turns, action, facts, guide_origin, eligible):
    """Bind an observation to its source and target before interpreting a structured outcome."""
    spec = table["confirmation"]["attribution"]
    active = set()
    mentioned = set()
    original_marker = False
    disowned_report = set()
    launch_files = guide_origin.files
    destinations = {name: set() for name in spec["sources"]}
    destinations["jsonl"].update(launch_files)
    destinations["receiver"].update(guide_origin.receivers)
    reports, quoted_values = _report_clauses(table, turns)
    original_ids, shared_ids, shared_unspecified = _report_identities(table, reports, quoted_values)
    if guide_origin.identifier:
        original_ids = {guide_origin.identifier}
    for clauses in reports:
        for clause in clauses:
            _, scope = _report_roles(spec, clause, quoted_values)
            if (not launch_files and re.search(spec["fileContext"], scope, re.I)
                    and not re.search(spec["lookupPerformed"], scope, re.I)):
                destinations["jsonl"].update(_report_files(table, scope, quoted_values))
            if re.search(spec["instanceContext"], scope, re.I) and not re.search(spec["lookupPerformed"], scope, re.I):
                for name, values in _source_instances(table, scope, quoted_values).items():
                    if name != "jsonl":
                        destinations[name].update(values)
    # A continuation belongs to the preceding lookup of that source. A different source
    # instance or identifier starts a separate attempt and cannot replace the original one.
    unique_ids = original_ids - shared_ids
    origin = {name: (name, frozenset(instances), frozenset(unique_ids)) for name, instances in destinations.items()}
    attempts = {key: _SourceCheck() for key in origin.values()}
    current = dict(origin)
    latest = dict(origin)
    foreign_context = set()
    unbound_details = []
    unresolved_stages = set()
    inspected_sources = set()
    background_sources = set()
    narrative_gap = False
    if "readDifferentPath" in facts:
        attempts[origin["jsonl"]].correct_instance = False
    for turn_index, clauses in enumerate(reports):
        if turn_index and not active and action in ("query_storage", "stored") and source == "storage":
            active = {source}
        for clause in clauses:
            if re.fullmatch(table["lookupLanguage"]["phraseRoles"]["unreadDownstream"], clause.strip(), re.I):
                attempts[origin["storage"]].unchecked = True
                continue
            if re.fullmatch(table["lookupLanguage"]["phraseRoles"]["neutralReadQuestion"], clause.strip(), re.I):
                continue
            if re.fullmatch(spec["reportIntent"], clause, re.I):
                continue
            if re.fullmatch(spec["narrativeGap"], clause, re.I):
                narrative_gap = True
                continue
            roles, source_scope = _report_roles(spec, clause, quoted_values)
            source_scope = re.sub(spec["rowStages"]["completedAspect"], r"\1", source_scope.strip(), flags=re.I)
            # The writer's output and a retained ID describe the evidence used for
            # a lookup. Neither assertion reports that the reader inspected a row.
            background = spec["backgroundRoles"]
            file_scope = re.sub(table["fileMentions"], " sourcefile ", clause, flags=re.I)
            if re.fullmatch(spec["rowStages"]["writerInput"], clause.strip(), re.I):
                continue
            if re.fullmatch(background["writer"], file_scope, re.I):
                background_sources.add("jsonl")
                continue
            retained = re.fullmatch(background["identifier"], clause, re.I)
            if retained and quoted_values.get(retained.group(1), retained.group(1)) in unique_ids:
                continue
            if re.fullmatch(background["rowQuestion"], clause, re.I):
                continue
            if background_sources and not inspected_sources and not active and source_scope.strip():
                active = set(background_sources)
            verbs = re.sub(table["fileMentions"], lambda match: " " * len(match.group()), source_scope, flags=re.I)
            checked = re.search(spec["lookupPerformed"], verbs, re.I)
            if active and re.match(spec["resultContinuation"], source_scope, re.I):
                checked = None
            denied_check = (re.search(spec["unchecked"], source_scope, re.I)
                            or re.search(spec["unseenResult"], source_scope, re.I))
            reported = checked or re.search(spec["resultActor"], source_scope, re.I)
            other_reader = _other_reader(spec, source_scope, reported)
            original = re.search(spec["original"], source_scope, re.I)
            qualified = not re.search(spec["uncertain"], source_scope, re.I)
            if re.search(spec["originalMarker"], source_scope, re.I) and qualified:
                original_marker = True
            named = {key for key, pattern in spec["sources"].items() if re.search(pattern, source_scope, re.I)}
            if (checked or denied_check) and re.search(spec["fileReference"], source_scope, re.I):
                named.add("jsonl")
            if _stage_row_detail(spec, source_scope.strip(), named, active or {source}, current, attempts,
                                 unique_ids, quoted_values):
                continue
            details = {role for role, pattern in spec["detailRoles"].items() if re.search(pattern, source_scope, re.I)}
            if (details or re.search(spec["standaloneResult"], source_scope, re.I)) and not (named or active):
                # A standalone result qualifier still constrains the submitted report.
                active = {source}
            meaning = {name for name, rule in spec["observedSemantics"].items() if _matches(rule, source_scope)}
            if (re.match(spec["rowStages"]["unknownResult"], source_scope, re.I)
                    and not _context_statement(table, clause)):
                returned = _returned_result(spec, source_scope.strip(), roles)
                allowed_read = checked and _accounted_read(table, source_scope, roles["lookupIdentifier"])
                consumed = returned or allowed_read or _permitted_continuation(spec, source_scope)
                if returned == "unknown" or not consumed:
                    unresolved_stages.update(named or active or {source})
            if "exclusiveEvidence" in meaning and active:
                for target in active:
                    if "externalEvidence" in meaning or (named and target not in named):
                        attempts[current[target]].foreign_reader_only = True
            exclusive_subject = re.match(spec["exclusiveSubject"], source_scope, re.I)
            if exclusive_subject and named and not other_reader:
                for other in set(origin) - named:
                    attempts[origin[other]].unchecked = True
            if re.fullmatch(spec["exclusiveReference"], source_scope, re.I):
                for other in set(origin) - (mentioned or active):
                    attempts[origin[other]].unchecked = True
            if other_reader:
                foreign_context = named or active or set(origin)
                for target in foreign_context:
                    prior = attempts[origin[target]]
                    if re.search(spec["rowStages"]["remainingPortion"], source_scope, re.I):
                        prior.personal_gap = True
                    prior.clarification_veto = True
                    excluded_reader = re.search(spec["readerExcluded"], source_scope, re.I)
                    if exclusive_subject or excluded_reader or not prior.original_target or (
                        not checked and prior.outcome is None
                    ):
                        prior.foreign_reader_only = True
                continue
            # Once a source is under inspection, a follow-on statement about it
            # must be accounted for, even when it contains no familiar outcome term.
            bound_before = bool(active)
            if checked or re.search(spec["detailSelf"], source_scope, re.I):
                foreign_context = set()
            elif foreign_context and (not named or named <= foreign_context):
                continue
            clause = source_scope
            inspected = _source_instances(table, source_scope, quoted_values)
            sought = set(roles["lookupIdentifier"])
            if checked and not sought:
                alternatives = re.search(spec["lookupAlternatives"], clause[checked.end():], re.I)
                operand = alternatives or re.search(spec["bareQueryTarget"], clause[checked.end():], re.I)
                if operand and (alternatives or original_ids):
                    value = operand.group(1)
                    if not re.fullmatch(spec["genericQueryTarget"], value, re.I):
                        sought.add(quoted_values.get(value, value))
            subject = re.search(spec["outcomeTarget"], clause, re.I)
            if subject:
                sought = {quoted_values.get(subject.group(1), subject.group(1))}
            if named:
                mentioned = named
            unbound_source = False
            if checked and not (named or active):
                candidates = {kind for kind, instances in destinations.items() if instances}
                if not candidates and re.search(spec["sourceReference"], clause, re.I):
                    candidates = mentioned
                if len(candidates) == 1:
                    active = candidates
                else:
                    unbound_source = True
            event = dict(current)
            for name in named or active:
                if checked or subject or re.search(spec["attempted"], clause, re.I):
                    instance = frozenset(inspected[name] or (current[name][1] if subject else destinations[name]))
                    identifiers = frozenset(sought or unique_ids or original_ids)
                    event[name] = (name, instance, identifiers)
                    attempts.setdefault(event[name], _SourceCheck())
                    if checked or not subject:
                        current[name] = event[name]
                latest[name] = event[name]
            checks = {name: attempts[key] for name, key in event.items()}
            if sought and re.search(spec["receiptBindsTarget"], source_scope, re.I) and (
                not original_ids or sought == original_ids
            ):
                original = True
            inquiry = (re.search(spec["questionLead"], source_scope, re.I)
                       and all(re.fullmatch(spec["neutralQuestionWords"], word, re.I)
                               for word in re.findall(r"[\w-]+", source_scope)))
            if not inspected_sources and not checked and not active and source_scope.strip():
                if not (inquiry or roles["declaredIdentifier"] or _context_statement(table, clause)
                        or _permitted_continuation(spec, clause)):
                    unbound_details.append((named, clause))
            if checked and named and not bound_before:
                known_ids = original_ids | set(roles["lookupIdentifier"])
                if not _accounted_read(table, source_scope, known_ids):
                    unbound_details.append((named, source_scope))
                linked = re.split(spec["coordinateDetail"], source_scope, flags=re.I)[1:]
                target = re.search(spec["lookupSuffixAnchor"], source_scope[checked.end():], re.I)
                trailing = source_scope[checked.end() + target.end():].strip(" ,") if target else ""
                tail_parts = re.split(spec["coordinateDetail"], trailing, maxsplit=1, flags=re.I)
                if len(tail_parts) == 2 and re.fullmatch(spec["neutralLookupTail"], tail_parts[0], re.I):
                    trailing = tail_parts[1]
                if trailing and not re.fullmatch(spec["neutralLookupTail"], trailing, re.I):
                    detail = re.sub(r"^(?:and|then)\s+", "", trailing, flags=re.I)
                    if not _permitted_continuation(spec, detail):
                        linked.append(detail)
                if any(not kinds or kinds & named for kinds, _ in unbound_details) or (linked and not all(
                    _permitted_continuation(spec, phrase)
                    or (named == {"jsonl"} and re.fullmatch(spec["fileListOperand"], phrase.strip(), re.I))
                    for phrase in linked
                )):
                    for name in named:
                        checks[name].unaccounted = True
                        checks[name].clarification_veto = True
                unbound_details.clear()
            if (bound_before and source_scope.strip() and not inquiry
                    and not roles["declaredIdentifier"]):
                for name in named or active:
                    permitted = _permitted_continuation(spec, source_scope)
                    if checked and name in inspected_sources and named and qualified and permitted:
                        checks[name].unaccounted = False
                    elif not permitted:
                        checks[name].unaccounted = True
                        checks[name].clarification_veto = True
                    if re.search(spec["outsideInstance"], source_scope, re.I):
                        checks[name].correct_instance = False
            # A recognized noun phrase alone cannot certify a record. Index
            # the actual object, identity and multiplicity on the owning lookup.
            owners = ((named or active) & inspected_sources) | (named if checked else set())
            if not owners and re.match(spec["standaloneResult"], source_scope, re.I):
                owners = named or active or {source}
            result_grammar = spec["resultOperand"]
            reported_sources = named or active
            if (reported_sources and source not in reported_sources
                    and (roles["returnedIdentifier"] or re.match(result_grammar["head"], source_scope, re.I))):
                attempts[origin[source]].clarification_veto = True
            mixed_match = False
            for result_clause in re.split(result_grammar["combinedRead"], source_scope, flags=re.I) if owners else ():
                if re.match(result_grammar["implicitReader"], result_clause, re.I):
                    result_clause = "it " + result_clause.lstrip()
                returned = _returned_result(spec, result_clause, roles)
                if returned == "mixed_match":
                    mixed_match = True
                    returned = "match"
                if not returned:
                    continue
                parsed = returned
                for name in owners:
                    check = checks[name]
                    returned = parsed
                    if returned in ("match", "miss", "unlinked_record"):
                        check.add_object(returned)
                        returned = check.latest_object
                    if returned == "named":
                        returned_ids = roles["returnedIdentifier"]
                        if returned_ids and len(unique_ids) == 1:
                            check.latest_object = "match" if returned_ids[-1] in unique_ids else "miss"
                    if (returned in ("match", "miss", "named", "zero", "unlinked_record") and check.original_target
                            and re.match(spec["rowStages"]["completedReturn"], result_clause.strip(), re.I)):
                        check.pending = False
                    if returned in ("match", "named", "miss", "zero", "unlinked_record"):
                        for other in set(origin) - owners:
                            attempts[origin[other]].clarification_veto = True
                    if returned == "conflict":
                        check.conflicting_results = True
                    if returned == "zero":
                        check.latest_object = "zero"
                        check.zero_matches = True
                        check.conflicting_results |= (check.anonymous_match or check.matching_rows > 0
                                                      or bool(original_ids.intersection(roles["returnedIdentifier"])))
                    if returned == "match":
                        check.conflicting_results |= check.zero_matches
                        check.anonymous_match = True
                    if (returned == "named" and check.outcome == "matched"
                            and re.search(spec["returnAdditiveNamed"], result_clause, re.I)
                            and original_ids.intersection(roles["returnedIdentifier"])):
                        check.ambiguous = True
                    if returned == "ambiguous":
                        check.ambiguous = True
                    elif returned == "unknown":
                        check.unaccounted = True
                        check.clarification_veto = True
                    elif returned in ("neutral", "unlinked_record"):
                        check.unlinked_record = returned == "unlinked_record"
                        check.returned_unbound = True
                    elif returned in ("match", "miss", "zero"):
                        if returned == "match" and check.outcome == "matched" and re.match(
                            spec["additionalResult"], result_clause, re.I
                        ):
                            check.ambiguous = True
                        check.outcome = "matched" if returned == "match" else "no-match"
                        check.returned_unbound = False
                    if returned in ("miss", "zero"):
                        meaning.add("differentTarget")
            if re.search(spec["resultOperand"]["comparisonComplete"], source_scope, re.I):
                for name in named or active:
                    checks[name].returned_unbound = False
            if checked:
                inspected_sources.update(named)
            if unbound_source:
                for kind in origin:
                    attempts[origin[kind]].unresolved_lookup = True
            for name in named or active:
                if checked and sought:
                    checks[name].unresolved_lookup = not original and not bool(original_ids)
                elif checked and original:
                    checks[name].unresolved_lookup = False
                wanted = destinations[name]
                different_instance = inspected[name] and wanted and inspected[name].isdisjoint(wanted)
                if checked and different_instance:
                    checks[name].correct_instance = False
                if shared_ids.intersection(event[name][2]) or (shared_unspecified and not unique_ids):
                    checks[name].shared_identifier = True
            failed = (re.search(spec["attempted"], clause, re.I) or re.search(spec["readFailed"], clause, re.I)
                      or re.search(spec["readException"], clause) or re.search(spec["plannedRead"], clause, re.I))
            excluded_source = checked and re.search(spec["excludedOriginalSource"], clause[checked.end():], re.I)
            uncertain_source = not excluded_source and re.search(spec["unclearSource"], clause, re.I)
            uncertain_read = checked and (not qualified or uncertain_source
                                          or re.search(spec["unclearTarget"], clause, re.I))
            if uncertain_read and not named and not active:
                for possible in origin:
                    attempts[origin[possible]].unchecked = True
            if failed or uncertain_read or denied_check:
                blocked = named or active
                if not re.fullmatch(table["lookupLanguage"]["phraseRoles"]["inactiveInspection"],
                                    source_scope.strip(), re.I):
                    active = set(blocked)
                for target in blocked:
                    wanted = destinations[target]
                    if inspected[target] and wanted and inspected[target].isdisjoint(wanted):
                        continue
                    checks[target].unchecked = True
                    checks[target].original_target = False
                    checks[target].outcome = None
                    checks[target].matching_rows = 0
                continue
            if checked and qualified:
                for target in named:
                    if re.search(spec["sourceByReference"], clause, re.I) and re.search(
                        spec["lookupTargetRole"], clause, re.I
                    ):
                        checks[target].referenced_check = True
                    if re.search(spec["partialCoverage"], clause, re.I):
                        checks[target].partial = True
                    elif re.search(spec["exhaustiveCoverage"], clause, re.I):
                        checks[target].partial = False
                    if checks[target].group_size is None:
                        checks[target].unresolved_detail = False
                    corrected = re.search(spec["correctedInspection"], clause, re.I)
                    if inspected[target] and destinations[target] and corrected:
                        attempts[origin[target]].correct_instance = inspected[target] == destinations[target]
                    if excluded_source:
                        checks[target].correct_instance = False
                    elif inspected[target] and destinations[target]:
                        checks[target].correct_instance = (inspected[target] == destinations[target]
                                                           and len(inspected[target]) == 1)
                    elif re.search(spec["originalSource"], clause, re.I):
                        checks[target].correct_instance = True
                    checks[target].matching_rows = 0
            # A search and its result can occupy separate sentences. Apply the
            # qualification to that lookup, without starting another source check.
            for target in named or active:
                check = checks[target]
                if "failedSearch" in meaning or re.search(spec["detailPending"], clause, re.I):
                    check.pending = True
                elif (check.original_target and qualified and (re.search(spec["sameReaderResult"], clause, re.I)
                      or re.fullmatch(spec["completedStatus"], clause, re.I))):
                    check.pending = False
                    check.foreign_reader_only = False
                if (re.search(spec["returnedIdentitySubject"], clause, re.I)
                        and re.search(spec["foreign"], clause, re.I)):
                    check.outcome = "no-match"
                if "limitedSearch" in meaning or re.search(spec["partialCoverage"], clause, re.I):
                    check.partial = True
                if re.search(spec["returnedObject"], clause, re.I) and re.search(spec["unclearTarget"], clause, re.I):
                    check.unresolved_lookup = True
                different = "differentTarget" in meaning or re.search(spec["identifierComparison"], clause, re.I)
                if not mixed_match and different:
                    check.outcome = "no-match"
                elif qualified and re.search(spec["equalIdentifiers"], clause, re.I):
                    check.outcome = "matched"
            # Structured fields may supply an unreported result. If the reader
            # describes one, interpret that detail on the same observation. An
            # unrecognized qualifier cannot authorize a stronger claim.
            for target in (named or active) if details else ():
                check = checks[target]
                if "comparison" in details:
                    if not mixed_match and re.search(spec["detailMismatch"], clause, re.I):
                        check.outcome = "no-match"
                    elif not (re.search(spec["equalIdentifiers"], clause, re.I)
                              or re.search(spec["exclusiveOther"], clause, re.I)):
                        check.unresolved_detail = True
                if "assessment" in details and not details.intersection(("comparison", "progress", "identity")):
                    understood = (roles["returnedIdentifier"] or any(re.search(spec[name], clause, re.I) for name in
                                  ("match", "miss", "originalMatch", "equalIdentifiers", "identifierComparison",
                                   "detailAffirmativeIdentity", "detailNeutral", "detailComplete")))
                    if not understood:
                        check.unresolved_detail = True
                if "identity" in details:
                    identified = (roles["returnedIdentifier"]
                                  or re.search(spec["detailAffirmativeIdentity"], clause, re.I))
                    if re.search(spec["detailNoID"], clause, re.I) or not identified:
                        check.unresolved_detail = True
                if "source" in details and (re.search(spec["excludedOriginalSource"], clause, re.I)
                                            or not re.search(spec["detailOriginalSource"], clause, re.I)):
                    check.correct_instance = False
                if "progress" in details:
                    if re.search(spec["detailLimited"], clause, re.I):
                        check.partial = True
                    elif not check.pending and not re.search(spec["detailComplete"], clause, re.I):
                        check.unchecked = True
            if checked and qualified and named:
                positions = [m.start() for key in named for m in [re.search(spec["sources"][key], source_scope, re.I)]
                             if m is not None]
                if not positions:
                    positions.append(checked.end())
                first = min(positions)
                if "jsonl" in named:
                    filename = re.search(table["fileMentions"], source_scope, re.I)
                    if filename:
                        first = min(first, filename.start())
                gap = clause[checked.end():first]
                before = re.search(spec["exclusivePrefix"], clause[:checked.start()], re.I)
                if ((before and re.fullmatch(spec["exclusiveGap"], gap, re.I))
                        or re.fullmatch(spec["exclusiveSuffix"], gap, re.I)):
                    for target in checks.keys() - named:
                        attempts[origin[target]].unchecked = True
            if named and (checked or re.search(spec["observation"], verbs, re.I)):
                active = named
            targets = named or active or set(spec["sources"])
            if not named and re.search(spec["runlessReport"], clause, re.I):
                targets = set(spec["sources"])
            if checked and qualified and (named or re.search(spec["reportRef"], clause, re.I)):
                for target in targets:
                    checks[target].unchecked = False
                    checks[target].foreign_reader_only = False
            attribution = re.sub(spec["readerContext"], "", source_scope, flags=re.I)
            attribution = re.sub(spec["notForeign"], "", attribution, flags=re.I)
            foreign = re.search(spec["foreign"], attribution, re.I)
            explicit = (re.search(spec["reportedForeign"], clause, re.I)
                        or re.search(spec["foreignLookup"], clause, re.I))
            query_ids = set(sought)
            if subject:
                query_ids.add(quoted_values.get(subject.group(1), subject.group(1)))
            row_ids = set(roles["returnedIdentifier"])
            comparable = original_ids and query_ids
            if comparable and (unique_ids or original_ids) != query_ids and not shared_ids.intersection(query_ids):
                foreign, explicit = True, True
            original_target = original or original_marker or (comparable and (unique_ids or original_ids) == query_ids)
            if checked and qualified and original_target and not (foreign and explicit):
                for target in targets:
                    checks[target].original_target = True
            ambiguous_rows = re.search(spec["ambiguousRows"], clause, re.I)
            miss = not mixed_match and ("differentTarget" in meaning or re.search(spec["miss"], clause, re.I))
            if not (checked or ambiguous_rows or miss or re.search(spec["observation"], clause, re.I)
                    or re.search(spec["exclusiveOther"], clause, re.I)):
                continue
            for target in targets:
                check = checks[target]
                # Foreign rows can be returned by a search for the original identifier.
                contents = check.original_target and (re.search(spec["returnedContents"], clause, re.I)
                                                      or re.search(spec["reportedContents"], clause, re.I))
                if foreign and (explicit or not contents):
                    if re.search(spec["reportedForeign"], clause, re.I):
                        disowned_report.add(target)
                    check.foreign_target = True
                    check.original_target = False
                    check.outcome = None
                    continue
                same_id = query_ids and (unique_ids or original_ids) == query_ids
                identified = original or same_id
                correction = (qualified and identified and (named or re.search(spec["reportRef"], clause, re.I))
                              and (checked or re.search(spec["identifiesOriginal"], clause, re.I)))
                if correction:
                    disowned_report.discard(target)
                    check.foreign_target = False
                    check.original_target = True
                if correction and checked:
                    check.ambiguous = False
                if ambiguous_rows or (original_ids and len(unique_ids or original_ids) != 1 and (query_ids or row_ids)):
                    check.ambiguous = True
                if not check.foreign_target and (check.original_target or (row_ids and original_ids)):
                    if miss:
                        check.outcome = "no-match"
                    elif qualified and len(unique_ids or original_ids) == 1 and row_ids:
                        count = sum(value in (unique_ids or original_ids) for value in roles["returnedIdentifier"])
                        check.matching_rows += count
                        if check.matching_rows > 1:
                            check.ambiguous = True
                        check.conflicting_results |= check.zero_matches and check.matching_rows > 0
                        check.outcome = "matched" if check.matching_rows or check.anonymous_match else "no-match"
                    elif qualified and re.search(spec["originalMatch"], clause, re.I):
                        check.outcome = "matched"
                    elif re.search(spec["exclusiveOther"], clause, re.I):
                        check.outcome = "no-match"
                    elif qualified and not foreign and re.search(spec["match"], clause, re.I):
                        check.outcome = "matched"
    if source in disowned_report:
        return "confirmation_other_run"
    original_check = attempts[origin[source]]
    last_check = attempts[latest[source]]
    check = original_check if original_check.outcome or original_check.ambiguous else last_check
    if source == "storage" and "foreignStoreComparison" in facts:
        return "confirmation_other_run"
    if source == "storage" and check.outcome is None:
        if "linkedStoreReadback" in facts:
            check.outcome = "matched"
        elif "storageIDMismatch" in facts:
            check.outcome = "no-match"
    conflict = check.compare(source, result, action, facts)
    if source in unresolved_stages and conflict in (None, "ask_evidence") and result != "unverified":
        return "confirmation_unresolved_lookup"
    accounted_result = check.original_target and check.outcome == result
    needs_binding = narrative_gap or (check.referenced_check and check.outcome is None)
    unexplained = any(not kinds or source in kinds for kinds, _ in unbound_details)
    if (check.clarification_veto or unexplained) and (conflict == "ask_evidence" or (
        needs_binding and not accounted_result
    )):
        return "confirmation_unresolved_lookup" if conflict in (None, "ask_evidence") else conflict
    if needs_binding and not conflict and result != "unverified" and not accounted_result:
        conflict = "ask_evidence"
    if eligible and result in ("matched", "no-match") and conflict in (None, "ask_evidence"):
        located, exclusive, linked, unresolved, completed = _located_results(table, reports, quoted_values)
        own_result = source in linked[result] and (source in completed[result]
                                                   or (check.original_target and check.outcome == result))
        if exclusive[result] - {source} or (located - {source} and not own_result):
            return "confirmation_other_source"
        if unresolved - {source} and not own_result:
            return "confirmation_unresolved_lookup"
    return conflict


def _observation_action(report):
    source, result = report["source"], report["result"]
    if result == "unverified":
        return None
    if report.get("reader") in ("another", "unverified"):
        return "confirmation_unchecked"
    if report.get("sourceMatch") == "different":
        return "confirmation_other_file" if source == "jsonl" else "confirmation_other_source"
    if report.get("targetMatch") == "different":
        return "confirmation_other_run"
    if (report.get("reader"), report.get("sourceMatch"), report.get("targetMatch")) != ("me", "original", "original"):
        return "confirmation_details"
    if result == "no-match" and report.get("coverage") != "complete":
        return "confirmation_partial"
    return None


def _reply_stream(facts):
    """Follow the app's known output stream through the original launch's redirections."""
    if "stdoutMergedIntoStderr" in facts:
        return "reply_stdout_discarded" if "discardedStderr" in facts else "reply_stderr"
    if "stderrMergedIntoStdout" in facts:
        return "reply_stdout_discarded" if "discardedStdout" in facts else "reply"
    if "replyOnStderr" in facts:
        return "reply_streams" if "replyOnStdout" in facts and "discardedStdout" not in facts else "reply_stderr"
    return "reply_stdout_discarded" if "discardedStdout" in facts else "reply"


def route(table: Dict[str, Any], question: str, answer: Optional[str] = None,
          shell: Optional[str] = None, inherited: Optional[Dict[str, str]] = None,
          confirmation: Optional[Union[Tuple[str, str], Dict[str, str]]] = None,
          privacy_notes: Tuple[str, ...] = ()) -> Tuple[str, str]:
    origin = _GuideOrigin()
    key, facts = _next_step(table, question, answer, shell, inherited, privacy_notes, origin)
    turns = (question, answer)
    account = _lookup_account(table, turns)
    roles, target_ids, paths, results, unknown = _phrase_observation(table, turns)
    available_id = "availableExactID" in roles or ("receiptID" in roles and len(target_ids) == 1)
    if (roles >= {"assignedFile", "availableFile"} and "jsonl" in (facts | roles) and available_id and len(paths) <= 1
            and len(target_ids) <= 1 and not roles.intersection(("unresolvedProvenance", "absentID"))):
        if key in table["lookupLanguage"]["missingActions"] or key == "fallback":
            key = "jsonl"
    if ("consoleDemo" in roles and "disownedDemo" not in roles and key in ("ask_subject", "ask_exporter")
            and ("wantMeta" in facts or facts.intersection(("askedStderr", "askedStdout",
                                                           "explicitDemoStderr", "explicitDemoStdout")))):
        key = _select(table, facts | {"nodeDemoConsole", "node", "metadata"})
    if ({"readOnlyAccount", "consoleDemo"} <= roles and key == "node_console" and "node" in facts
            and facts.isdisjoint(("inlineConsole", "resolvedConsole", "ranDefaultNodeDemo"))):
        key = "ask_exporter"
    elif ({"consoleDemo", "onlyStdout"} <= roles and ("wantsMetadata" in roles or "wantMeta" in facts)
          and "node" in facts and "disownedDemo" not in roles
          and key in ("ask_exporter", "ask_subject", "node_console", "console", "console_unretained",
                      "console_discarded_metadata")):
        key = "node_stdout_only_metadata"
    elif ("wantsBothStreams" in roles and "disownedDemo" not in roles
          and ("ranDefaultNodeDemo" in facts or {"nodeDemoConsole", "appRan"} <= facts
               or "personalConsoleDemo" in roles)
          and facts.isdisjoint(("discardedStdout", "discardedStderr",
                                "stdoutMergedIntoStderr", "stderrMergedIntoStdout"))):
        key = "node_console"
    if "wantsBothStreams" in roles:
        if "readDemoOnly" in facts and "disownedDemo" in roles:
            key = "node_demo_preview"
        elif "ranDefaultNodeDemo" in facts and ("onlyStderr" in roles or "discardedStdout" in facts):
            key = "node_stderr_only_outputs"
    elif "wantsDecisionRecord" in roles and {"ranDefaultNodeDemo", "discardedStderr"} <= facts:
        key = "node_discarded_decision"
    if "wantsApplicationReply" in roles and key in ("fallback", "ask_subject") and {"node", "stdout"} <= facts:
        key = _reply_stream(facts)
    if {"unavailableOriginal", "reusedTicket"} <= roles and key in ("ask_path", "ask_subject"):
        key = "ask_id"
    if "explicitWorkerConsole" in roles and "redirectErr" in facts and key in ("ask_exporter", "ask_subject"):
        key = "console"
    if ("wantsMetadata" in roles and roles.intersection(("retainedNodeStreams", "ownNodeDecision"))
            and "decisionOnStderr" in facts and "discardedStderr" not in facts):
        key = "node_console"
    if ("wantsApplicationReply" in roles and "node" in facts and key == "fallback" and answer
            and ("stdout" in facts or {"consoleJob", "separateAppReply"} <= roles
                 or roles.intersection(("retainedSourceStreams", "retainedNodeStreams",
                                        "archivedNodeArtifacts")))):
        key = _reply_stream(facts)
    if {"uniqueMarker", "absentID"} <= roles and key in ("ask_id", "fallback", "jsonl_candidates"):
        key = "surviving_marker"
    sources = roles.intersection(("jsonl", "receiver", "storage"))
    if (not unknown and "contraryID" not in roles and len(sources) == 1 and len(results) == len(target_ids) == 1
            and {"reader", "origin", "target"} <= roles and not (account and account.disputed)):
        account = _LookupAccount(next(iter(sources)), results[0], True, False, False)
    if (not unknown and results == ["unlinked"] and sources == {"storage"} and not target_ids
            and {"reader", "origin", "receiptReference"} <= roles and not roles.intersection(("contraryID", "absentID"))
            and not (account and account.disputed)):
        # An unlabeled receipt value does not supply the missing comparison to the returned record.
        account = _LookupAccount("storage", "unlinked", True, False, False)
    if account:
        language = table["lookupLanguage"]
        if not account.disputed and ((account.accounted and account.result != "unknown")
                                     or key in language["missingActions"]):
            key = language["routes"][account.source][account.result if account.accounted else "unknown"]
    if confirmation:
        report = confirmation if isinstance(confirmation, dict) else dict(zip(("source", "result"), confirmation))
        source, result = report["source"], report["result"]
        outcome = table["confirmation"]["outcomes"].get(source, {}).get(result)
        if outcome:
            if origin.files:
                aliases: Dict[str, Set[str]] = {}
                for turn in (question, answer or ""):
                    _source_text(table, turn, aliases)
                origin.files = {original for path in origin.files for original in aliases.get(path, {path})}
            missing = _observation_action(report)
            conflict = _confirmation_conflict(table, source, result, (question, answer), key, facts, origin,
                                              eligible=not missing)
            if (table["lookupLanguage"]["phraseRoles"]["unreadBySource"][source] in roles
                    and result != "unverified" and source in sources):
                conflict = "confirmation_unchecked"
            elif ("contraryID" in roles and source in sources and "reader" in roles and result == "matched"):
                conflict = "confirmation_inconsistent"
            elif account and account.disputed and result != "unverified":
                conflict = "confirmation_ambiguous"
            elif account and account.unknown_clause and account.result != "unknown" and result != "unverified":
                # A recognized record cannot resolve an unconsumed qualification of that lookup.
                if conflict in (None, "ask_evidence"):
                    conflict = "confirmation_unresolved_lookup"
            elif (account and account.accounted and account.result != "unknown" and result != "unverified"
                  and conflict in (None, "ask_evidence", "confirmation_unresolved_lookup")):
                # Only a fully consumed account can resolve a previously unknown observation.
                # A separately established conflict in source, target, multiplicity or scope always stands.
                kind, observed = account.source, account.result
                if source != kind:
                    conflict = "confirmation_unchecked"
                elif observed == "unlinked":
                    conflict = "ask_evidence" if result == "matched" else "confirmation_unresolved_lookup"
                else:
                    agrees = (result == "matched") == (observed == "match")
                    conflict = None if agrees else "confirmation_inconsistent"
            disposition = ("needs_clarification" if conflict == "ask_evidence" and not missing
                           else "contradicts" if conflict or missing else "supports")
            reply = report.get("evidenceReply")
            followup = table["confirmation"]["clarification"]
            if reply is not None and (disposition != "needs_clarification" or reply not in followup["choices"]):
                raise _SessionError("Invalid record-help arguments. See record-help --help.")
            if disposition == "needs_clarification":
                if reply is None:
                    prompt = followup["match" if result == "matched" else "miss"]
                    return "ask_evidence", prompt.format(source=followup["sources"][source])
                key = outcome if reply == "yes" else "confirmation_unresolved_lookup"
            else:
                key = missing if conflict == "ask_evidence" else conflict or missing or outcome
    return key, table["actions"][key]["text"]


def _next_step(table, question, answer=None, shell=None, inherited=None, privacy_notes=(), guide_origin=None):
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

    path_aliases = {}
    source_question = _original_commands(table, _source_text(table, question, path_aliases))
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
    facts.update(_lookup_evidence(table, evidence))
    if {"consoleDecisionExport", "redirectErr"} <= facts and facts.isdisjoint(("decisionOnStdout", "consoleStdout")):
        facts.add("decisionOnStderr")
    if ("storeQuestion" in facts or re.search(table["readbackPair"]["source"], source_question, re.I)):
        readback = _storage_lookup(table, question) if not _sensitive(table, question) else None
        if readback:
            facts.add(readback)
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
    commands, choices, wrapper_facts = _execution_evidence(table, source_question, inherited, guide_origin)
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
    if re.search(table["confirmation"]["attribution"]["sharedIdentifier"], question, re.I):
        reports, quoted = _report_clauses(table, (question,))
        originals, shared, unspecified = _report_identities(table, reports, quoted)
        if (originals.intersection(shared) or unspecified) and not originals.difference(shared):
            facts.discard("hasID")
            facts.add("sharedInput")
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
    if guide_origin is not None and shell_path and not facts.intersection(("wrapperRejected", "inlineUnknown")):
        guide_origin.files.update(path_aliases.get(shell_path, {shell_path}))
    # Positive launch and retained-artifact roles remain available when a clarification chooses the goal.
    artifact_roles = _phrase_observation(table, (source_question,))[0]
    if "ranStock" in artifact_roles and not facts.intersection(("otherProgram", "programAlternatives")):
        facts.update(("stockRan", "quickstart"))
        if facts.isdisjoint(("receiptObserved", "http", "otlp")):
            facts.discard("receiver")
    archived_artifacts = ("archivedNodeArtifacts" in artifact_roles
                          or {"retainedNodeStreams", "nodeOutputInventory"} <= artifact_roles
                          or {"demoReplyRetained", "demoDecisionRetained"} <= artifact_roles
                          or {"consoleJob", "separateAppReply"} <= artifact_roles)
    if archived_artifacts:
        facts.update(("node", "genericResult", "console"))
        if facts.isdisjoint(("replyOnStderr", "stdoutMergedIntoStderr")):
            facts.add("replyOnStdout")
        if facts.isdisjoint(("decisionOnStdout", "stderrMergedIntoStdout", "consoleStdout")):
            facts.add("decisionOnStderr")
    key = _select(table, facts)
    if archived_artifacts and facts.isdisjoint(("wantMeta", "wantReply", "requestedHosted")):
        key = "ask_subject"
    if key == "ask_program" and answer:
        launcher_answer = _original_commands(table, _source_text(table, answer))
        identified, _, _, _, unresolved = _phrase_observation(table, (launcher_answer,))
        if not unresolved and {"readLauncher", "workerIdentified"} <= identified:
            return "console", facts | {"appRan", "console"}
    if guide_origin is not None and ("inlineHTTP" not in facts or facts.intersection(
            ("wrapperRejected", "bootstrapDisabled", "pythonUninstrumented", "inlineUnknown", "loaderOnly"))):
        guide_origin.receivers.clear()
    if key in table["requiresPath"] and not (
        shell_path or entities.get("path", {}).get("value") or "referencedFileSearch" in facts
    ):
        key = "ask_path"
    action = table["actions"][key]
    slot = action.get("slot")
    # A typed clarification fills the missing field of this lookup. Interpret an
    # open-ended answer jointly only when the pending decision has no typed field.
    pending_lookup = _select(table, facts | {slot["fact"]}) if slot else None
    if answer and not slot and (action["kind"] == "ask" or key == "query_storage") and not _sensitive(table, answer):
        incoming = _lookup_evidence(table, answer)
        combined = question + "\n" + answer
        storage = (re.search(table["readbackPair"]["source"], combined, re.I)
                   and _storage_lookup(table, combined) == "linkedStoreReadback")
        if incoming or storage:
            joined_origin = _GuideOrigin()
            joined_key, joined = _next_step(table, combined, shell=shell, inherited=inherited,
                                            guide_origin=joined_origin)
            if _complete_check(table, joined) and table["actions"][joined_key]["kind"] != "ask":
                if guide_origin is not None:
                    guide_origin.files.update(joined_origin.files)
                    guide_origin.receivers.update(joined_origin.receivers)
                return joined_key, joined
    if action["kind"] == "ask" and answer is not None:
        reply_source = answer
        if _sensitive(table, answer) or re.search(table["privateWebAddress"], answer, re.I):
            aside = re.sub(table["sensitiveAsideSplit"], "\n", answer, flags=re.I)
            reply_source = "\n".join(c for c in re.split(table["split"], aside, flags=re.I)
                                     if not (_sensitive(table, c) or re.search(table["privateWebAddress"], c, re.I)))
        path_disowned = False
        if key in ("ask_path", "ask_exporter"):
            attribution = table["confirmation"]["attribution"]
            path_lines = [line.strip() for line in reply_source.splitlines() if line.strip()]
            if len(path_lines) > 1 and _matches(table["replies"]["ask_path"][0], path_lines[0]):
                details = path_lines[1:]
                if all(re.search(attribution["reportedForeign"], line, re.I)
                       and re.search(attribution["foreign"], line, re.I) for line in details):
                    reply_source = path_lines[0]
                else:
                    path_disowned = any(re.search(attribution["foreign"], line, re.I) for line in details)
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
        if key == "ask_path" and (launches or calls):
            # The reply must actually resolve the pending file, even when runtime activation is conditional.
            reply_origin = _GuideOrigin()
            resolved, _ = _next_step(table, launch_reply, shell=shell, inherited=inherited, guide_origin=reply_origin)
            safe_launch = resolved in table["fileActions"] and len(reply_origin.files) == 1
        unsafe_reply = ((_sensitive(table, question) and not (safe_choice or safe_path or safe_launch))
                        or any(value in reply_source for value in raw_private))
        if safe_launch and not unsafe_reply:
            # Reuse the first-turn routing path with the accepted original launch as the clarification.
            # The combined session has no pending answer, so this cannot trigger another clarification.
            choice, combined_facts = _next_step(table, question + "\n" + launch_reply, shell=shell, inherited=inherited,
                                                privacy_notes=(*privacy_notes, answer), guide_origin=guide_origin)
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
        reply_id = reply_entities.get(slot["entity"], {}) if slot else {}
        if slot and reply_id.get("value") and "originalEmittedID" in _lookup_evidence(table, reply):
            reply_id.update({"fact": "hasID", "independent": True})
        repeated = not facts.isdisjoint(("sharedInput", "multiple"))
        shared_value = (entities.get("businessTicket") or entities.get("correlation") or {}).get("value")
        valid_id = not slot or (reply_id.get("fact") == slot["fact"]
                                and not (repeated and reply_id.get("value") == shared_value))
        if slot and re.search(table["confirmation"]["attribution"]["sharedIdentifier"], answer, re.I):
            valid_id = False
        if key in ("ask_exporter", "ask_path") and (path_disowned or len(_report_files(table, answer, {})) > 1):
            valid = None
            facts.add("unresolvedPath")
        if key == "ask_path" and valid is None:
            facts.add("pendingPath")
        if slot and (not valid_id or valid is None):
            facts.add("unresolvedOriginal")
        if not reply or re.fullmatch(table["unknown"], reply, re.I) or valid is None or not valid_id:
            key = table["fallback"]
        else:
            if key in ("ask_path", "ask_exporter"):
                shell_path = None
            if guide_origin is not None and key in ("ask_path", "ask_exporter") and safe_path:
                path = reply_entities.get("path", {}).get("value")
                if path:
                    guide_origin.files = {path}
            if guide_origin is not None and slot and valid_id:
                setattr(guide_origin, slot["origin"], reply_id.get("value"))
            entities.update(reply_entities)
            evidence = contextual_reply + "\n" + evidence
            if action.get("artifactSlot") and not valid.get("action"):
                goal_facts = (facts - set(valid.get("drop", []))) | set(valid.get("add", []))
                for choice in table["decisions"]:
                    if (choice.get("resolvesArtifact") and goal_facts.issuperset(choice["all"])
                            and goal_facts.isdisjoint(choice.get("none", []))):
                        return choice["action"], goal_facts
            if key in ("ask_subject", "ask_run") and not valid.get("action"):
                facts.update(_collect_facts(table, _scoped(table, preprocess(projected))))
                facts.update(preflight)
                if len(launches) == 1 or calls or not facts.isdisjoint(("file", "jsonl")):
                    combined, combined_facts = _next_step(table, question + "\n" + launch_reply, shell=shell,
                                                          inherited=inherited, guide_origin=guide_origin)
                    if table["actions"][combined]["kind"] != "ask":
                        return combined, combined_facts
            if valid.get("resumeLocal"):
                local_facts = facts - {"hosted", "requestedHosted", "confirmedHosted"}
                local_action = _select(table, local_facts)
                if (table["actions"][local_action]["kind"] != "ask"
                        and local_action not in (table["fallback"], "hosted")):
                    return local_action, local_facts
            facts.difference_update(valid.get("drop", []))
            facts.update(valid.get("add", []))
            key = valid.get("action") or pending_lookup or _select(table, facts)
            unresolved_path = key in table["requiresPath"] and not (shell_path or entities.get("path", {}).get("value"))
            if table["actions"][key]["kind"] == "ask" or unresolved_path:
                key = table["fallback"]
            if key == "reply":
                facts.update(_collect_facts(table, _source_text(table, reply_source))
                             & {"replyOnStdout", "replyOnStderr"})
    if key == "reply":
        key = _reply_stream(facts)
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


def _confirmation(table: Dict[str, Any], line: str) -> Optional[Dict[str, str]]:
    parts = line.split()
    info = table["confirmation"]
    if len(parts) < 2 or parts[1] not in info["outcomes"].get(parts[0], {}):
        return None
    fields = ("source", "result", "reader", "sourceMatch", "targetMatch", "coverage")
    if len(parts) > len(fields) or any(
        value not in info["observation"][field] for field, value in zip(fields[2:], parts[2:])
    ):
        raise _SessionError("Choose supported observation values in the order shown in --help.")
    return dict(zip(fields, parts))


class _SessionError(ValueError):
    """All messages are public constant diagnostics."""


def _session(table, args, turns):
    if bool(args.source) != bool(args.result):
        raise _SessionError("Confirmation needs both --source and --result.")
    if args.answer and len(args.answer) != 1:
        raise _SessionError("Supply one clarification answer, once.")
    answer = args.answer[0] if args.answer else None
    pair = None
    options = {"reader": args.reader, "sourceMatch": args.source_match,
               "targetMatch": args.target_match, "coverage": args.coverage}
    if any(options.values()) and not args.source:
        raise _SessionError("An observation needs both --source and --result.")
    if args.source:
        if (len(args.source) != 1 or len(args.result) != 1
                or args.result[0] not in table["confirmation"]["outcomes"].get(args.source[0], {})):
            raise _SessionError("Choose exactly one supported source and one observed result. See --help.")
        pair = {"source": args.source[0], "result": args.result[0]}
        for option, values in options.items():
            if values:
                if len(values) != 1 or values[0] not in table["confirmation"]["observation"][option]:
                    raise _SessionError("Choose one supported value for each observation field. See --help.")
                pair[option] = values[0]
    lines = ([args.question] if args.question is not None else []) + [line for line in turns if line.strip()]
    replies = list(getattr(args, "evidence_reply", None) or [])
    # A final bare response is valid only after the observation it answers.
    if len(lines) >= 2 and lines[-1] in table["confirmation"]["clarification"]["choices"] and (
        pair or _confirmation(table, lines[-2])
    ):
        replies.append(lines.pop())
    for index, line in enumerate(list(lines)):
        if re.match(r"^evidence(?:\s|$)", line, re.I):
            if index != len(lines) - 1 or len(line.split()) != 2:
                raise _SessionError("Invalid record-help arguments. See record-help --help.")
            replies.append(line.split()[1])
            lines.pop()
    reports = [(index, item) for index, line in enumerate(lines) if (item := _confirmation(table, line))]
    if ((answer and _confirmation(table, answer)) or
            (reports and (pair or len(reports) != 1 or reports[0][0] != len(lines) - 1))):
        raise _SessionError("Supply one source and result together, once, at the end of the session.")
    if reports:
        pair = reports[0][1]
        lines.pop()
    if replies:
        choices = table["confirmation"]["clarification"]["choices"]
        if args.static or not pair or len(replies) != 1 or replies[0] not in choices:
            raise _SessionError("Invalid record-help arguments. See record-help --help.")
        pair["evidenceReply"] = replies[0]
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


def _verify_local_file(table, args):
    from briefcase.cli._record_file import verify

    incompatible = ("static", "answer", "source", "result", "reader", "source_match", "target_match",
                    "coverage", "evidence_reply")
    if (len(args.verify_jsonl) != 1 or args.question is not None
            or any(getattr(args, opt, None) for opt in incompatible)):
        raise _SessionError("Invalid record-help arguments. See record-help --help.")
    spec = table["localFileCheck"]
    limit = spec["limits"]["lineBytes"]
    original_id = ""
    try:
        if sys.stdin.isatty():
            import getpass
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                original_id = getpass.getpass("Original decision ID (input hidden): ")
        else:
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            raw = stream.readline(limit + 3)
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if line.endswith("\n"):
                line = line[:-1].removesuffix("\r")
            if not stream.read(1):
                original_id = line
        if not original_id or len(original_id.encode("utf-8")) > limit:
            raise ValueError
    except (EOFError, OSError, UnicodeError, ValueError, Warning):
        print(spec["actions"]["file_check_incomplete"])
        return 1
    result = verify(args.verify_jsonl[0], original_id, spec["limits"])
    print(spec["actions"][result])
    return int(result == "file_check_incomplete")


def _queued_terminal_lines(stream):
    """Collect input already submitted to an otherwise complete flag invocation."""
    if stream.seekable():
        return stream.read().splitlines()
    if os.name == "nt":
        import msvcrt

        chars = []
        while msvcrt.kbhit():
            chars.append(msvcrt.getwch())
        return "".join(chars).splitlines()

    import select

    # Read from the descriptor before any buffered stdin reads. In canonical
    # mode even a partially typed line stays in the terminal until submitted.
    fd = stream.fileno()
    chunks = []
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 8192)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(stream.encoding or "utf-8", errors="replace").splitlines()


def _guide_session(args) -> int:
    table = json.loads(_TABLE.read_text(encoding="utf-8"))
    if getattr(args, "verify_jsonl", None):
        return _verify_local_file(table, args)
    interactive = _ux.is_interactive() and sys.stdin.isatty()
    turns: List[str] = []

    eof = False
    eof_hint = "Ctrl+Z then Enter" if os.name == "nt" else "Ctrl+D"

    def read_turn(prompt: str):
        nonlocal eof
        if eof:
            return _session(table, args, turns)
        print(prompt, end="", flush=True)
        for raw in sys.stdin:
            line = raw.strip()
            if line:
                turns.append(line)
                break
        else:
            eof = True
        return _session(table, args, turns)

    def finish_report():
        nonlocal eof
        if interactive and not eof:
            print(f"Press {eof_hint} on an empty line to finish this report: ", end="", flush=True)
            turns.extend(line.strip() for line in sys.stdin if line.strip())
            eof = True
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

    def show_action(action, message):
        print(message, flush=True)
        if action in table["localFileCheck"]["eligibleActions"]:
            print(table["localFileCheck"]["cliDiscovery"], flush=True)

    if not confirmation and not args.answer and table["actions"][key]["kind"] == "ask":
        show_action(key, text)
        if answer is None:
            if not interactive:
                return 0
            question, answer, confirmation = read_turn("Answer (or unknown): ")
    if answer is not None and table["actions"][key]["kind"] == "ask":
        shell, inherited = _session_environment(table, question + "\n" + answer)
    key, text = route(table, question, answer, shell, inherited, confirmation, tuple(turns))
    prompted = not confirmation and interactive and not eof and key in table["confirmation"]["next"]
    previous_key = key
    if prompted:
        show_action(key, text)
        print(table["confirmation"]["instruction"])
        question, answer, confirmation = read_turn(f"Report your lookup or end input ({eof_hint}): ")
        if answer and not confirmation and not eof and turns[-1].strip():
            applied, instruction = route(table, question, answer, shell, inherited)
            if applied != previous_key:
                show_action(applied, instruction)
                previous_key = applied
            question, answer, confirmation = read_turn(f"Confirm what you found or end input ({eof_hint}): ")
        key, text = route(table, question, answer, shell, inherited, confirmation, tuple(turns))
    if sys.stdin.isatty() and confirmation and not turns and not (interactive and key == "ask_evidence"):
        queued = _queued_terminal_lines(sys.stdin)
        if queued:
            turns.extend(queued)
            question, answer, confirmation = _session(table, args, turns)
            shell, inherited = _session_environment(table, question)
            if answer and table["actions"][route(table, question, None, shell, inherited)[0]]["kind"] == "ask":
                shell, inherited = _session_environment(table, question + "\n" + answer)
            key, text = route(table, question, answer, shell, inherited, confirmation, tuple(turns))
            # A completed flag invocation consumes submitted input without
            # waiting for a second end-of-input gesture.
            eof = key != "ask_evidence"
    evidence_prompted = False
    if interactive and confirmation and (turns or key == "ask_evidence"):
        if key == "ask_evidence" and not eof:
            print(text, flush=True)
            evidence_prompted = True
            print(table["confirmation"]["clarification"]["help"], flush=True)
            raw = sys.stdin.readline()
            if raw:
                value = raw.strip()
                if value in table["confirmation"]["clarification"]["choices"]:
                    value = "evidence " + value
                if value:
                    turns.append(value)
            else:
                eof = True
        question, answer, confirmation = finish_report()
        key, text = route(table, question, answer, shell, inherited, confirmation, tuple(turns))
    if (not prompted or confirmation or key != previous_key) and not (evidence_prompted and key == "ask_evidence"):
        show_action(key, text)
        if key == "ask_evidence":
            print(table["confirmation"]["clarification"]["help"])
    if not prompted and key in table["confirmation"]["next"]:
        print(table["confirmation"]["instruction"])
    if key == table["fallback"]:
        print("\n" + table["guide"])
    elif key in ("fresh", "future", "memory_lost"):
        print("Open the example with: briefcase-ai record-help --static")
    return 0
