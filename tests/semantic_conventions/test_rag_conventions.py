"""
Tests for RAG (Retrieval-Augmented Generation) semantic conventions.

Covers:
  - All constants are defined as strings
  - Constant values follow naming convention (rag.*)
  - Document versioning constants
  - Embedding versioning constants
  - Vector index versioning constants
  - Retrieval provenance constants
  - Retrieved document tracking constants
  - Version drift detection constants
"""

from briefcase.semantic_conventions import rag


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

def test_rag_document_id_defined():
    """Test that RAG_DOCUMENT_ID is defined."""
    assert hasattr(rag, 'RAG_DOCUMENT_ID')


def test_rag_document_version_defined():
    """Test that RAG_DOCUMENT_VERSION is defined."""
    assert hasattr(rag, 'RAG_DOCUMENT_VERSION')


def test_rag_document_path_defined():
    """Test that RAG_DOCUMENT_PATH is defined."""
    assert hasattr(rag, 'RAG_DOCUMENT_PATH')


def test_rag_document_hash_defined():
    """Test that RAG_DOCUMENT_HASH is defined."""
    assert hasattr(rag, 'RAG_DOCUMENT_HASH')


def test_rag_document_updated_at_defined():
    """Test that RAG_DOCUMENT_UPDATED_AT is defined."""
    assert hasattr(rag, 'RAG_DOCUMENT_UPDATED_AT')


def test_rag_embedding_model_defined():
    """Test that RAG_EMBEDDING_MODEL is defined."""
    assert hasattr(rag, 'RAG_EMBEDDING_MODEL')


def test_rag_embedding_model_version_defined():
    """Test that RAG_EMBEDDING_MODEL_VERSION is defined."""
    assert hasattr(rag, 'RAG_EMBEDDING_MODEL_VERSION')


def test_rag_embedding_dimensions_defined():
    """Test that RAG_EMBEDDING_DIMENSIONS is defined."""
    assert hasattr(rag, 'RAG_EMBEDDING_DIMENSIONS')


def test_rag_embedding_created_at_defined():
    """Test that RAG_EMBEDDING_CREATED_AT is defined."""
    assert hasattr(rag, 'RAG_EMBEDDING_CREATED_AT')


def test_rag_embedding_batch_id_defined():
    """Test that RAG_EMBEDDING_BATCH_ID is defined."""
    assert hasattr(rag, 'RAG_EMBEDDING_BATCH_ID')


def test_rag_index_name_defined():
    """Test that RAG_INDEX_NAME is defined."""
    assert hasattr(rag, 'RAG_INDEX_NAME')


def test_rag_index_version_defined():
    """Test that RAG_INDEX_VERSION is defined."""
    assert hasattr(rag, 'RAG_INDEX_VERSION')


def test_rag_index_size_defined():
    """Test that RAG_INDEX_SIZE is defined."""
    assert hasattr(rag, 'RAG_INDEX_SIZE')


def test_rag_index_updated_at_defined():
    """Test that RAG_INDEX_UPDATED_AT is defined."""
    assert hasattr(rag, 'RAG_INDEX_UPDATED_AT')


def test_rag_query_text_defined():
    """Test that RAG_QUERY_TEXT is defined."""
    assert hasattr(rag, 'RAG_QUERY_TEXT')


def test_rag_query_embedding_model_defined():
    """Test that RAG_QUERY_EMBEDDING_MODEL is defined."""
    assert hasattr(rag, 'RAG_QUERY_EMBEDDING_MODEL')


def test_rag_top_k_defined():
    """Test that RAG_TOP_K is defined."""
    assert hasattr(rag, 'RAG_TOP_K')


def test_rag_similarity_threshold_defined():
    """Test that RAG_SIMILARITY_THRESHOLD is defined."""
    assert hasattr(rag, 'RAG_SIMILARITY_THRESHOLD')


def test_rag_retrieved_count_defined():
    """Test that RAG_RETRIEVED_COUNT is defined."""
    assert hasattr(rag, 'RAG_RETRIEVED_COUNT')


