#!/usr/bin/env python3
"""Fold ``changelog.d/*.md`` fragments into ``CHANGELOG.md``'s Unreleased section.

Contributors add one fragment per change under ``changelog.d/`` (see its README)
instead of editing ``CHANGELOG.md`` directly, so parallel branches never
conflict. At release time this script merges them:

    python scripts/changelog_fold.py --dry-run          # show the result, change nothing
    python scripts/changelog_fold.py                    # fold and delete the fragments
    python scripts/changelog_fold.py --check            # exit 1 if fragments are pending
    python scripts/changelog_fold.py --release 4.5.0    # fold, then cut the release section

Fragment format: Keep a Changelog headings (``### Added`` ...) and ``- `` bullets.
``### Breaking`` (or ``Breaking changes``) is accepted and becomes ``Changed`` with
a ``**Breaking:**`` prefix on each bullet. Any other heading is an error (exit 2,
nothing is changed). Breaking bullets sort first in their subsection.

A fragment can retire a bullet that already sits in Unreleased (for example one describing an
interim behaviour that this release replaces) with a line ``Supersedes: "text"`` anywhere in the
fragment: the Unreleased bullet whose whole text equals it (whitespace-insensitive) is removed, and it is an
error (exit 2) if none does or the selector is empty. Bullets from other fragments are never removed.

Identical bullets are dropped. Near-duplicates (the same feature described twice)
are never merged; they are reported on stderr for a human to resolve, or abort
the run with ``--strict``.

Exit codes: 0 ok; 1 ``--check`` found fragments, ``--strict`` found
near-duplicates, or ``--release`` had nothing to release; 2 usage error or
unparsable input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

CANONICAL = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
# Heading (lower-cased) -> (canonical section, mark bullets breaking?)
HEADINGS: Dict[str, Tuple[str, bool]] = {s.lower(): (s, False) for s in CANONICAL}
HEADINGS.update({"breaking": ("Changed", True), "breaking changes": ("Changed", True)})

WIDTH = 80
UNRELEASED_RE = re.compile(r"^## \[Unreleased\]\s*$")
VERSION_HEAD_RE = re.compile(r"^## \[")
SUPERSEDES_RE = re.compile(r'^Supersedes:\s*"(?P<text>.*)"\s*$')
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
BREAKING_RE = re.compile(r"^\*\*Breaking\b", re.IGNORECASE)
CODE_RE = re.compile(r"`([^`]+)`")
WORD_RE = re.compile(r"[a-z][a-z0-9_]{3,}")
STOP = frozenset("""with that this from when have been into than then they their there which while
    also only each other more most such will would should could does some still used uses using
    over after before both about like same without within unless instead where every""".split())


class FoldError(Exception):
    """Unparsable input or a refused operation: reported on stderr, nothing written."""


class NothingToRelease(FoldError):
    pass


class Bullet:
    def __init__(self, lines: List[str], source: str, fresh: bool):
        self.lines = lines  # raw lines, the first starts with "- "
        self.source = source
        self.fresh = fresh  # came from a fragment, so it is re-wrapped on output
        self.nested = any(re.match(r"^\s+([-*]|\d+\.)\s", ln) for ln in lines[1:])

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.lines).split())[2:]  # without the "- "

    @property
    def breaking(self) -> bool:
        return bool(BREAKING_RE.match(self.text))

    def render(self) -> List[str]:
        if not self.fresh or self.nested:
            return list(self.lines)
        return textwrap.wrap("- " + self.text, width=WIDTH, subsequent_indent="  ",
                             break_long_words=False, break_on_hyphens=False)


Sections = Dict[str, List[Bullet]]


def parse_sections(lines: Sequence[str], source: str, fresh: bool) -> Tuple[List[str], Sections]:
    """Return (text before the first heading, {canonical section: bullets})."""
    preamble: List[str] = []
    out: Sections = {}
    current: Optional[str] = None
    mark = False
    bullet: Optional[List[str]] = None

    def flush() -> None:
        nonlocal bullet
        if bullet is not None and current is not None:
            b = Bullet(bullet, source, fresh)
            if mark and not b.breaking:
                b = Bullet(["- **Breaking:** " + b.lines[0][2:]] + b.lines[1:], source, fresh)
            out.setdefault(current, []).append(b)
        bullet = None

    for n, line in enumerate(lines, 1):
        m = re.match(r"^#{2,4}\s+(.*?)\s*$", line)
        if m:
            flush()
            name = m.group(1).strip()
            if name.lower() not in HEADINGS:
                raise FoldError(f"{source}:{n}: cannot map heading {name!r} "
                                f"(known: {', '.join(CANONICAL)}, Breaking)")
            current, mark = HEADINGS[name.lower()]
            continue
        if current is None:
            if line.strip() and fresh:
                raise FoldError(f"{source}:{n}: text before the first heading: {line.strip()[:60]!r}")
            if line.strip() or preamble:
                preamble.append(line)
            continue
        if re.match(r"^[-*]\s", line):
            flush()
            bullet = ["- " + line[2:].rstrip()]
        elif not line.strip():
            continue  # blank lines never survive; a bullet ends at the next "- " or heading
        elif bullet is not None:
            bullet.append(line.rstrip())
        else:
            raise FoldError(f"{source}:{n}: text that is not a bullet: {line.strip()[:60]!r}")
    flush()
    while preamble and not preamble[-1].strip():
        preamble.pop()
    return preamble, out


def norm(text: str) -> str:
    return " ".join(text.split())


def signature(text: str) -> Tuple[frozenset, frozenset]:
    spans = frozenset(norm(s).lower() for s in CODE_RE.findall(text))
    words = frozenset(w for w in WORD_RE.findall(CODE_RE.sub(" ", text).lower()) if w not in STOP)
    return spans, words


def near_duplicate(a: Bullet, b: Bullet) -> bool:
    """Heuristic: the same identifiers, or mostly the same vocabulary, in two bullets."""
    sa, wa = signature(a.text)
    sb, wb = signature(b.text)
    shared = len(sa & sb)
    if shared >= 2 and shared / min(len(sa), len(sb)) >= 0.6:
        return True
    ta, tb = sa | wa, sb | wb
    return min(len(ta), len(tb)) >= 8 and len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def merge(existing: Sections, fragments: List[Tuple[str, Sections]]
          ) -> Tuple[Sections, List[str], List[Tuple[Bullet, str, Bullet, str]]]:
    """Merge fragments into existing: (merged, dropped-identical notes, near-duplicate pairs)."""
    merged: Sections = {k: list(v) for k, v in existing.items()}
    dropped: List[str] = []
    for source, secs in fragments:
        for name, bullets in secs.items():
            have = {norm(b.text) for b in merged.get(name, [])}
            for b in bullets:
                key = norm(b.text)
                if key in have:
                    dropped.append(f"{source}: identical to a bullet already under {name}: {b.text[:70]}")
                    continue
                merged.setdefault(name, []).append(b)
                have.add(key)
    flat = [(name, b) for name in CANONICAL for b in merged.get(name, [])]
    pairs = [(a, na, b, nb) for i, (na, a) in enumerate(flat) for nb, b in flat[i + 1:]
             if (a.fresh or b.fresh) and a.source != b.source and near_duplicate(a, b)]
    for name in merged:  # stable: breaking first
        merged[name] = [b for b in merged[name] if b.breaking] + [b for b in merged[name] if not b.breaking]
    return merged, dropped, pairs


def render_unreleased(preamble: List[str], secs: Sections) -> List[str]:
    out = ["## [Unreleased]", ""]
    if preamble:
        out += preamble + [""]
    for name in CANONICAL:
        if secs.get(name):
            out.append(f"### {name}")
            for b in secs[name]:
                out += b.render()
            out.append("")
    return out


def locate_unreleased(lines: List[str]) -> Tuple[int, int]:
    start = next((i for i, ln in enumerate(lines) if UNRELEASED_RE.match(ln)), None)
    if start is None:
        raise FoldError("CHANGELOG.md has no '## [Unreleased]' section")
    end = next((i for i in range(start + 1, len(lines)) if VERSION_HEAD_RE.match(lines[i])), len(lines))
    return start, end


def fragment_files(frag_dir: Path) -> List[Path]:
    if not frag_dir.is_dir():
        return []
    return sorted(p for p in frag_dir.glob("*.md") if p.name.lower() != "readme.md")


LINK_RE = re.compile(
    r"^\[Unreleased\]:\s*(?P<base>\S+?)/compare/(?P<tag>v?)(?P<prev>[^\s.]\S*?)\.\.\.(?P<head>\S+)\s*$")


def update_links(lines: List[str], version: str) -> List[str]:
    """Roll the compare links forward, if the file keeps them."""
    for i, ln in enumerate(lines):
        m = LINK_RE.match(ln)
        if m:
            base, tag, prev = m["base"], m["tag"], m["prev"]
            return lines[:i] + [f"[Unreleased]: {base}/compare/{tag}{version}...{m['head']}",
                                f"[{version}]: {base}/compare/{tag}{prev}...{tag}{version}"] + lines[i + 1:]
    return lines


def build(root: Path, release: Optional[str], date: str, strict: bool):
    """Compute the new text: (text or None if unchanged, fragments, notes, warnings)."""
    frags = fragment_files(root / "changelog.d")
    original = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = original.split("\n")
    start, end = locate_unreleased(lines)
    notes: List[str] = []
    warnings: List[str] = []

    if frags:
        preamble, existing = parse_sections(lines[start + 1:end], "CHANGELOG.md [Unreleased]", fresh=False)
        parsed = []
        for p in frags:
            raw = p.read_text(encoding="utf-8").split("\n")
            directives = [m["text"] for m in map(SUPERSEDES_RE.match, raw) if m]
            body = [ln for ln in raw if not SUPERSEDES_RE.match(ln)]
            _, secs = parse_sections(body, f"changelog.d/{p.name}", True)
            parsed.append((f"changelog.d/{p.name}", secs))
            for text in directives:
                wanted = " ".join(text.split())
                if not wanted:
                    raise FoldError(f"changelog.d/{p.name}: Supersedes: needs the text of the bullet it replaces "
                                    "(the selector is empty)")
                removed = 0
                for name in existing:
                    kept = [b for b in existing[name] if wanted != b.text]
                    removed += len(existing[name]) - len(kept)
                    existing[name] = kept
                if not removed:
                    raise FoldError(f"changelog.d/{p.name}: Supersedes: {text!r} matches no bullet in Unreleased")
                notes.append(f"superseded {removed} Unreleased bullet(s) matching {text!r}")
        merged, dropped, pairs = merge(existing, parsed)
        notes += [f"dropped duplicate: {d}" for d in dropped]
        for a, na, b, nb in pairs:
            warnings.append(f"possible near-duplicate:\n    [{na}] ({a.source}) {a.text[:110]}\n"
                            f"    [{nb}] ({b.source}) {b.text[:110]}")
        if strict and pairs:
            raise FoldError("near-duplicates found and --strict given:\n" + "\n".join(warnings))
        lines = lines[:start] + render_unreleased(preamble, merged) + lines[end:]
        start, end = locate_unreleased(lines)

    if release:
        section = lines[start + 1:end]
        if not any(ln.strip() for ln in section):
            raise NothingToRelease("[Unreleased] is empty; nothing to release")
        if any(ln.startswith(f"## [{release}]") for ln in lines):
            raise FoldError(f"CHANGELOG.md already has a section for {release}")
        while section and not section[-1].strip():
            section.pop()
        lines = (lines[:start] + ["## [Unreleased]", "", f"## [{release}] - {date}"] + section + [""]
                 + lines[end:])
        lines = update_links(lines, release)

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return (text if text != original else None), frags, notes, warnings


def unreleased_text(text: str) -> str:
    lines = text.split("\n")
    s, e = locate_unreleased(lines)
    return "\n".join(lines[s:e]).rstrip() + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fold changelog.d fragments into CHANGELOG.md.")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="repository root (default: the parent of scripts/)")
    ap.add_argument("--check", action="store_true", help="exit 1 if any fragment is present")
    ap.add_argument("--dry-run", action="store_true", help="print the result; change nothing")
    ap.add_argument("--strict", action="store_true", help="abort when near-duplicates are found")
    ap.add_argument("--release", metavar="X.Y.Z", help="after folding, cut the release section")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="release date (default: today)")
    args = ap.parse_args(argv)
    root: Path = args.root

    if args.date and not args.release:
        ap.error("--date needs --release")
    if args.release and not SEMVER_RE.match(args.release):
        ap.error(f"--release {args.release!r} is not X.Y.Z")
    if args.date:
        try:
            dt.date.fromisoformat(args.date)
        except ValueError:
            ap.error(f"--date {args.date!r} is not YYYY-MM-DD")
    date = args.date or dt.date.today().isoformat()

    if args.check:
        pending = fragment_files(root / "changelog.d")
        if pending:
            print("changelog fragments not yet folded into CHANGELOG.md:", file=sys.stderr)
            for p in pending:
                print(f"  changelog.d/{p.name}", file=sys.stderr)
            return 1
        print("no pending changelog fragments")
        return 0

    try:
        text, frags, notes, warnings = build(root, args.release, date, args.strict)
    except NothingToRelease as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for line in notes + warnings:
        print(line, file=sys.stderr)
    if warnings:
        print(f"{len(warnings)} possible near-duplicate pair(s): resolve by hand in CHANGELOG.md",
              file=sys.stderr)

    if args.dry_run:
        current = text if text is not None else (root / "CHANGELOG.md").read_text(encoding="utf-8")
        print(unreleased_text(current))
        print("fragments that would be deleted:" if frags else "no fragments to fold")
        for p in frags:
            print(f"  changelog.d/{p.name}")
        return 0

    if text is not None:
        (root / "CHANGELOG.md").write_text(text, encoding="utf-8")
    for p in frags:
        p.unlink()
    print(f"folded {len(frags)} fragment(s)" + (f"; released {args.release}" if args.release else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
