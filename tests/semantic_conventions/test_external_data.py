"""
Tests for external data source semantic conventions.

Covers:
  - All constants are defined as strings
  - Constant values follow naming convention (external.*)
  - API versioning constants
  - Data versioning constants
  - ETL job tracking constants
  - Database query tracking constants
  - Cache tracking constants
  - Snapshot metadata constants
"""

import pytest
from briefcase.semantic_conventions import external_data


# 
# Helper function
# 

def get_module_constants(module):
    """Get all uppercase constants from a module."""
    return {
        name: getattr(module, name)
        for name in dir(module)
        if name.isupper() and not name.startswith('_')
    }


# 
# Tests: All constants defined
# 

def test_external_api_name_defined():
    """Test that EXTERNAL_API_NAME is defined."""
    assert hasattr(external_data, 'EXTERNAL_API_NAME')


def test_external_api_version_defined():
    """Test that EXTERNAL_API_VERSION is defined."""
    assert hasattr(external_data, 'EXTERNAL_API_VERSION')


def test_external_api_endpoint_defined():
    """Test that EXTERNAL_API_ENDPOINT is defined."""
    assert hasattr(external_data, 'EXTERNAL_API_ENDPOINT')


def test_external_api_method_defined():
    """Test that EXTERNAL_API_METHOD is defined."""
    assert hasattr(external_data, 'EXTERNAL_API_METHOD')


def test_external_api_status_code_defined():
    """Test that EXTERNAL_API_STATUS_CODE is defined."""
    assert hasattr(external_data, 'EXTERNAL_API_STATUS_CODE')


def test_external_data_source_defined():
    """Test that EXTERNAL_DATA_SOURCE is defined."""
    assert hasattr(external_data, 'EXTERNAL_DATA_SOURCE')


def test_external_data_timestamp_defined():
    """Test that EXTERNAL_DATA_TIMESTAMP is defined."""
    assert hasattr(external_data, 'EXTERNAL_DATA_TIMESTAMP')


def test_external_data_hash_defined():
    """Test that EXTERNAL_DATA_HASH is defined."""
    assert hasattr(external_data, 'EXTERNAL_DATA_HASH')


def test_external_data_size_defined():
    """Test that EXTERNAL_DATA_SIZE is defined."""
    assert hasattr(external_data, 'EXTERNAL_DATA_SIZE')


def test_external_data_record_count_defined():
    """Test that EXTERNAL_DATA_RECORD_COUNT is defined."""
    assert hasattr(external_data, 'EXTERNAL_DATA_RECORD_COUNT')


def test_external_etl_job_id_defined():
    """Test that EXTERNAL_ETL_JOB_ID is defined."""
    assert hasattr(external_data, 'EXTERNAL_ETL_JOB_ID')


def test_external_etl_run_id_defined():
    """Test that EXTERNAL_ETL_RUN_ID is defined."""
    assert hasattr(external_data, 'EXTERNAL_ETL_RUN_ID')


def test_external_etl_pipeline_defined():
    """Test that EXTERNAL_ETL_PIPELINE is defined."""
    assert hasattr(external_data, 'EXTERNAL_ETL_PIPELINE')


def test_external_db_system_defined():
    """Test that EXTERNAL_DB_SYSTEM is defined."""
    assert hasattr(external_data, 'EXTERNAL_DB_SYSTEM')


def test_external_db_name_defined():
    """Test that EXTERNAL_DB_NAME is defined."""
    assert hasattr(external_data, 'EXTERNAL_DB_NAME')


def test_external_db_query_defined():
    """Test that EXTERNAL_DB_QUERY is defined."""
    assert hasattr(external_data, 'EXTERNAL_DB_QUERY')


def test_external_db_query_hash_defined():
    """Test that EXTERNAL_DB_QUERY_HASH is defined."""
    assert hasattr(external_data, 'EXTERNAL_DB_QUERY_HASH')


def test_external_db_result_count_defined():
    """Test that EXTERNAL_DB_RESULT_COUNT is defined."""
    assert hasattr(external_data, 'EXTERNAL_DB_RESULT_COUNT')


