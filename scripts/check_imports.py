#!/usr/bin/env python3
"""Import-smoke test for the built wheel.

Run against an *installed* briefcase-ai wheel (NOT the source tree, and NOT under
the test suite, which mocks ``briefcase._native``). Every module in REQUIRED must
import from a bare install; this is what catches native-binding registration
regressions such as a missing ``add_class`` in ``bindings/python/src/lib.rs``.

OPTIONAL modules are gated behind pip extras (opentelemetry, lakefs, pyiceberg);
a missing-dependency ImportError there is treated as a skip, anything else fails.

    python scripts/check_imports.py
"""

from __future__ import annotations

import importlib
import sys

# Must import from a bare `pip install briefcase-ai` with no extras.
REQUIRED = [
    "briefcase",
    "briefcase.cost",
    "briefcase.drift",
    "briefcase.sanitize",
    "briefcase.storage",
    "briefcase.replay",
    "briefcase.decorators",
    "briefcase.config",
    "briefcase.hardware",
    "briefcase.auto",
    "briefcase.bitemporal",
    "briefcase.compliance",
    "briefcase.routing",
    "briefcase.validation",
    "briefcase.validate",
    "briefcase.rag",
    "briefcase.guardrails",
    "briefcase.events",
    "briefcase.correlation",
    "briefcase.external",
    "briefcase.external_data",
    "briefcase.exporters",
    "briefcase.semantic_conventions",
    "briefcase.integrations.evals",
    "briefcase.controls",
    "briefcase.controls.providers",
    "briefcase.integrations.frameworks",
    "briefcase.integrations.frameworks.pageindex_mcp",
    "briefcase.guardrails.envs",
    "briefcase.guardrails.envs.rbac",
    "briefcase.guardrails.envs.abac",
    "briefcase.routing.internal",
    "briefcase.events.webhook",
]

# Gated behind pip extras; skip cleanly when the extra is not installed.
# kdb / iceberg_glue / signed_bundle import lazily, so the module import
# itself must succeed on a bare install; they are listed here anyway so a
# packaging regression that drops the files from the wheel is caught.
OPTIONAL = [
    "briefcase.otel",
    "briefcase.integrations.lakefs",
    "briefcase.integrations.gym",
    "briefcase.bitemporal.backends.iceberg",
    "briefcase.bitemporal.backends.iceberg_glue",
    "briefcase.bitemporal.backends.kdb",
    "briefcase.compliance.signed_bundle",
    "briefcase.mcp",
    "briefcase.integrations.lakefs.branches",
    "briefcase.integrations.lakefs.lineage",
    "briefcase.integrations.lakefs.staged",
    "briefcase.routing.opa",
    "briefcase.exporters.otel",
    "briefcase.exporters.gcp_logging",
    "briefcase.events.kafka",
    "briefcase.rag.vector_stores",
    "briefcase.integrations.vcs",
    "briefcase.integrations.frameworks.langchain_handler",
    "briefcase.integrations.frameworks.llamaindex_handler",
    "briefcase.integrations.frameworks.crewai_handler",
    "briefcase.integrations.frameworks.ag2_handler",
    "briefcase.integrations.frameworks.autogen_handler",
    "briefcase.integrations.frameworks.openai_agents_handler",
    "briefcase.integrations.frameworks.pageindex_handler",
]


def _is_missing_extra(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "requires the" in msg and "extra" in msg or isinstance(exc, ModuleNotFoundError)


def main() -> int:
    failures: list[str] = []

    for name in REQUIRED:
        try:
            importlib.import_module(name)
            print(f"  OK       {name}")
        except Exception as exc:  # noqa: BLE001 - report everything
            print(f"  FAIL     {name}: {type(exc).__name__}: {exc}")
            failures.append(name)

    for name in OPTIONAL:
        try:
            importlib.import_module(name)
            print(f"  OK       {name}")
        except Exception as exc:  # noqa: BLE001
            if _is_missing_extra(exc):
                print(f"  SKIP     {name} (optional extra not installed)")
            else:
                print(f"  FAIL     {name}: {type(exc).__name__}: {exc}")
                failures.append(name)

    # Package data for `briefcase stack` / `briefcase doctor` must ship in the wheel (maturin only
    # bundles non-.py files listed in `[tool.maturin] include`). A missing entry silently drops them,
    # so assert they resolve from the installed package.
    for resource in ("docker-compose.yml", "compat.json"):
        try:
            from importlib.resources import files

            data = (files("briefcase.cli.stack") / resource).read_text("utf-8")
            assert data.strip(), f"{resource} is empty"
            print(f"  OK       briefcase/cli/stack/{resource} (package data)")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL     briefcase/cli/stack/{resource}: {type(exc).__name__}: {exc}")
            failures.append(f"stack/{resource}")

    if failures:
        print(f"\nFAILED: {len(failures)} module(s) did not import: {', '.join(failures)}")
        return 1
    print("\nAll required public submodules import cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
