"""briefcase.observe() with no arguments: destination from the environment."""

import logging

import pytest

import briefcase
from briefcase import _observe
from briefcase.config import BriefcaseConfig
from briefcase.exporters.base import BaseExporter

ENV_VARS = (
    "BRIEFCASE_EXPORTER", "BRIEFCASE_EXPORTER_TOKEN",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SERVICE_NAME",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    BriefcaseConfig.reset()
    yield
    BriefcaseConfig.reset()


class FakeOTel(BaseExporter):
    """Records how observe() would construct the real OTelExporter."""

    def __init__(self, endpoint, service_name, protocol):
        self.endpoint = endpoint
        self.service_name = service_name
        self.protocol = protocol

    async def export(self, decision):
        return True

    async def flush(self):
        pass

    async def close(self):
        pass


@pytest.fixture
def fake_otel(monkeypatch):
    import briefcase.exporters.otel as otel_module

    monkeypatch.setattr(otel_module, "OTelExporter", FakeOTel)
    return FakeOTel


# -- BRIEFCASE_EXPORTER -----------------------------------------------------

def test_no_arguments_and_no_environment_is_the_console():
    exporter = briefcase.observe()
    assert isinstance(exporter, briefcase.ConsoleExporter)
    assert BriefcaseConfig.get().exporter is exporter


@pytest.mark.parametrize("value,expected", [
    ("console", briefcase.ConsoleExporter),
    ("memory", briefcase.MemoryExporter),
    ("https://collector.example.com/v1/decisions", briefcase.HTTPExporter),
    ("http://localhost:8080/ingest", briefcase.HTTPExporter),
])
def test_briefcase_exporter_shorthands(monkeypatch, value, expected):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", value)
    exporter = briefcase.observe()
    assert isinstance(exporter, expected)
    assert BriefcaseConfig.get().exporter is exporter


def test_briefcase_exporter_jsonl_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", str(tmp_path / "runs.jsonl"))
    assert isinstance(briefcase.observe(), briefcase.JSONLFileExporter)


def test_token_is_sent_as_bearer_for_http_destinations(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "https://collector.example.com/ingest")
    monkeypatch.setenv("BRIEFCASE_EXPORTER_TOKEN", "tok-123")
    exporter = briefcase.observe()
    assert exporter._headers["Authorization"] == "Bearer tok-123"


def test_http_destination_without_token_sends_no_authorization(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "https://collector.example.com/ingest")
    assert "Authorization" not in briefcase.observe()._headers


def test_unknown_briefcase_exporter_value_raises(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "carrier-pigeon")
    with pytest.raises(ValueError, match="Unknown exporter shorthand"):
        briefcase.observe()


# -- explicit arguments keep their behaviour --------------------------------

def test_explicit_arguments_ignore_the_environment(monkeypatch, fake_otel):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "memory")
    monkeypatch.setenv("BRIEFCASE_EXPORTER_TOKEN", "tok-123")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")

    assert isinstance(briefcase.observe("console"), briefcase.ConsoleExporter)
    assert isinstance(briefcase.observe(None), briefcase.ConsoleExporter)
    assert isinstance(briefcase.observe(exporter="memory"), briefcase.MemoryExporter)
    mine = briefcase.MemoryExporter()
    assert briefcase.observe(mine) is mine
    explicit_http = briefcase.observe("https://collector.example.com/ingest")
    assert "Authorization" not in explicit_http._headers
    with pytest.raises(ValueError):
        briefcase.observe("nope")


# -- OpenTelemetry environment ----------------------------------------------

def test_otel_endpoint_defaults_to_grpc(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    exporter = briefcase.observe()
    assert isinstance(exporter, fake_otel)
    assert (exporter.endpoint, exporter.protocol) == ("http://collector:4317", "grpc")
    assert exporter.service_name == "briefcase-ai"
    assert BriefcaseConfig.get().exporter is exporter


def test_otel_http_protocol_appends_the_traces_path(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example.com/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout")
    exporter = briefcase.observe()
    assert exporter.endpoint == "https://otlp.example.com/v1/traces"
    assert exporter.protocol == "http"
    assert exporter.service_name == "checkout"


def test_otel_port_4318_implies_http(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    exporter = briefcase.observe()
    assert (exporter.endpoint, exporter.protocol) == ("http://localhost:4318/v1/traces", "http")


def test_otel_traces_endpoint_is_used_as_given(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://ignored:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://t.example.com/custom/v1/traces")
    exporter = briefcase.observe()
    assert exporter.endpoint == "https://t.example.com/custom/v1/traces"
    assert exporter.protocol == "http"


def test_otel_explicit_grpc_protocol_wins_over_the_port(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    exporter = briefcase.observe()
    assert (exporter.endpoint, exporter.protocol) == ("http://localhost:4318", "grpc")


def test_briefcase_exporter_takes_precedence_over_otel(monkeypatch, fake_otel):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "memory")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    assert isinstance(briefcase.observe(), briefcase.MemoryExporter)


def test_briefcase_exporter_otlp_uses_the_otel_defaults(monkeypatch, fake_otel):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "otlp")
    exporter = briefcase.observe()
    assert isinstance(exporter, fake_otel)
    assert (exporter.endpoint, exporter.protocol) == ("http://localhost:4317", "grpc")


def test_missing_otel_extra_falls_back_to_console_with_a_warning(monkeypatch, mocker):
    import briefcase.exporters.otel as otel_module

    class Unavailable:
        def __init__(self, *args, **kwargs):
            raise ImportError("opentelemetry-api and opentelemetry-sdk are required.")

    monkeypatch.setattr(otel_module, "OTelExporter", Unavailable)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    log_warning = mocker.patch.object(_observe.logger, "warning")

    with pytest.warns(RuntimeWarning, match=r"briefcase-ai\[otel\]"):
        exporter = briefcase.observe()

    assert isinstance(exporter, briefcase.ConsoleExporter)
    assert BriefcaseConfig.get().exporter is exporter
    assert log_warning.call_count == 1
    assert "pip install 'briefcase-ai[otel]'" in log_warning.call_args[0][0]


def test_real_otel_exporter_honours_headers_and_service_name(monkeypatch):
    """With the otel extra installed, the standard variables reach the real
    exporter; OTEL_EXPORTER_OTLP_HEADERS is read by the OTLP exporter itself."""
    pytest.importorskip("opentelemetry.sdk.trace")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=abc123")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout")

    exporter = briefcase.observe()
    try:
        assert isinstance(exporter, briefcase.OTelExporter)
        assert exporter.protocol == "http"
        assert exporter._provider.resource.attributes["service.name"] == "checkout"
        span_exporter = exporter.span_exporter
        assert span_exporter._endpoint == "http://localhost:4318/v1/traces"
        candidates = [
            getattr(span_exporter, "_headers", None),
            getattr(getattr(span_exporter, "_client", None), "_headers", None),
            getattr(getattr(span_exporter, "_session", None), "headers", None),
        ]
        headers = [dict(c) for c in candidates if c]
        if not headers:
            pytest.skip("this OTLP exporter version does not expose its headers")
        assert any(h.get("x-api-key") == "abc123" for h in headers)
    finally:
        logging.disable(logging.CRITICAL)
        try:
            exporter._provider.shutdown()
        finally:
            logging.disable(logging.NOTSET)


def test_level_still_enables_logging(monkeypatch):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "memory")
    try:
        briefcase.observe(level="WARNING")
        assert logging.getLogger("briefcase").level == logging.WARNING
    finally:
        briefcase.disable_logging()