def test_external_cache_key_defined():
    """Test that EXTERNAL_CACHE_KEY is defined."""
    assert hasattr(external_data, 'EXTERNAL_CACHE_KEY')


def test_external_cache_hit_defined():
    """Test that EXTERNAL_CACHE_HIT is defined."""
    assert hasattr(external_data, 'EXTERNAL_CACHE_HIT')


def test_external_cache_ttl_defined():
    """Test that EXTERNAL_CACHE_TTL is defined."""
    assert hasattr(external_data, 'EXTERNAL_CACHE_TTL')


def test_external_snapshot_id_defined():
    """Test that EXTERNAL_SNAPSHOT_ID is defined."""
    assert hasattr(external_data, 'EXTERNAL_SNAPSHOT_ID')


def test_external_snapshot_timestamp_defined():
    """Test that EXTERNAL_SNAPSHOT_TIMESTAMP is defined."""
    assert hasattr(external_data, 'EXTERNAL_SNAPSHOT_TIMESTAMP')


def test_external_snapshot_location_defined():
    """Test that EXTERNAL_SNAPSHOT_LOCATION is defined."""
    assert hasattr(external_data, 'EXTERNAL_SNAPSHOT_LOCATION')


# 
# Tests: All constants are strings
# 

def test_all_constants_are_strings():
    """Test that all constants are strings."""
    constants = get_module_constants(external_data)
    for name, value in constants.items():
        assert isinstance(value, str), f"{name} is not a string, got {type(value)}"


# 
# Tests: Naming convention (external.*)
# 

def test_all_constants_follow_naming_convention():
    """Test that all constants follow external.* naming convention."""
    constants = get_module_constants(external_data)
    for name, value in constants.items():
        assert value.startswith("external."), \
            f"{name}='{value}' does not start with 'external.'"


def test_api_constants_have_correct_prefix():
    """Test that API constants have external.api prefix."""
    assert external_data.EXTERNAL_API_NAME.startswith("external.api")
    assert external_data.EXTERNAL_API_VERSION.startswith("external.api")
    assert external_data.EXTERNAL_API_ENDPOINT.startswith("external.api")
    assert external_data.EXTERNAL_API_METHOD.startswith("external.api")
    assert external_data.EXTERNAL_API_STATUS_CODE.startswith("external.api")


def test_data_constants_have_correct_prefix():
    """Test that data constants have external.data prefix."""
    assert external_data.EXTERNAL_DATA_SOURCE.startswith("external.data")
    assert external_data.EXTERNAL_DATA_TIMESTAMP.startswith("external.data")
    assert external_data.EXTERNAL_DATA_HASH.startswith("external.data")
    assert external_data.EXTERNAL_DATA_SIZE.startswith("external.data")
    assert external_data.EXTERNAL_DATA_RECORD_COUNT.startswith("external.data")


def test_etl_constants_have_correct_prefix():
    """Test that ETL constants have external.etl prefix."""
    assert external_data.EXTERNAL_ETL_JOB_ID.startswith("external.etl")
    assert external_data.EXTERNAL_ETL_RUN_ID.startswith("external.etl")
    assert external_data.EXTERNAL_ETL_PIPELINE.startswith("external.etl")


def test_db_constants_have_correct_prefix():
    """Test that database constants have external.db prefix."""
    assert external_data.EXTERNAL_DB_SYSTEM.startswith("external.db")
    assert external_data.EXTERNAL_DB_NAME.startswith("external.db")
    assert external_data.EXTERNAL_DB_QUERY.startswith("external.db")
    assert external_data.EXTERNAL_DB_QUERY_HASH.startswith("external.db")
    assert external_data.EXTERNAL_DB_RESULT_COUNT.startswith("external.db")


def test_cache_constants_have_correct_prefix():
    """Test that cache constants have external.cache prefix."""
    assert external_data.EXTERNAL_CACHE_KEY.startswith("external.cache")
    assert external_data.EXTERNAL_CACHE_HIT.startswith("external.cache")
    assert external_data.EXTERNAL_CACHE_TTL.startswith("external.cache")


