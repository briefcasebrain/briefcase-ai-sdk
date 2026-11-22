"""``briefcase guard``: see what the content guardrails find in a text, and why.

    briefcase guard test --text "call me on +44 20 7946 0958, password=hunter22"
    briefcase guard test --file prompt.txt --source tool        # as a tool result
    briefcase guard init                                         # write ~/.briefcase/guard.toml

Checks come from ~/.briefcase/guard.toml ($BRIEFCASE_GUARD_FILE) or the defaults
(PII: redact in prompts, warn in completions; prompt-injection heuristics on tool and
retrieved content: warn). Apps enforce them after briefcase.guardrails.content.enable().
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from briefcase.cli import _ux

GROUP = "enforce"
ORDER = 45

TEST_EXAMPLE = 'example: briefcase guard test --text "call +44 20 7946 0958, password=hunter22"'

#: Why a finding kind fires, for ``guard test``.
WHY = {
    "secret_assignment": "a password/secret/key name assigned a value (key=value, key: value, JSON)",
    "authorization": "an Authorization header credential or a Bearer token",
    "private_key": "a PEM/OpenSSH/PGP private key block",
    "phone_intl": "+ and a country code followed by 8-15 digits in groups",
    "phone_national": "a national phone format (French pairs, (area) code, or 0... next to a phone word)",
    "email": "an e-mail address",
    "credit_card": "13-19 digits passing the Luhn check",
    "ssn": "a US social security number shape",
    "api_key": "a known API-key prefix (sk-, AKIA, ghp_, ...)",
    "ignore_instructions": "asks the model to ignore earlier instructions",
    "new_instructions": "announces new or hidden instructions",
    "role_override": "tries to switch the model into an unrestricted role",
    "chat_template_tokens": "contains chat-template control tokens",
    "prompt_leak": "asks the model to reveal its system prompt",
    "conceal_from_user": "tells the model to hide something from the user",
    "exfiltration_link": "a markdown image whose URL carries data in its query",
    "exfiltration_request": "asks to send conversation data or secrets to an address",
    "classifier": "the configured classifier flagged it",
    "match": "the output regex matched",
    "no_match": "the required output regex did not match",
    "not_json": "the completion is not JSON",
    "schema_violation": "the JSON does not match the schema",
    "rejected": "the callable check returned False",
    "checker_error": "the check itself raised; its on_error setting decided",
}

DEFAULT_TOML = """# briefcase content guardrails (docs: docs/features/content-guardrails.md).
# Enforced in apps that call briefcase.guardrails.content.enable().

[pii]
input = "redact"      # block | redact | warn | off  (request text, before it is sent)
output = "warn"       # block | redact | warn | off  (the completion)

[injection]
effect = "warn"       # block | warn
sources = ["tool", "retrieved"]

# [[output]]
# regex = "(?i)internal use only"
# effect = "block"

