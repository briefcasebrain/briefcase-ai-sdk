"""Versioned evaluation datasets built from captured decision records.

Turn the JSONL that ``briefcase.observe("runs.jsonl")`` writes into an
immutable, content-addressed dataset version, then score a function against
it (:mod:`briefcase.datasets.evaluate`). Everything is local files under
``$BRIEFCASE_HOME/datasets`` (default ``~/.briefcase/datasets``); no server.

    from briefcase.datasets import DatasetStore, promote, read_records

    result = promote(read_records("runs.jsonl"), source_name="runs.jsonl",
                     where=["decision_type=triage"], allow_unsanitized=True)
    version = DatasetStore().save("triage", result.cases, result.metadata)

The CLI is ``briefcase dataset from-records|list|show|diff|export|import``
and ``briefcase eval run``.
"""

from briefcase.datasets.core import (
    DATASET_SCHEMA_VERSION,
    DatasetError,
    DatasetStore,
    DatasetVersion,
    PromoteResult,
    case_content_hash,
    diff_versions,
    native_sanitizer,
    parse_cases_jsonl,
    parse_ref,
    promote,
    read_records,
)
from briefcase.datasets.formats import EXPORT_FORMATS, export_inspect, export_jsonl, export_lm_eval

__all__ = [
    "DATASET_SCHEMA_VERSION",
    "DatasetError",
    "DatasetStore",
    "DatasetVersion",
    "EXPORT_FORMATS",
    "PromoteResult",
    "case_content_hash",
    "diff_versions",
    "export_inspect",
    "export_jsonl",
    "export_lm_eval",
    "native_sanitizer",
    "parse_cases_jsonl",
    "parse_ref",
    "promote",
    "read_records",
]