def test_snapshot_constants_have_correct_prefix():
    """Test that snapshot constants have external.snapshot prefix."""
    assert external_data.EXTERNAL_SNAPSHOT_ID.startswith("external.snapshot")
    assert external_data.EXTERNAL_SNAPSHOT_TIMESTAMP.startswith("external.snapshot")
    assert external_data.EXTERNAL_SNAPSHOT_LOCATION.startswith("external.snapshot")


# 
# Tests: Specific constant values
# 

def test_api_name_value():
    """Test EXTERNAL_API_NAME value."""
    assert external_data.EXTERNAL_API_NAME == "external.api.name"


def test_api_version_value():
    """Test EXTERNAL_API_VERSION value."""
    assert external_data.EXTERNAL_API_VERSION == "external.api.version"


def test_api_endpoint_value():
    """Test EXTERNAL_API_ENDPOINT value."""
    assert external_data.EXTERNAL_API_ENDPOINT == "external.api.endpoint"


def test_api_method_value():
    """Test EXTERNAL_API_METHOD value."""
    assert external_data.EXTERNAL_API_METHOD == "external.api.method"


def test_api_status_code_value():
    """Test EXTERNAL_API_STATUS_CODE value."""
    assert external_data.EXTERNAL_API_STATUS_CODE == "external.api.status_code"


def test_data_source_value():
    """Test EXTERNAL_DATA_SOURCE value."""
    assert external_data.EXTERNAL_DATA_SOURCE == "external.data.source"


def test_data_timestamp_value():
    """Test EXTERNAL_DATA_TIMESTAMP value."""
    assert external_data.EXTERNAL_DATA_TIMESTAMP == "external.data.timestamp"


def test_data_hash_value():
    """Test EXTERNAL_DATA_HASH value."""
    assert external_data.EXTERNAL_DATA_HASH == "external.data.hash"


def test_data_size_value():
    """Test EXTERNAL_DATA_SIZE value."""
    assert external_data.EXTERNAL_DATA_SIZE == "external.data.size"


def test_data_record_count_value():
    """Test EXTERNAL_DATA_RECORD_COUNT value."""
    assert external_data.EXTERNAL_DATA_RECORD_COUNT == "external.data.record_count"


def test_etl_job_id_value():
    """Test EXTERNAL_ETL_JOB_ID value."""
    assert external_data.EXTERNAL_ETL_JOB_ID == "external.etl.job_id"


def test_etl_run_id_value():
    """Test EXTERNAL_ETL_RUN_ID value."""
    assert external_data.EXTERNAL_ETL_RUN_ID == "external.etl.run_id"


def test_etl_pipeline_value():
    """Test EXTERNAL_ETL_PIPELINE value."""
    assert external_data.EXTERNAL_ETL_PIPELINE == "external.etl.pipeline"


def test_db_system_value():
    """Test EXTERNAL_DB_SYSTEM value."""
    assert external_data.EXTERNAL_DB_SYSTEM == "external.db.system"


def test_db_name_value():
    """Test EXTERNAL_DB_NAME value."""
    assert external_data.EXTERNAL_DB_NAME == "external.db.name"


def test_db_query_value():
    """Test EXTERNAL_DB_QUERY value."""
    assert external_data.EXTERNAL_DB_QUERY == "external.db.query"


def test_db_query_hash_value():
    """Test EXTERNAL_DB_QUERY_HASH value."""
    assert external_data.EXTERNAL_DB_QUERY_HASH == "external.db.query_hash"


def test_db_result_count_value():
    """Test EXTERNAL_DB_RESULT_COUNT value."""
    assert external_data.EXTERNAL_DB_RESULT_COUNT == "external.db.result_count"


def test_cache_key_value():
    """Test EXTERNAL_CACHE_KEY value."""
    assert external_data.EXTERNAL_CACHE_KEY == "external.cache.key"


def test_cache_hit_value():
    """Test EXTERNAL_CACHE_HIT value."""
    assert external_data.EXTERNAL_CACHE_HIT == "external.cache.hit"


