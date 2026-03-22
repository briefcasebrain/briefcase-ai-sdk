"""
Semantic conventions for external data source versioning.
"""

# API versioning
EXTERNAL_API_NAME = "external.api.name"
EXTERNAL_API_VERSION = "external.api.version"
EXTERNAL_API_ENDPOINT = "external.api.endpoint"
EXTERNAL_API_METHOD = "external.api.method"
EXTERNAL_API_STATUS_CODE = "external.api.status_code"

# Data versioning
EXTERNAL_DATA_SOURCE = "external.data.source"  # e.g., "ofac_sdn_list"
EXTERNAL_DATA_TIMESTAMP = "external.data.timestamp"  # When data was fetched
EXTERNAL_DATA_HASH = "external.data.hash"  # SHA-256 of response
EXTERNAL_DATA_SIZE = "external.data.size"  # Bytes
EXTERNAL_DATA_RECORD_COUNT = "external.data.record_count"

# ETL job tracking
EXTERNAL_ETL_JOB_ID = "external.etl.job_id"
EXTERNAL_ETL_RUN_ID = "external.etl.run_id"
EXTERNAL_ETL_PIPELINE = "external.etl.pipeline"

# Database query tracking
EXTERNAL_DB_SYSTEM = "external.db.system"  # e.g., "postgresql"
EXTERNAL_DB_NAME = "external.db.name"
EXTERNAL_DB_QUERY = "external.db.query"  # Parameterized query
EXTERNAL_DB_QUERY_HASH = "external.db.query_hash"
EXTERNAL_DB_RESULT_COUNT = "external.db.result_count"

# Cache tracking
EXTERNAL_CACHE_KEY = "external.cache.key"
EXTERNAL_CACHE_HIT = "external.cache.hit"
EXTERNAL_CACHE_TTL = "external.cache.ttl"

# Snapshot metadata
EXTERNAL_SNAPSHOT_ID = "external.snapshot.id"
EXTERNAL_SNAPSHOT_TIMESTAMP = "external.snapshot.timestamp"
EXTERNAL_SNAPSHOT_LOCATION = "external.snapshot.location"  # lakeFS path
