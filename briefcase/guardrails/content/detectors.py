"""Text detectors for the content guardrails: PII and secrets, and
prompt-injection heuristics.

PII: the native ``briefcase.sanitize.Sanitizer`` (Rust; e-mail, cards, SSN,
IBAN, North-American and ``+``-prefixed contiguous phone numbers, IPs, API
keys, JWTs) when the compiled extension is importable, plus pure-Python
detectors for what it does not cover:

=====================  =====================================================
kind                   what
=====================  =====================================================
``secret_assignment``  ``password=…``, ``"api_key": "…"``, ``DB_PASSWORD=…``,
                       ``client_secret: …``. Only the value is redacted.
                       Placeholders (``***``, ``<your-key>``, ``${VAR}``,
                       ``{{ var }}``, ``YOUR_API_KEY``, ``changeme``) and
                       code (``os.environ[...]``) are ignored; after ``:`` an
                       unquoted value must contain a digit or symbol, so
                       prose ("password: required") does not fire.
``authorization``      ``Authorization: Bearer|Basic|Token <credential>`` and
                       ``Bearer <token>`` (16+ characters with a digit).
``private_key``        PEM / OpenSSH / PGP private key blocks, also when
                       truncated (to the end of the text). Public keys and
                       certificates are not matched.
``phone_intl``         ``+`` and a country code, then 8-15 digits in groups
                       (``+44 20 7946 0958``, ``+33 1 23 45 67 89``,
                       ``+49 (0)30 12345678``, ``+1-202-555-0143``).
``phone_national``     French pairs (``01 23 45 67 89``), a parenthesised
                       area code (``(02) 1234 5678``), or a trunk-0 number
                       (``020 7946 0958``, ``030 12345678``, ``07700 900123``)
                       **only with a phone word** (tel, phone, mobile, call,
                       fax, Telefon, …) in the 40 characters before it.
=====================  =====================================================

Without the native extension, a small fallback set also runs: ``email``,
``credit_card`` (Luhn-checked), ``ssn`` (``123-45-6789`` shape) and
``api_key`` (common provider prefixes). Names and postal addresses are not
detected by anything here.

Prompt injection: :data:`INJECTION_PATTERNS`, a short, conservative set of
phrases seen in published injection examples. Heuristics like these are
easy to bypass (paraphrase, another language, encoding, splitting across
chunks); treat a miss as no evidence of safety. Plug a model-based detector
in through ``classifier=`` (see :mod:`briefcase.guardrails.content`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Pattern, Tuple


@dataclass(frozen=True)
class Finding:
    """One match. ``start`` / ``end`` index the text (characters); ``span``
    findings can be redacted, whole-text findings (``start == end == -1``)
    cannot."""

    kind: str
    start: int
    end: int
    detector: str

    @property
    def redactable(self) -> bool:
        return 0 <= self.start < self.end


# -- secrets ------------------------------------------------------------------------

_SECRET_NAMES = (
    r"password|passwd|pwd|passphrase|secret|client[_-]?secret|secret[_-]?key|api[_-]?key|"
    r"apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?key|"
    r"db[_-]?pass(?:word)?"
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,30}[_.-]){0,3}(?:" + _SECRET_NAMES + r")"
    r"[\"']?[ \t]*(?P<op>:=|=>|=|:)[ \t]*"
    r"(?P<val>\"[^\"\n]{1,256}\"|'[^'\n]{1,256}'|[^\s\"',;&}<>()\[\]]{1,256})"
)
_PLACEHOLDER = re.compile(
    r"(?i)^(?:\*+|x{3,}|\.{2,}|-+|_+|<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\{\{?[^}]*\}?\}|"
    r"%\(?\w*\)?[sd]|null|none|nil|true|false|undefined|redacted|\[redacted[^\]]*\]|"
    r"changeme|change[_-]?me|example|placeholder|secret|password|your[_-a-z0-9]*|"
    r"my[_-]?(?:password|secret|key|token)|\[?redacted_[a-z_]+\]?)$"
)
_ENV_NAME = re.compile(r"^[A-Z][A-Z_]*$")


_SECRET_HINTS = ("pass", "pwd", "secret", "key", "token")


def _secret_assignments(text: str, lower: str) -> List[Finding]:
    out: List[Finding] = []
    if not any(h in lower for h in _SECRET_HINTS):
        return out
    for match in SECRET_ASSIGNMENT.finditer(text):
        raw = match.group("val")
        quoted = raw[:1] in "\"'" and raw[-1:] == raw[:1] and len(raw) >= 2
        value = raw[1:-1] if quoted else raw
        start = match.start("val") + (1 if quoted else 0)
        if not value.strip() or _PLACEHOLDER.match(value) or _ENV_NAME.match(value):
            continue
        if value.startswith(("os.environ", "process.env", "getenv", "env(", "ENV[", "$")):
            continue
        if match.group("op") == ":" and not quoted:
            if len(value) < 6 or not re.search(r"[0-9\W_]", value) or value.endswith(":"):
                continue
        elif len(value) < 4:
            continue
        out.append(Finding("secret_assignment", start, start + len(value), "python"))
    return out


AUTHORIZATION = re.compile(
    r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:(?P<scheme>bearer|basic|token|apikey|digest|"
    r"negotiate)\s+)?(?P<val>[A-Za-z0-9._~+/=-]{8,})"
)
BEARER = re.compile(r"(?i)\bbearer\s+(?P<val>[A-Za-z0-9._~+/-]{16,}=*)")


def _authorization(text: str, lower: str) -> List[Finding]:
    out: List[Finding] = []
    if "authorization" not in lower and "bearer" not in lower:
        return out
    for match in AUTHORIZATION.finditer(text):
        value = match.group("val")
        if not match.group("scheme") and not (len(value) >= 16 and re.search(r"\d", value)):
            continue
        out.append(Finding("authorization", match.start("val"), match.end("val"), "python"))
    for match in BEARER.finditer(text):
        value = match.group("val")
        if re.search(r"\d", value) and re.search(r"[A-Za-z]", value):
            out.append(Finding("authorization", match.start("val"), match.end("val"), "python"))
    return out


PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL,
)


def _private_keys(text: str, lower: str) -> List[Finding]:
    if "private key" not in lower:
        return []
    return [Finding("private_key", m.start(), m.end(), "python") for m in PRIVATE_KEY.finditer(text)]


# -- phone numbers ----------------------------------------------------------------------

PHONE_INTL = re.compile(
    r"(?<![\w+.])\+[1-9][0-9]{0,2}(?:[ .-]?\(0\))?(?:[ .-]?\(?[0-9]{1,5}\)?){1,6}(?![\w.]?[0-9])"
)
PHONE_FR = re.compile(r"(?<![\w.+-])0[1-9](?:[ .][0-9]{2}){4}(?![\w.-]|\s[0-9])")
PHONE_PAREN = re.compile(r"(?<![\w(])\(0?[0-9]{1,4}\)[ -]?[0-9]{3,5}[ -]?[0-9]{3,5}(?![\w-]|[.,][0-9])")
PHONE_TRUNK = re.compile(
    r"(?<![\w.+/-])0[0-9]{2,4}[ /-][0-9]{3,8}(?:[ -][0-9]{2,4})?(?![\w/-]|[.,][0-9])"
)
PHONE_CONTEXT = re.compile(
    r"(?i)(?:phone|tel\b|tel\.|tél|telefon|telephone|téléphone|teléfono|telefono|mobile|mob\b|"
    r"cell|call|fax|whatsapp|sms|text me|ring|handy|portable|móvil|movil|celular|ph\.|☎|📞|電話)"
)


#: Lower-case substrings of every PHONE_CONTEXT word: a cheap test before the regex.
_PHONE_HINTS = ("phone", "tel", "tél", "mob", "cell", "call", "fax", "whatsapp", "sms",
                "text me", "ring", "handy", "portable", "móvil", "movil", "celular", "ph.",
                "☎", "📞", "電話")


def _digits(text: str) -> int:
    return sum(c.isdigit() for c in text)


def _phones(text: str, lower: str) -> List[Finding]:
    out: List[Finding] = []
    for match in (PHONE_INTL.finditer(text) if "+" in text else ()):
        body = match.group(0).replace("(0)", "")
        digits = _digits(body)
        groups = re.findall(r"[0-9]+", body)
        if not 8 <= digits <= 15:
            continue
        if len(groups) > 1 and any(len(g) > 5 for g in groups[1:-1]):
            continue
        out.append(Finding("phone_intl", match.start(), match.end(), "python"))
    has_zero = "0" in text
    has_context = has_zero and any(h in lower for h in _PHONE_HINTS)
    for pattern, low, high, context in ((PHONE_FR, 10, 10, False), (PHONE_PAREN, 9, 12, False),
                                        (PHONE_TRUNK, 9, 12, True)):
        if pattern is PHONE_PAREN:
            if "(" not in text:
                continue
        elif not has_zero or (context and not has_context):
            continue
        for match in pattern.finditer(text):
            if not low <= _digits(match.group(0)) <= high:
                continue
            if context and not PHONE_CONTEXT.search(text, max(0, match.start() - 40), match.start()):
                continue
            out.append(Finding("phone_national", match.start(), match.end(), "python"))
    return out


# -- fallbacks for when the native Sanitizer is not available -----------------------------

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# The leading digit is matched before the look-behind so the scan can skip
# non-digits quickly.
CARD = re.compile(r"\d(?<![\d-]\d)(?:[ -]?\d){11,17}[ -]?\d(?![\d-])")
SSN = re.compile(r"\d(?<![\d-]\d)\d\d(?<!000)(?<!666)(?<!9\d\d)-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
API_KEY = re.compile(
    r"\b(?:sk-|sk_live_|sk_test_|pk_live_|rk_live_|AIza|AKIA|ghp_|gho_|ghu_|ghs_|ghr_|"
    r"github_pat_|glpat-|hf_|xox[bpoa]-)[A-Za-z0-9_-]{15,}\b"
)


def _luhn(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


_API_HINTS = ("sk-", "sk_", "pk_", "rk_", "aiza", "akia", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
              "github_pat_", "glpat-", "hf_", "xox")


def _fallback(text: str, lower: str) -> List[Finding]:
    out: List[Finding] = []
    if "@" in text:
        out += [Finding("email", m.start(), m.end(), "python-fallback") for m in EMAIL.finditer(text)]
    out += [Finding("credit_card", m.start(), m.end(), "python-fallback")
            for m in CARD.finditer(text) if 13 <= _digits(m.group(0)) <= 19 and _luhn(m.group(0))]
    if "-" in text:
        out += [Finding("ssn", m.start(), m.end(), "python-fallback") for m in SSN.finditer(text)]
    if any(h in lower for h in _API_HINTS):
        out += [Finding("api_key", m.start(), m.end(), "python-fallback")
                for m in API_KEY.finditer(text)]
    return out


PYTHON_DETECTORS: Tuple[Callable[[str, str], List[Finding]], ...] = (
    _secret_assignments, _authorization, _private_keys, _phones,
)


# -- the native Sanitizer ---------------------------------------------------------------

_native: List[Any] = []  # [sanitizer or None], resolved once


def native_sanitizer() -> Optional[Any]:
    """The native ``Sanitizer`` when the compiled extension is importable
    and behaves like it (checked once on a known e-mail), else None."""
    if _native:
        return _native[0]
    sanitizer = None
    try:
        from briefcase._native import Sanitizer

        if isinstance(Sanitizer, type):
            candidate = Sanitizer()
            probe = candidate.sanitize("contact a.b@example.com")
            if isinstance(probe.sanitized, str) and "a.b@example.com" not in probe.sanitized \
                    and isinstance(list(probe.redactions), list):
                sanitizer = candidate
    except Exception:
        sanitizer = None
    _native.append(sanitizer)
    return sanitizer


def _char_index(text: str, encoded: bytes, byte_index: int) -> int:
    return len(encoded[:byte_index].decode("utf-8", "ignore"))


def native_findings(text: str, sanitizer: Any) -> List[Finding]:
    result = sanitizer.sanitize(text)
    redactions = list(result.redactions)
    if not redactions:
        return []
    ascii_only = text.isascii()
    encoded = b"" if ascii_only else text.encode("utf-8")
    out = []
    for item in redactions:
        start, end = int(item.start_position), int(item.end_position)
        if not ascii_only:
            start, end = _char_index(text, encoded, start), _char_index(text, encoded, end)
        out.append(Finding(str(item.pii_type).lower(), start, end, "native"))
    return out


def pii_findings(text: str, *, use_native: bool = True, sanitizer: Any = None) -> List[Finding]:
    """Every PII / secret finding in ``text``, native and pure-Python, sorted
    by position (overlaps are kept; :func:`redact` merges them)."""
    found: List[Finding] = []
    lower = text.lower()
    native = sanitizer if sanitizer is not None else (native_sanitizer() if use_native else None)
    if native is not None:
        found += native_findings(text, native)
    else:
        found += _fallback(text, lower)
    for detector in PYTHON_DETECTORS:
        found += detector(text, lower)
    found.sort(key=lambda f: (f.start, -f.end))
    return found


#: Marker names; the native kinds keep the native Sanitizer's markers.
LABELS = {
    "secret_assignment": "SECRET", "authorization": "CREDENTIAL", "private_key": "PRIVATE_KEY",
    "phone_intl": "PHONE", "phone_national": "PHONE", "creditcard": "CREDIT_CARD",
    "ipaddress": "IP", "ip_address": "IP", "apikey": "API_KEY",
}


def label(kind: str) -> str:
    return LABELS.get(kind, kind.upper())


def redact(text: str, findings: List[Finding]) -> str:
    """Replace each finding's span with ``[REDACTED_<LABEL>]``; overlapping
    spans are merged (the first one's label is kept)."""
    spans = sorted((f for f in findings if f.redactable), key=lambda f: (f.start, -f.end))
    merged: List[List[Any]] = []
    for f in spans:
        if merged and f.start < merged[-1][1]:
            if f.end > merged[-1][1]:
                merged[-1][1] = f.end
            continue
        merged.append([f.start, f.end, f.kind])
    pieces = []
    last = 0
    for start, end, kind in merged:
        pieces.append(text[last:start])
        pieces.append(f"[REDACTED_{label(kind)}]")
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


# -- prompt injection --------------------------------------------------------------------

INJECTION_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    ("ignore_instructions", re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
        r"\b(?:previous|prior|above|earlier|preceding|all|any|your|the|system|initial|original)\b"
        r"[^.\n]{0,20}?\b(?:instructions?|prompts?|directions?|rules|guidelines|directives)\b")),
    ("new_instructions", re.compile(
        r"(?im)(?:^[ \t>#*-]*|\b(?:the|your|these)\s+)(?:new|updated|revised|real|actual|true|hidden)"
        r"\s+(?:system\s+)?(?:instructions?|directives?)\s*:"
        r"|\bnew\s+system\s+(?:instructions?|prompt)\b")),
    ("role_override", re.compile(
        r"(?i)\byou\s+are\s+(?:now|no\s+longer)\s+(?:an?\s+)?(?:DAN|jailbroken|unrestricted|"
        r"unfiltered|uncensored|in\s+developer\s+mode|free\s+of|bound\s+by|an?\s+AI\s+without)"
        r"|\bdeveloper\s+mode\s+(?:enabled|activated|on)\b"
        r"|\bact\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|jailbroken|uncensored)\b")),
    ("chat_template_tokens", re.compile(
        r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|<\|assistant\|>|<\|user\|>|\[/?INST\]|"
        r"<</?SYS>>|<\|start_header_id\|>|<\|eot_id\|>|<start_of_turn>|<end_of_turn>")),
    ("prompt_leak", re.compile(
        r"(?i)\b(?:reveal|print|show|repeat|output|display|leak|dump|tell\s+me)\b[^.\n]{0,30}?"
        r"\b(?:system\s+prompt|hidden\s+(?:instructions?|prompt)|initial\s+(?:instructions?|"
        r"prompt)|your\s+(?:system\s+)?(?:instructions|prompt))\b")),
    ("conceal_from_user", re.compile(
        r"(?i)\b(?:do\s+not|don't|never)\s+(?:tell|inform|alert|warn|notify)\s+the\s+user\b")),
    ("exfiltration_link", re.compile(
        r"!\[[^\]\n]{0,100}\]\(\s*https?://[^)\s]+\?[^)\s]*"
        r"(?:\{|%7[Bb]|data=|q=|secret|token|key=|conversation|history|chat=)[^)]*\)")),
    ("exfiltration_request", re.compile(
        r"(?i)\b(?:send|post|upload|forward|transmit|exfiltrate|e-?mail)\b[^.\n]{0,40}?"
        r"\b(?:conversation|chat\s+history|credentials|api\s+keys?|passwords?|secrets?|"
        r"access\s+tokens?|user\s+data|personal\s+data)\b[^.\n]{0,40}?\b(?:to|at)\s+"
        r"(?:https?://|[\w.+-]+@[\w-]+\.|[\w-]+\.(?:com|net|org|io|ru|xyz)\b)")),
)


