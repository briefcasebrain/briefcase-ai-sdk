"""
Semantic conventions for bitemporal evidence records.

Attribute keys follow the existing ``briefcase.*`` namespace so they can
be attached to OpenTelemetry spans alongside the external-data conventions
without collision.
"""

# Record identity
BITEMPORAL_RECORD_ID = "briefcase.bitemporal.record_id"
BITEMPORAL_KEY = "briefcase.bitemporal.key"

# Time axes — the two fields that distinguish bitemporal from single-time
BITEMPORAL_VALID_TIME = "briefcase.bitemporal.valid_time"
BITEMPORAL_TRANSACTION_TIME = "briefcase.bitemporal.transaction_time"

# Attribution
BITEMPORAL_SOURCE = "briefcase.bitemporal.source"
BITEMPORAL_SOURCE_TRUST_LEVEL = "briefcase.bitemporal.source_trust_level"

# Correction lineage
BITEMPORAL_PARENT_RECORD_ID = "briefcase.bitemporal.parent_record_id"
BITEMPORAL_IS_CORRECTION = "briefcase.bitemporal.is_correction"

# Decision binding — ties the evidence to the action taken on it
BITEMPORAL_DECISION_ID = "briefcase.bitemporal.decision_id"

# As-of replay
BITEMPORAL_ASOF_TRANSACTION_TIME = "briefcase.bitemporal.asof.transaction_time"
BITEMPORAL_ASOF_VALID_TIME = "briefcase.bitemporal.asof.valid_time"

# Content integrity
BITEMPORAL_CONTENT_HASH = "briefcase.bitemporal.content_hash"