def test_rag_result_document_id_defined():
    """Test that RAG_RESULT_DOCUMENT_ID is defined."""
    assert hasattr(rag, 'RAG_RESULT_DOCUMENT_ID')


def test_rag_result_score_defined():
    """Test that RAG_RESULT_SCORE is defined."""
    assert hasattr(rag, 'RAG_RESULT_SCORE')


def test_rag_result_rank_defined():
    """Test that RAG_RESULT_RANK is defined."""
    assert hasattr(rag, 'RAG_RESULT_RANK')


def test_rag_result_document_version_defined():
    """Test that RAG_RESULT_DOCUMENT_VERSION is defined."""
    assert hasattr(rag, 'RAG_RESULT_DOCUMENT_VERSION')


def test_rag_version_drift_defined():
    """Test that RAG_VERSION_DRIFT is defined."""
    assert hasattr(rag, 'RAG_VERSION_DRIFT')


def test_rag_version_drift_reason_defined():
    """Test that RAG_VERSION_DRIFT_REASON is defined."""
    assert hasattr(rag, 'RAG_VERSION_DRIFT_REASON')


#
# Tests: All constants are strings
#

def test_all_constants_are_strings():
    """Test that all constants are strings."""
    constants = get_module_constants(rag)
    for name, value in constants.items():
        assert isinstance(value, str), f"{name} is not a string, got {type(value)}"


#
# Tests: Naming convention (rag.*)
#

def test_all_constants_follow_naming_convention():
    """Test that all constants follow rag.* naming convention."""
    constants = get_module_constants(rag)
    for name, value in constants.items():
        assert value.startswith("rag."), \
            f"{name}='{value}' does not start with 'rag.'"


def test_document_constants_have_correct_prefix():
    """Test that document constants have rag.document prefix."""
    assert rag.RAG_DOCUMENT_ID.startswith("rag.document")
    assert rag.RAG_DOCUMENT_VERSION.startswith("rag.document")
    assert rag.RAG_DOCUMENT_PATH.startswith("rag.document")
    assert rag.RAG_DOCUMENT_HASH.startswith("rag.document")
    assert rag.RAG_DOCUMENT_UPDATED_AT.startswith("rag.document")


def test_embedding_constants_have_correct_prefix():
    """Test that embedding constants have rag.embedding prefix."""
    assert rag.RAG_EMBEDDING_MODEL.startswith("rag.embedding")
    assert rag.RAG_EMBEDDING_MODEL_VERSION.startswith("rag.embedding")
    assert rag.RAG_EMBEDDING_DIMENSIONS.startswith("rag.embedding")
    assert rag.RAG_EMBEDDING_CREATED_AT.startswith("rag.embedding")
    assert rag.RAG_EMBEDDING_BATCH_ID.startswith("rag.embedding")


def test_index_constants_have_correct_prefix():
    """Test that index constants have rag.index prefix."""
    assert rag.RAG_INDEX_NAME.startswith("rag.index")
    assert rag.RAG_INDEX_VERSION.startswith("rag.index")
    assert rag.RAG_INDEX_SIZE.startswith("rag.index")
    assert rag.RAG_INDEX_UPDATED_AT.startswith("rag.index")


def test_retrieval_constants_have_correct_prefix():
    """Test that retrieval constants have rag.query or rag.retrieval prefix."""
    assert rag.RAG_QUERY_TEXT.startswith("rag.query")
    assert rag.RAG_QUERY_EMBEDDING_MODEL.startswith("rag.query")
    assert rag.RAG_TOP_K.startswith("rag.retrieval")
    assert rag.RAG_SIMILARITY_THRESHOLD.startswith("rag.retrieval")
    assert rag.RAG_RETRIEVED_COUNT.startswith("rag.retrieval")


def test_result_constants_have_correct_prefix():
    """Test that result constants have rag.result prefix."""
    assert rag.RAG_RESULT_DOCUMENT_ID.startswith("rag.result")
    assert rag.RAG_RESULT_SCORE.startswith("rag.result")
    assert rag.RAG_RESULT_RANK.startswith("rag.result")
    assert rag.RAG_RESULT_DOCUMENT_VERSION.startswith("rag.result")