#: Substrings (lower case) one of which must occur for a pattern to be able
#: to match; checked first because ``in`` is far cheaper than a regex scan.
_INJECTION_HINTS = {
    "ignore_instructions": ("instruction", "prompt", "direction", "rule", "guideline", "directive"),
    "new_instructions": ("instruction", "directive", "new system"),
    "role_override": ("you are now", "you are no longer", "developer mode", "act as"),
    "chat_template_tokens": ("<|", "[inst]", "[/inst]", "<<sys>>", "<</sys>>", "_of_turn>"),
    "prompt_leak": ("system prompt", "hidden", "initial", "your instructions", "your prompt",
                    "your system"),
    "conceal_from_user": ("the user",),
    "exfiltration_link": ("![",),
    "exfiltration_request": ("conversation", "chat history", "credential", "api key", "password",
                             "secret", "token", "user data", "personal data"),
}


def injection_findings(text: str) -> List[Finding]:
    out = []
    lower = text.lower()
    for name, pattern in INJECTION_PATTERNS:
        if not any(h in lower for h in _INJECTION_HINTS.get(name, ("",))):
            continue
        for match in pattern.finditer(text):
            out.append(Finding(name, match.start(), match.end(), "heuristic"))
    return out


__all__ = [
    "Finding", "INJECTION_PATTERNS", "PYTHON_DETECTORS", "injection_findings",
    "native_sanitizer", "pii_findings", "redact",
]
