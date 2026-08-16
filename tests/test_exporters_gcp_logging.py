"""Tests for briefcase/exporters/gcp_logging.py (GCPCloudLoggingExporter).

Stubs the google-cloud-logging client via sys.modules injection; no real
GCP credentials or network access are needed. The exporter imports its
client lazily, so the stub satisfies that import at construction time.
"""

import asyncio
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gcp_stub(monkeypatch):
    """Install google.cloud.logging / google.oauth2.service_account stubs."""
    google_mod = ModuleType("google")
    cloud_mod = ModuleType("google.cloud")
    logging_mod = ModuleType("google.cloud.logging")
    oauth2_mod = ModuleType("google.oauth2")
    sa_mod = ModuleType("google.oauth2.service_account")

    mock_client = MagicMock()
    mock_logger = MagicMock()
    logging_mod.Client = MagicMock(return_value=mock_client)
    mock_client.logger.return_value = mock_logger
    sa_mod.Credentials = MagicMock()

    google_mod.cloud = cloud_mod
    google_mod.oauth2 = oauth2_mod
    cloud_mod.logging = logging_mod
    oauth2_mod.service_account = sa_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.logging", logging_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_mod)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", sa_mod)

    yield {
        "logging": logging_mod,
        "sa": sa_mod,
        "client": mock_client,
        "logger": mock_logger,
    }


def _make_exporter(**kwargs):
    from briefcase.exporters.gcp_logging import GCPCloudLoggingExporter

    defaults = dict(project="my-project", batch_size=3)
    defaults.update(kwargs)
    return GCPCloudLoggingExporter(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_returns_true(gcp_stub):
    """export() returns True on success."""
    exporter = _make_exporter()
    result = _run(exporter.export({"decision_id": "abc"}))
    assert result is True


def test_export_buffers_until_batch_size(gcp_stub):
    """export() buffers up to batch_size before flushing."""
    exporter = _make_exporter(batch_size=3)
    _run(exporter.export({"id": "1"}))
    _run(exporter.export({"id": "2"}))
    gcp_stub["logger"].log_struct.assert_not_called()
    _run(exporter.export({"id": "3"}))
    assert gcp_stub["logger"].log_struct.call_count == 3


def test_flush_sends_structured_entries(gcp_stub):
    """flush() sends structured log entries with severity='INFO'."""
    exporter = _make_exporter(batch_size=100)
    _run(exporter.export({"decision_id": "test"}))
    _run(exporter.flush())

    gcp_stub["logger"].log_struct.assert_called_once_with(
        {"decision_id": "test"}, severity="INFO"
    )


def test_flush_sets_json_payload(gcp_stub):
    """flush() passes the decision dict through unchanged."""
    exporter = _make_exporter(batch_size=100)
    decision = {"field_a": "value_a", "field_b": 42}
    _run(exporter.export(decision))
    _run(exporter.flush())

    call_args = gcp_stub["logger"].log_struct.call_args
    assert call_args.args[0] == decision


def test_export_returns_false_on_error(gcp_stub):
    """export() returns False when the logging client raises."""
    exporter = _make_exporter(batch_size=1)
    gcp_stub["logger"].log_struct.side_effect = Exception("GCP error")
    result = _run(exporter.export({"id": "1"}))
    assert result is False


def test_credentials_path_used(gcp_stub):
    """credentials_path is passed to service_account.Credentials."""
    mock_creds = MagicMock()
    gcp_stub["sa"].Credentials.from_service_account_file.return_value = mock_creds
    _make_exporter(credentials_path="/path/to/sa.json")
    gcp_stub["sa"].Credentials.from_service_account_file.assert_called_once_with(
        "/path/to/sa.json"
    )
    gcp_stub["logging"].Client.assert_called_once_with(
        project="my-project", credentials=mock_creds
    )


def test_no_credentials_uses_default(gcp_stub):
    """No credentials_path uses the default Client."""
    _make_exporter(credentials_path=None)
    gcp_stub["logging"].Client.assert_called_once_with(project="my-project")
    gcp_stub["sa"].Credentials.from_service_account_file.assert_not_called()


def test_flush_clears_buffer(gcp_stub):
    """flush() clears the buffer after sending."""
    exporter = _make_exporter(batch_size=100)
    _run(exporter.export({"id": "1"}))
    _run(exporter.flush())
    gcp_stub["logger"].log_struct.reset_mock()
    _run(exporter.flush())
    gcp_stub["logger"].log_struct.assert_not_called()


def test_decision_to_dict_with_dataclass(gcp_stub):
    """_decision_to_dict converts dataclasses."""
    from dataclasses import dataclass

    @dataclass
    class Decision:
        id: str
        score: float

    exporter = _make_exporter()
    result = exporter._decision_to_dict(Decision(id="abc", score=0.9))
    assert result == {"id": "abc", "score": 0.9}


def test_decision_to_dict_with_object(gcp_stub):
    """_decision_to_dict handles plain objects."""

    class Obj:
        def __init__(self):
            self.x = 1

    exporter = _make_exporter()
    result = exporter._decision_to_dict(Obj())
    assert result["x"] == 1


def test_close_flushes(gcp_stub):
    """close() flushes buffered records."""
    exporter = _make_exporter(batch_size=100)
    _run(exporter.export({"id": "1"}))
    _run(exporter.close())
    gcp_stub["logger"].log_struct.assert_called_once()


def test_missing_dependency_raises_with_hint(monkeypatch):
    """ImportError with install hint when google-cloud-logging is absent."""
    from briefcase.exporters.gcp_logging import GCPCloudLoggingExporter

    monkeypatch.setitem(sys.modules, "google", None)
    monkeypatch.setitem(sys.modules, "google.cloud", None)
    monkeypatch.setitem(sys.modules, "google.cloud.logging", None)
    with pytest.raises(ImportError, match="pip install"):
        GCPCloudLoggingExporter(project="p")
