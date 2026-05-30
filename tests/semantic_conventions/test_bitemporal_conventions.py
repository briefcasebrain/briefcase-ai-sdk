"""Semantic convention tests for bitemporal + routing_policy modules."""

from briefcase.semantic_conventions import bitemporal, routing_policy, external_data


def _constants(module):
    return {
        name: getattr(module, name)
        for name in dir(module)
        if name.isupper() and not name.startswith("_")
    }


# --------------------------------------------------------------------------
# Bitemporal conventions
# --------------------------------------------------------------------------

def test_bitemporal_constants_are_strings_with_expected_prefix():
    consts = _constants(bitemporal)
    assert consts, "no constants defined in briefcase.semantic_conventions.bitemporal"
    for name, value in consts.items():
        assert isinstance(value, str), f"{name} is not a string"
        assert value.startswith("briefcase.bitemporal."), (
            f"{name} = {value!r} breaks the briefcase.bitemporal.* namespace"
        )


def test_bitemporal_core_attributes_present():
    required = {
        "BITEMPORAL_RECORD_ID",
        "BITEMPORAL_KEY",
        "BITEMPORAL_VALID_TIME",
        "BITEMPORAL_TRANSACTION_TIME",
        "BITEMPORAL_SOURCE",
        "BITEMPORAL_PARENT_RECORD_ID",
        "BITEMPORAL_ASOF_TRANSACTION_TIME",
        "BITEMPORAL_CONTENT_HASH",
    }
    assert required.issubset(_constants(bitemporal).keys())


# --------------------------------------------------------------------------
# Routing policy conventions
# --------------------------------------------------------------------------

def test_routing_policy_constants_are_strings_with_prefix():
    consts = _constants(routing_policy)
    assert consts
    for name, value in consts.items():
        assert isinstance(value, str), name
        assert value.startswith("briefcase.routing."), (
            f"{name} = {value!r} breaks the briefcase.routing.* namespace"
        )


def test_routing_policy_core_attributes_present():
    required = {
        "ROUTING_POLICY_ID",
        "ROUTING_POLICY_VERSION",
        "ROUTING_MATCHED_RULE_ID",
        "ROUTING_DECISION_ID",
        "ROUTING_USE_CASE",
        "ROUTING_SELECTED",
        "ROUTING_EVIDENCE_REFS",
    }
    assert required.issubset(_constants(routing_policy).keys())


# --------------------------------------------------------------------------
# External data conventions — new bitemporal attributes added
# --------------------------------------------------------------------------

def test_external_data_has_bitemporal_additions():
    consts = _constants(external_data)
    assert "EXTERNAL_DATA_VALID_TIME" in consts
    assert "EXTERNAL_DATA_TRANSACTION_TIME" in consts
    assert "EXTERNAL_DATA_CORRECTION_OF" in consts
    assert "EXTERNAL_DATA_SOURCE_TRUST_LEVEL" in consts
    # Legacy attribute is preserved.
    assert "EXTERNAL_DATA_TIMESTAMP" in consts
