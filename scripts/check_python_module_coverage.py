#!/usr/bin/env python3
"""Enforce per-module Python coverage thresholds from a coverage.py JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail if any module under a prefix is below a coverage threshold."
    )
    parser.add_argument(
        "--coverage-json",
        required=True,
        help="Path to coverage.py JSON output (from --cov-report=json:...).",
    )
    parser.add_argument(
        "--module-prefix",
        default="briefcase/",
        help="Only evaluate files whose path starts with this prefix.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=90.0,
        help="Minimum percent covered required for every matching module.",
    )
    parser.add_argument(
        "--exclude-init",
        action="store_true",
        help="Exclude __init__.py modules from threshold checks.",
    )
    return parser.parse_args()


def _load_report(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Coverage report not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _collect_under_threshold(
    report: dict, module_prefix: str, threshold: float, exclude_init: bool
) -> tuple[list[tuple[str, float]], int]:
    files = report.get("files", {})
    total_modules = 0
    under: list[tuple[str, float]] = []

    for module_path in sorted(files):
        if not module_path.startswith(module_prefix):
            continue
        if exclude_init and module_path.endswith("/__init__.py"):
            continue

        total_modules += 1
        summary = files[module_path].get("summary", {})
        covered = float(summary.get("percent_covered", 0.0))
        if covered < threshold:
            under.append((module_path, covered))

    return under, total_modules


def main() -> int:
    args = _parse_args()
    report = _load_report(Path(args.coverage_json))
    under, total = _collect_under_threshold(
        report,
        module_prefix=args.module_prefix,
        threshold=args.threshold,
        exclude_init=args.exclude_init,
    )

    print(
        f"Checked {total} modules under '{args.module_prefix}' "
        f"with threshold >= {args.threshold:.1f}%."
    )

    if not under:
        print("All modules passed coverage threshold.")
        return 0

    print("Modules below threshold:")
    for module_path, covered in under:
        print(f"- {module_path}: {covered:.2f}%")
    return 1


if __name__ == "__main__":
    sys.exit(main())
