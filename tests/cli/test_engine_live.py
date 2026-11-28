"""Opt-in read-only smoke test against a managed endpoint supplied by the operator."""
import os

import pytest

from briefcase.engine.client import EngineClient


@pytest.mark.skipif(not os.environ.get("BRIEFCASE_ENGINE_LIVE") or not os.environ.get("BRIEFCASE_ENGINE_ADDR"),
                    reason="requires an explicitly configured managed engine")
def test_managed_endpoint_identity():
    client = EngineClient()
    try:
        assert client.call("WhoAmI", {})
    finally:
        client.close()
