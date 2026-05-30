#!/usr/bin/env python3
"""Manifest-driven version reconciliation for all publishable surfaces."""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from collections.abc import Iterable
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]

SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclasses.dataclass(frozen=True)
class Target:
    id: str
    path: Path
    pattern: str
    replacement: str
    canonical: bool = False

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def manifest_path() -> Path:
    return repo_root() / "scripts" / "version_targets.toml"


def load_targets(path: Path) -> list[Target]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    target_rows = raw.get("targets")
    if not isinstance(target_rows, list) or not target_rows:
        raise RuntimeError(f"No [[targets]] entries found in {path}")

    targets: list[Target] = []
    for row in target_rows:
        if not isinstance(row, dict):
            raise RuntimeError("Each target entry must be a table")

        try:
            target_id = str(row["id"])
            rel_path = str(row["path"])
            pattern = str(row["pattern"])
            replacement = str(row["replacement"])
            canonical = bool(row.get("canonical", False))
        except KeyError as exc:
            raise RuntimeError(f"Missing required target key: {exc}") from exc

        abs_path = repo_root() / rel_path
        if not abs_path.exists():
            raise RuntimeError(f"Target file not found: {abs_path}")

        targets.append(
            Target(
                id=target_id,
                path=abs_path,
                pattern=pattern,
                replacement=replacement,
                canonical=canonical,
            )
        )

    canonical_count = sum(1 for target in targets if target.canonical)
    if canonical_count != 1:
        raise RuntimeError(
            "Exactly one target must set canonical = true "
            f"(found {canonical_count})"
        )

    return targets


def unique_paths(targets: Iterable[Target]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for target in targets:
        if target.path not in seen:
            seen.add(target.path)
            ordered.append(target.path)
    return ordered


def extract_match(target: Target) -> tuple[str, re.Match[str]]:
    text = target.path.read_text(encoding="utf-8")
    matches = list(target.regex.finditer(text))
    if len(matches) != 1:
        rel = target.path.relative_to(repo_root())
        raise RuntimeError(
            f"Target {target.id} expected exactly 1 match in {rel}, found {len(matches)}"
        )

    match = matches[0]
    if "version" not in match.groupdict():
        raise RuntimeError(
            f"Target {target.id} pattern must define a named group 'version'"
        )

    return text, match


def read_versions(targets: Iterable[Target]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for target in targets:
        _, match = extract_match(target)
        versions[target.id] = match.group("version")
    return versions


def canonical_target(targets: Iterable[Target]) -> Target:
    for target in targets:
        if target.canonical:
            return target
    raise RuntimeError("No canonical target found")


def canonical_version(targets: Iterable[Target]) -> str:
    source = canonical_target(targets)
    _, match = extract_match(source)
    return match.group("version")


def validate_version(version: str) -> None:
    if not SEMVER_PATTERN.fullmatch(version):
        raise RuntimeError(
            f"Invalid version '{version}'. Expected semantic version (e.g. 2.1.30)."
        )


def format_target_path(path: Path) -> str:
    return str(path.relative_to(repo_root()))


def check_versions(targets: list[Target], expected: str | None) -> int:
    if expected is None:
        expected = canonical_version(targets)
    else:
        validate_version(expected)

    mismatches: list[tuple[Target, str]] = []
    for target in targets:
        _, match = extract_match(target)
        found = match.group("version")
        if found != expected:
            mismatches.append((target, found))

    if mismatches:
        print(f"Version mismatch detected (expected {expected}):")
        for target, found in mismatches:
            print(
                f" - {target.id}: {format_target_path(target.path)} "
                f"(found {found})"
            )
        return 1

    print(f"All version targets are consistent at {expected}")
    return 0


def render_replacement(match: re.Match[str], replacement: str, version: str) -> str:
    data = match.groupdict()
    data["version"] = version
    return replacement.format(**data)


def set_versions(targets: list[Target], version: str, dry_run: bool = False) -> int:
    validate_version(version)

    updated: list[tuple[Target, str, str]] = []
    for target in targets:
        text, match = extract_match(target)
        current = match.group("version")
        if current == version:
            continue

        def repl(found: re.Match[str]) -> str:
            return render_replacement(found, target.replacement, version)

        new_text, count = target.regex.subn(repl, text, count=1)
        if count != 1:
            rel = format_target_path(target.path)
            raise RuntimeError(
                f"Failed to update target {target.id} in {rel}: expected 1 replacement, got {count}"
            )

        if not dry_run:
            target.path.write_text(new_text, encoding="utf-8")

        updated.append((target, current, version))

    if updated:
        prefix = "[dry-run] " if dry_run else ""
        print(f"{prefix}Updated {len(updated)} target(s):")
        for target, before, after in updated:
            print(
                f" - {target.id}: {format_target_path(target.path)} "
                f"({before} -> {after})"
            )
    else:
        print(f"No updates needed; all targets already at {version}")

    return 0


def bump_semver(version: str, bump: str) -> str:
    if not re.fullmatch(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        raise RuntimeError(
            f"Cannot {bump}-bump non-stable semantic version '{version}'"
        )

    major_str, minor_str, patch_str = version.split(".")
    major = int(major_str)
    minor = int(minor_str)
    patch = int(patch_str)

    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor += 1
        patch = 0
    elif bump == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise RuntimeError(f"Unsupported bump part: {bump}")

    return f"{major}.{minor}.{patch}"


def cmd_show(targets: list[Target], canonical_only: bool) -> int:
    if canonical_only:
        print(canonical_version(targets))
        return 0

    versions = read_versions(targets)
    source = canonical_target(targets)
    print(f"Canonical target: {source.id} ({format_target_path(source.path)})")
    print(f"Canonical version: {versions[source.id]}")
    for target in targets:
        marker = "*" if target.canonical else " "
        print(
            f"{marker} {target.id:34s} "
            f"{versions[target.id]:12s} "
            f"{format_target_path(target.path)}"
        )
    return 0


def cmd_paths(targets: list[Target]) -> int:
    for path in unique_paths(targets):
        print(format_target_path(path))
    return 0


def cmd_next(targets: list[Target], bump: str) -> int:
    current = canonical_version(targets)
    print(bump_semver(current, bump))
    return 0




def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize version strings across all package surfaces."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Validate all targets are version-consistent.")
    check.add_argument(
        "--version",
        help="Expected version. If omitted, uses canonical source from the manifest.",
    )

    set_cmd = sub.add_parser("set", help="Update all targets to the requested version.")
    set_cmd.add_argument("--version", required=True, help="Version to write.")
    set_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )

    show = sub.add_parser("show", help="Print current versions for all targets.")
    show.add_argument(
        "--canonical-only",
        action="store_true",
        help="Print only the canonical version value.",
    )

    sub.add_parser("paths", help="Print unique target paths (one per line).")

    next_cmd = sub.add_parser(
        "next", help="Compute next semantic version from canonical target."
    )
    next_cmd.add_argument(
        "--bump",
        choices=["patch", "minor", "major"],
        default="patch",
        help="Semantic version part to bump (default: patch).",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    targets = load_targets(manifest_path())

    if args.command == "check":
        return check_versions(targets, args.version)
    if args.command == "set":
        result = set_versions(targets, args.version, dry_run=args.dry_run)
        if args.dry_run:
            return result
        return check_versions(targets, args.version)
    if args.command == "show":
        return cmd_show(targets, canonical_only=args.canonical_only)
    if args.command == "paths":
        return cmd_paths(targets)
    if args.command == "next":
        return cmd_next(targets, bump=args.bump)

    raise RuntimeError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