def test_version_drift_constants_have_correct_prefix():
    """Test that version drift constants have rag.version prefix."""
    assert rag.RAG_VERSION_DRIFT.startswith("rag.version")
    assert rag.RAG_VERSION_DRIFT_REASON.startswith("rag.version")


#
# Tests: Specific constant values
#

def test_document_id_value():
    """Test RAG_DOCUMENT_ID value."""
    assert rag.RAG_DOCUMENT_ID == "rag.document.id"


def test_document_version_value():
    """Test RAG_DOCUMENT_VERSION value."""
    assert rag.RAG_DOCUMENT_VERSION == "rag.document.version"


def test_document_path_value():
    """Test RAG_DOCUMENT_PATH value."""
    assert rag.RAG_DOCUMENT_PATH == "rag.document.path"


def test_document_hash_value():
    """Test RAG_DOCUMENT_HASH value."""
    assert rag.RAG_DOCUMENT_HASH == "rag.document.hash"


def test_document_updated_at_value():
    """Test RAG_DOCUMENT_UPDATED_AT value."""
    assert rag.RAG_DOCUMENT_UPDATED_AT == "rag.document.updated_at"


def test_embedding_model_value():
    """Test RAG_EMBEDDING_MODEL value."""
    assert rag.RAG_EMBEDDING_MODEL == "rag.embedding.model"


def test_embedding_model_version_value():
    """Test RAG_EMBEDDING_MODEL_VERSION value."""
    assert rag.RAG_EMBEDDING_MODEL_VERSION == "rag.embedding.model.version"


def test_embedding_dimensions_value():
    """Test RAG_EMBEDDING_DIMENSIONS value."""
    assert rag.RAG_EMBEDDING_DIMENSIONS == "rag.embedding.dimensions"


def test_embedding_created_at_value():
    """Test RAG_EMBEDDING_CREATED_AT value."""
    assert rag.RAG_EMBEDDING_CREATED_AT == "rag.embedding.created_at"


def test_embedding_batch_id_value():
    """Test RAG_EMBEDDING_BATCH_ID value."""
    assert rag.RAG_EMBEDDING_BATCH_ID == "rag.embedding.batch_id"


def test_index_name_value():
    """Test RAG_INDEX_NAME value."""
    assert rag.RAG_INDEX_NAME == "rag.index.name"


def test_index_version_value():
    """Test RAG_INDEX_VERSION value."""
    assert rag.RAG_INDEX_VERSION == "rag.index.version"


def test_index_size_value():
    """Test RAG_INDEX_SIZE value."""
    assert rag.RAG_INDEX_SIZE == "rag.index.size"


def test_index_updated_at_value():
    """Test RAG_INDEX_UPDATED_AT value."""
    assert rag.RAG_INDEX_UPDATED_AT == "rag.index.updated_at"


def test_query_text_value():
    """Test RAG_QUERY_TEXT value."""
    assert rag.RAG_QUERY_TEXT == "rag.query.text"


def test_query_embedding_model_value():
    """Test RAG_QUERY_EMBEDDING_MODEL value."""
    assert rag.RAG_QUERY_EMBEDDING_MODEL == "rag.query.embedding.model"


def test_top_k_value():
    """Test RAG_TOP_K value."""
    assert rag.RAG_TOP_K == "rag.retrieval.top_k"


def test_similarity_threshold_value():
    """Test RAG_SIMILARITY_THRESHOLD value."""
    assert rag.RAG_SIMILARITY_THRESHOLD == "rag.retrieval.similarity_threshold"


def test_retrieved_count_value():
    """Test RAG_RETRIEVED_COUNT value."""
    assert rag.RAG_RETRIEVED_COUNT == "rag.retrieval.count"


def test_result_document_id_value():
    """Test RAG_RESULT_DOCUMENT_ID value."""
    assert rag.RAG_RESULT_DOCUMENT_ID == "rag.result.document.id"


