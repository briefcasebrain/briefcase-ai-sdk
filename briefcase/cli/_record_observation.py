"""Evidence retained for a single reported lookup in record-help."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class SourceCheck:
    """Scope a reader's stated evidence to one source instance and lookup target."""

    original_target: bool = False
    foreign_target: bool = False
    unchecked: bool = False
    ambiguous: bool = False
    correct_instance: Optional[bool] = None
    outcome: Optional[str] = None
    matching_rows: int = 0
    shared_identifier: bool = False
    unresolved_lookup: bool = False
    partial: bool = False
    pending: bool = False
    foreign_reader_only: bool = False
    unaccounted: bool = False
    returned_unbound: bool = False
    conflicting_results: bool = False
    unlinked_record: bool = False
    referenced_check: bool = False
    clarification_veto: bool = False
    anonymous_match: bool = False
    zero_matches: bool = False
    unresolved_detail: bool = False
    matched_objects: int = 0
    other_objects: int = 0
    latest_object: Optional[str] = None
    relocated: bool = False
    uncertain_origin: bool = False
    personal_gap: bool = False
    group_size: Optional[int] = None
    members: Dict[int, bool] = field(default_factory=dict)
    invalid_members: bool = False
    unresolved_population: bool = False
    pending_relation: Optional[str] = None

    def record_multiple_matches(self) -> None:
        """A new search declaration cannot erase observed matching rows."""
        self.matched_objects = max(self.matched_objects, 2)
        self.ambiguous = True

    def add_object(self, comparison):
        if self.pending_relation:
            if comparison in ("match", "miss") and comparison != self.pending_relation:
                self.conflicting_results = True
            comparison = self.pending_relation
            self.pending_relation = None
        self.latest_object = comparison
        self.matched_objects += comparison == "match"
        self.other_objects += comparison == "miss"

    def classify_member(self, index: int, relation: str, *, last_of_two: bool) -> None:
        if (self.group_size is None or index in self.members or index > self.group_size
                or (last_of_two and self.group_size != 2)
                or (relation == "same" and (not self.original_target or self.latest_object == "miss"))):
            self.invalid_members = True
            return
        matched = relation in ("original", "same")
        self.members[index] = matched
        self.add_object("match" if matched else "miss")
        self.outcome = "matched" if any(self.members.values()) else "no-match"

    def compare(self, source, result, action, facts):
        if self.foreign_target:
            return "confirmation_other_run"
        if result == "unverified":
            return None
        if self.relocated:
            return "confirmation_other_source"
        if self.uncertain_origin:
            return "confirmation_unresolved_lookup"
        if self.personal_gap and result == "no-match":
            return "confirmation_partial"
        if self.unchecked or self.pending or self.foreign_reader_only:
            return "confirmation_unchecked"
        if source == "jsonl" and ("unresolvedPath" in facts or ("pendingPath" in facts and not self.original_target)):
            return "confirmation_unresolved_path"
        if self.correct_instance is False:
            return "confirmation_other_file" if source == "jsonl" else "confirmation_other_source"
        if action == "ask_id" or "unresolvedOriginal" in facts or self.shared_identifier:
            return "ask_id"
        if self.unresolved_lookup or self.unresolved_detail or self.invalid_members or self.unresolved_population:
            return "confirmation_unresolved_lookup"
        if self.partial and result == "no-match":
            return "confirmation_partial"
        if self.group_size is not None:
            if len(self.members) != self.group_size:
                return "confirmation_unresolved_lookup"
            if sum(self.members.values()) > 1:
                return "confirmation_ambiguous"
        if self.conflicting_results or (self.zero_matches and (self.matched_objects or self.matching_rows)):
            return "confirmation_inconsistent"
        if self.ambiguous or self.matched_objects + self.matching_rows > 1:
            return "confirmation_ambiguous"
        # A nonmatching returned row does not erase another row that matched the same lookup.
        observed = self.outcome
        if self.other_objects and (self.matched_objects or self.matching_rows):
            observed = "matched"
        if observed and result != observed:
            return "confirmation_inconsistent"
        if self.unlinked_record and self.returned_unbound and self.original_target and not self.unaccounted:
            return "ask_evidence" if result == "matched" else "confirmation_unresolved_lookup"
        if self.unaccounted or self.returned_unbound:
            return "confirmation_unresolved_lookup"
        return None