def test_cache_ttl_value():
    """Test EXTERNAL_CACHE_TTL value."""
    assert external_data.EXTERNAL_CACHE_TTL == "external.cache.ttl"


def test_snapshot_id_value():
    """Test EXTERNAL_SNAPSHOT_ID value."""
    assert external_data.EXTERNAL_SNAPSHOT_ID == "external.snapshot.id"


def test_snapshot_timestamp_value():
    """Test EXTERNAL_SNAPSHOT_TIMESTAMP value."""
    assert external_data.EXTERNAL_SNAPSHOT_TIMESTAMP == "external.snapshot.timestamp"


def test_snapshot_location_value():
    """Test EXTERNAL_SNAPSHOT_LOCATION value."""
    assert external_data.EXTERNAL_SNAPSHOT_LOCATION == "external.snapshot.location"


# 
# Tests: Constant count and coverage
# 

def test_expected_number_of_constants():
    """Test that the module has the expected number of constants."""
    constants = get_module_constants(external_data)
    # 5 API + 5 Data + 3 ETL + 5 DB + 3 Cache + 3 Snapshot + 4 Bitemporal = 28
    assert len(constants) == 28, f"Expected 28 constants, got {len(constants)}"


def test_no_duplicate_values():
    """Test that no two constants have the same value."""
    constants = get_module_constants(external_data)
    values = list(constants.values())
    assert len(values) == len(set(values)), "Duplicate constant values found"


# 
# Tests: Semantic grouping
# 

def test_api_group_completeness():
    """Test that all API-related constants are present."""
    api_constants = [
        external_data.EXTERNAL_API_NAME,
        external_data.EXTERNAL_API_VERSION,
        external_data.EXTERNAL_API_ENDPOINT,
        external_data.EXTERNAL_API_METHOD,
        external_data.EXTERNAL_API_STATUS_CODE,
    ]
    assert len(api_constants) == 5
    assert all(isinstance(c, str) for c in api_constants)


def test_data_group_completeness():
    """Test that all data-related constants are present."""
    data_constants = [
        external_data.EXTERNAL_DATA_SOURCE,
        external_data.EXTERNAL_DATA_TIMESTAMP,
        external_data.EXTERNAL_DATA_HASH,
        external_data.EXTERNAL_DATA_SIZE,
        external_data.EXTERNAL_DATA_RECORD_COUNT,
    ]
    assert len(data_constants) == 5
    assert all(isinstance(c, str) for c in data_constants)


def test_etl_group_completeness():
    """Test that all ETL-related constants are present."""
    etl_constants = [
        external_data.EXTERNAL_ETL_JOB_ID,
        external_data.EXTERNAL_ETL_RUN_ID,
        external_data.EXTERNAL_ETL_PIPELINE,
    ]
    assert len(etl_constants) == 3
    assert all(isinstance(c, str) for c in etl_constants)


def test_db_group_completeness():
    """Test that all database-related constants are present."""
    db_constants = [
        external_data.EXTERNAL_DB_SYSTEM,
        external_data.EXTERNAL_DB_NAME,
        external_data.EXTERNAL_DB_QUERY,
        external_data.EXTERNAL_DB_QUERY_HASH,
        external_data.EXTERNAL_DB_RESULT_COUNT,
    ]
    assert len(db_constants) == 5
    assert all(isinstance(c, str) for c in db_constants)


def test_cache_group_completeness():
    """Test that all cache-related constants are present."""
    cache_constants = [
        external_data.EXTERNAL_CACHE_KEY,
        external_data.EXTERNAL_CACHE_HIT,
        external_data.EXTERNAL_CACHE_TTL,
    ]
    assert len(cache_constants) == 3
    assert all(isinstance(c, str) for c in cache_constants)


def test_snapshot_group_completeness():
    """Test that all snapshot-related constants are present."""
    snapshot_constants = [
        external_data.EXTERNAL_SNAPSHOT_ID,
        external_data.EXTERNAL_SNAPSHOT_TIMESTAMP,
        external_data.EXTERNAL_SNAPSHOT_LOCATION,
    ]
    assert len(snapshot_constants) == 3
    assert all(isinstance(c, str) for c in snapshot_constants)
