#!/usr/bin/env python3
"""Version bump helper backed by scripts/version_sync.py."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def info(message: str) -> None:
    print(f"{Colors.BLUE}{message}{Colors.NC}")


def success(message: str) -> None:
    print(f"{Colors.GREEN}{message}{Colors.NC}")


def warning(message: str) -> None:
    print(f"{Colors.YELLOW}{message}{Colors.NC}")


def fail(message: str) -> None:
    print(f"{Colors.RED}Error: {message}{Colors.NC}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], cwd: Path, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=True,
        text=True,
        capture_output=capture,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a version across all language surfaces and optionally commit/tag/push."
    )
    parser.add_argument("version", help="New semantic version, e.g. 2.1.31")
    parser.add_argument(
        "-c",
        "--commit",
        action="store_true",
        help="Create a commit for the version bump",
    )
    parser.add_argument(
        "-t",
        "--tag",
        action="store_true",
        help="Create an annotated tag (implies --commit)",
    )
    parser.add_argument(
        "-p",
        "--push",
        action="store_true",
        help="Push branch and tag to origin (implies --tag --commit)",
    )
    return parser.parse_args()


def require_clean_worktree(repo_root: Path) -> None:
    status = run(["git", "status", "--porcelain"], cwd=repo_root, capture=True).stdout
    if status.strip():
        warning("Uncommitted changes were detected:")
        print(status.rstrip())
        response = input("Continue anyway? (y/N) ").strip().lower()
        if response not in {"y", "yes"}:
            raise SystemExit(0)


def get_target_paths(repo_root: Path) -> list[str]:
    output = run(
        [sys.executable, "scripts/version_sync.py", "paths"],
        cwd=repo_root,
        capture=True,
    ).stdout
    paths = [line.strip() for line in output.splitlines() if line.strip()]
    if not paths:
        fail("No version targets were returned by scripts/version_sync.py paths")
    return paths


def ensure_tag_not_exists(repo_root: Path, tag: str) -> None:
    local = subprocess.run(
        ["git", "rev-parse", tag],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
    )
    if local.returncode == 0:
        fail(f"Tag already exists locally: {tag}")

    remote = subprocess.run(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
    )
    if remote.returncode == 0 and remote.stdout.strip():
        fail(f"Tag already exists on origin: {tag}")


def main() -> int:
    args = parse_args()

    if not SEMVER_PATTERN.fullmatch(args.version):
        fail(
            f"Invalid version '{args.version}'. "
            "Expected semantic version like 2.1.31."
        )

    do_commit = args.commit or args.tag or args.push
    do_tag = args.tag or args.push
    do_push = args.push

    repo_root = Path(__file__).resolve().parent.parent

    print()
    info("")
    info("   Briefcase AI Version Bump Script        ")
    info("")
    print()

    if do_commit:
        require_clean_worktree(repo_root)

    run([sys.executable, "scripts/version_sync.py", "show"], cwd=repo_root)
    print()
    warning(f"Applying version: {args.version}")
    run(
        [sys.executable, "scripts/version_sync.py", "set", "--version", args.version],
        cwd=repo_root,
    )
    run(
        [sys.executable, "scripts/version_sync.py", "check", "--version", args.version],
        cwd=repo_root,
    )
    print()

    if do_commit:
        target_paths = get_target_paths(repo_root)
        run(["git", "add", *target_paths], cwd=repo_root)
        run(
            [
                "git",
                "commit",
                "-m",
                f"chore(release): reconcile version to {args.version}",
            ],
            cwd=repo_root,
        )
        success("Created commit")
        print()

    if do_tag:
        tag_name = f"v{args.version}"
        ensure_tag_not_exists(repo_root, tag_name)
        run(
            [
                "git",
                "tag",
                "-a",
                tag_name,
                "-m",
                f"Release {args.version}",
            ],
            cwd=repo_root,
        )
        success(f"Created tag {tag_name}")
        print()

    if do_push:
        branch = run(
            ["git", "branch", "--show-current"],
            cwd=repo_root,
            capture=True,
        ).stdout.strip()
        if not branch:
            fail("Unable to determine current branch")
        run(["git", "push", "origin", branch], cwd=repo_root)
        run(["git", "push", "origin", f"v{args.version}"], cwd=repo_root)
        success("Pushed branch and tag")
        print()

    success("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