def test_result_score_value():
    """Test RAG_RESULT_SCORE value."""
    assert rag.RAG_RESULT_SCORE == "rag.result.score"


def test_result_rank_value():
    """Test RAG_RESULT_RANK value."""
    assert rag.RAG_RESULT_RANK == "rag.result.rank"


def test_result_document_version_value():
    """Test RAG_RESULT_DOCUMENT_VERSION value."""
    assert rag.RAG_RESULT_DOCUMENT_VERSION == "rag.result.document.version"


def test_version_drift_value():
    """Test RAG_VERSION_DRIFT value."""
    assert rag.RAG_VERSION_DRIFT == "rag.version.drift"


def test_version_drift_reason_value():
    """Test RAG_VERSION_DRIFT_REASON value."""
    assert rag.RAG_VERSION_DRIFT_REASON == "rag.version.drift.reason"


#
# Tests: Constant count and coverage
#

def test_expected_number_of_constants():
    """Test that the module has the expected number of constants."""
    constants = get_module_constants(rag)
    # 5 Document + 5 Embedding + 4 Index + 5 Retrieval + 4 Result + 2 Drift = 25
    assert len(constants) == 25, f"Expected 25 constants, got {len(constants)}"


def test_no_duplicate_values():
    """Test that no two constants have the same value."""
    constants = get_module_constants(rag)
    values = list(constants.values())
    assert len(values) == len(set(values)), "Duplicate constant values found"


#
# Tests: Semantic grouping
#

def test_document_group_completeness():
    """Test that all document-related constants are present."""
    document_constants = [
        rag.RAG_DOCUMENT_ID,
        rag.RAG_DOCUMENT_VERSION,
        rag.RAG_DOCUMENT_PATH,
        rag.RAG_DOCUMENT_HASH,
        rag.RAG_DOCUMENT_UPDATED_AT,
    ]
    assert len(document_constants) == 5
    assert all(isinstance(c, str) for c in document_constants)


def test_embedding_group_completeness():
    """Test that all embedding-related constants are present."""
    embedding_constants = [
        rag.RAG_EMBEDDING_MODEL,
        rag.RAG_EMBEDDING_MODEL_VERSION,
        rag.RAG_EMBEDDING_DIMENSIONS,
        rag.RAG_EMBEDDING_CREATED_AT,
        rag.RAG_EMBEDDING_BATCH_ID,
    ]
    assert len(embedding_constants) == 5
    assert all(isinstance(c, str) for c in embedding_constants)


def test_index_group_completeness():
    """Test that all index-related constants are present."""
    index_constants = [
        rag.RAG_INDEX_NAME,
        rag.RAG_INDEX_VERSION,
        rag.RAG_INDEX_SIZE,
        rag.RAG_INDEX_UPDATED_AT,
    ]
    assert len(index_constants) == 4
    assert all(isinstance(c, str) for c in index_constants)


def test_retrieval_group_completeness():
    """Test that all retrieval-related constants are present."""
    retrieval_constants = [
        rag.RAG_QUERY_TEXT,
        rag.RAG_QUERY_EMBEDDING_MODEL,
        rag.RAG_TOP_K,
        rag.RAG_SIMILARITY_THRESHOLD,
        rag.RAG_RETRIEVED_COUNT,
    ]
    assert len(retrieval_constants) == 5
    assert all(isinstance(c, str) for c in retrieval_constants)


def test_result_group_completeness():
    """Test that all result-related constants are present."""
    result_constants = [
        rag.RAG_RESULT_DOCUMENT_ID,
        rag.RAG_RESULT_SCORE,
        rag.RAG_RESULT_RANK,
        rag.RAG_RESULT_DOCUMENT_VERSION,
    ]
    assert len(result_constants) == 4
    assert all(isinstance(c, str) for c in result_constants)


def test_version_drift_group_completeness():
    """Test that all version drift-related constants are present."""
    drift_constants = [
        rag.RAG_VERSION_DRIFT,
        rag.RAG_VERSION_DRIFT_REASON,
    ]
    assert len(drift_constants) == 2
    assert all(isinstance(c, str) for c in drift_constants)