[stream]
chunk_check_chars = 0  # >0: re-check streamed text every N characters and stop early
"""


def register(sub) -> None:
    fmt = argparse.RawDescriptionHelpFormatter
    p = sub.add_parser(
        "guard", help="Content guardrails: test what fires on a text and why.",
        description="PII/secret, prompt-injection and output-policy checks on LLM calls. "
                    "Config: ~/.briefcase/guard.toml or $BRIEFCASE_GUARD_FILE.",
        epilog=TEST_EXAMPLE, formatter_class=fmt)
    p.set_defaults(func=cmd_guard, guard_parser=p)
    gs = p.add_subparsers(dest="guard_cmd")

    sp = gs.add_parser("test", help="Run the checks on a text and explain every finding.",
                       description="Nothing is sent anywhere. The text is read from --text, "
                                   "--file, or stdin ('-').",
                       epilog=TEST_EXAMPLE, formatter_class=fmt)
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="The text to check.")
    src.add_argument("--file", help="A file to check ('-' for stdin).")
    sp.add_argument("--direction", choices=("input", "output"), default="input",
                    help="Check as request text (default) or as a completion.")
    sp.add_argument("--source", choices=("user", "system", "assistant", "tool", "retrieved"),
                    default="user", help="Where the text sits in the request (input only).")
    sp.add_argument("--all-sources", action="store_true",
                    help="Also run checks limited to other sources (e.g. injection on user text).")
    _ux.add_json_flag(sp)
    sp.set_defaults(func=cmd_test)

    sp = gs.add_parser("show", help="The checks in effect (guard.toml or the defaults).",
                       epilog="example: briefcase guard show --json", formatter_class=fmt)
    _ux.add_json_flag(sp)
    sp.set_defaults(func=cmd_show)

    sp = gs.add_parser("init", help="Write a starting guard.toml (the defaults, commented).",
                       epilog="example: briefcase guard init --dry-run", formatter_class=fmt)
    sp.add_argument("--dry-run", dest="guard_dry_run", action="store_true")
    sp.add_argument("--yes", "-y", action="store_true")
    sp.set_defaults(func=cmd_init)


def _guard() -> Any:
    from briefcase.guardrails import content

    loaded = content.load()
    return loaded if loaded is not None else content.ContentGuard(content.default_checks()), \
        loaded is not None


def cmd_guard(args, store=None, engine=None, stack=None) -> int:
    args.guard_parser.print_help()
    _ux.print_next('briefcase guard test --text "call +44 20 7946 0958"')
    return 0


def cmd_test(args, store=None, engine=None, stack=None) -> int:
    from briefcase.guardrails import content
    from briefcase.guardrails.content.checks import ContentGuard
    from briefcase.guardrails.content.detectors import label

    if args.text is not None:
        text = args.text
    elif args.file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _ux.fail(f"cannot read {args.file}: {exc.strerror or exc}", code=_ux.EXIT_USAGE)
    try:
        guard, from_file = _guard()
    except ValueError as exc:
        return _ux.fail(str(exc), fix=f"briefcase guard init --yes  # or edit {content.config_path()}")
    if args.all_sources:
        import copy

        checks = []
        for check in guard.checks:
            clone = copy.copy(check)
            clone.sources = ("system", "user", "assistant", "tool", "retrieved")
            checks.append(clone)
        guard = ContentGuard(checks, chunk_check_chars=guard.chunk_check_chars,
                             unredactable=guard.unredactable)
    source = "assistant" if args.direction == "output" else args.source
    decision = guard.scan(text, args.direction, source)
    redacted = guard.apply(text, decision)
    spans = _spans(guard, text, args.direction, source)
    result = {
        "config": str(content.config_path()) if from_file else "defaults",
        "native_sanitizer": content.native_available(),
        "direction": args.direction, "source": source, "effect": decision.effect,
        "findings": decision.findings, "errors": decision.errors, "spans": spans,
        "redacted": redacted if decision.redactions else None,
        "elapsed_ms": round(decision.elapsed_ms, 3),
    }
    if args.json:
        return _ux.print_json(result)
    print(f"checks: {result['config']}; PII detectors: "
          + ("native Sanitizer + Python" if result["native_sanitizer"]
             else "Python only (native Sanitizer not available)"))
    print(f"checked as {args.direction}" + (f" from {source}" if args.direction == "input" else "")
          + f" ({len(text)} characters, {result['elapsed_ms']} ms)")
    skipped = [c.name for c in guard.checks if not c.applies(args.direction, source)
               and c.direction in (args.direction, "both")]
    if skipped:
        print(f"not run for this source: {', '.join(sorted(set(skipped)))} "
              "(--source tool, or --all-sources)")
    if not decision.findings and not decision.errors:
        print("nothing found -> allow")
        _ux.print_next("briefcase guard show", "see which checks ran")
        return 0
    for item in spans:
        where = f"chars {item['start']}-{item['end']}" if item["start"] >= 0 else "whole text"
        print(f"  {item['check']}: {item['kind']} at {where} -> {item['effect']}"
              + (f" [REDACTED_{label(item['kind'])}]" if item["effect"] == "redact" else ""))
        print(f"      why: {WHY.get(item['kind'], item['kind'])} ({item['detector']})")
    for error in decision.errors:
        print(f"  error: {error}")
    print(f"decision: {decision.effect.upper()}")
    if result["redacted"] is not None:
        print("after redaction:")
        print("  " + redacted.replace("\n", "\n  "))
    _ux.print_next("briefcase guard show", "see every check and its effect")
    return 0


def _spans(guard: Any, text: str, direction: str, source: str) -> list:
    """Per-finding positions, for the explanation (the decision itself
    aggregates kinds)."""
    out = []
    for check in guard.checks:
        if not check.applies(direction, source):
            continue
        try:
            findings = check.find(text, source)
        except Exception as exc:
            out.append({"check": check.name, "kind": "checker_error", "start": -1, "end": -1,
                        "effect": check.on_error, "detector": type(exc).__name__})
            continue
        effect = check.effect
        if effect == "redact" and not all(f.redactable for f in findings):
            effect = "block"
        for f in findings:
            out.append({"check": check.name, "kind": f.kind, "start": f.start, "end": f.end,
                        "effect": effect, "detector": f.detector})
    return out


def cmd_show(args, store=None, engine=None, stack=None) -> int:
    from briefcase.guardrails import content

    try:
        guard, from_file = _guard()
    except ValueError as exc:
        return _ux.fail(str(exc), fix=f"edit {content.config_path()}")
    info = {"config": str(content.config_path()) if from_file else "defaults (no guard.toml)",
            "native_sanitizer": content.native_available(),
            "chunk_check_chars": guard.chunk_check_chars, "unredactable": guard.unredactable,
            "checks": guard.describe()}
    if args.json:
        return _ux.print_json(info)
    print(f"checks from: {info['config']}")
    for check in info["checks"]:
        sources = "" if check["direction"] == "output" else f", sources {','.join(check['sources'])}"
        print(f"  {check['check']}: {check['direction']} -> {check['effect']} "
              f"(on error: {check['on_error']}{sources})")
    print(f"streams: {'re-check every ' + str(guard.chunk_check_chars) + ' chars' if guard.chunk_check_chars else 'final text only'}; "  # noqa: E501
          f"unredactable paths: {guard.unredactable}")
    _ux.print_next('briefcase guard test --text "password=hunter22"')
    return 0


def cmd_init(args, store=None, engine=None, stack=None) -> int:
    from briefcase.guardrails import content

    path = content.config_path()
    if path.exists():
        print(f"{path} exists; leaving it as it is.")
        _ux.print_next("briefcase guard show")
        return 0
    diff = "".join(difflib.unified_diff([], DEFAULT_TOML.splitlines(True), "/dev/null", str(path)))
    print(diff, end="")
    if args.guard_dry_run:
        print("Dry run: nothing written.")
        return 0
    from briefcase.pricing import briefcase_home

    inside = True
    try:
        path.resolve().relative_to(briefcase_home().resolve())
    except ValueError:
        inside = False
    if not inside:
        answer = _ux.confirm(f"Write {path}?", assume_yes=args.yes)
        if not answer:
            return _ux.fail(f"{path} is outside ~/.briefcase; not written",
                            fix="briefcase guard init --yes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_TOML, encoding="utf-8")
    print(f"Wrote {path} (undo: delete it). Apps enforce it after "
          "briefcase.guardrails.content.enable().")
    _ux.print_next('briefcase guard test --text "password=hunter22"')
    return 0


def status() -> Optional[Dict[str, Any]]:
    from briefcase.guardrails import content

    path = content.config_path()
    if not path.exists():
        return {"id": "content-guardrails", "title": "Content guardrails", "state": "off",
                "detail": "no guard.toml (apps may still call content.guard() in code)",
                "next": "briefcase guard init"}
    try:
        guard = content.load(path)
    except ValueError as exc:
        return {"id": "content-guardrails", "title": "Content guardrails", "state": "attention",
                "detail": f"guard.toml is invalid: {str(exc)[:120]}", "next": "briefcase guard show"}
    count = len(guard.checks) if guard else 0
    native = content.native_available()
    detail = (f"{count} check{'s' if count != 1 else ''} in guard.toml; enforced in apps that call "
              "briefcase.guardrails.content.enable()")
    if not native:
        detail += "; PII via Python detectors only (native Sanitizer not built)"
    return {"id": "content-guardrails", "title": "Content guardrails", "state": "on",
            "detail": detail, "next": None}
