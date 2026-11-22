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
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SERVICE_NAME", "OTEL_SDK_DISABLED",
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

def test_otel_endpoint_defaults_to_the_specification_protocol(monkeypatch, fake_otel):
    # http/protobuf is the OpenTelemetry default; the base endpoint gets the
    # traces path appended, as the OTel SDK does.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exporter = briefcase.observe()
    assert isinstance(exporter, fake_otel)
    assert (exporter.endpoint, exporter.protocol) == ("http://collector:4318/v1/traces", "http")
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
    assert (exporter.endpoint, exporter.protocol) == ("http://localhost:4318/v1/traces", "http")


def test_missing_otel_extra_falls_back_to_console_with_a_warning(monkeypatch, mocker):
    import briefcase.exporters.otel as otel_module

    class Unavailable:
        def __init__(self, *args, **kwargs):
            raise ImportError("opentelemetry-api and opentelemetry-sdk are required.")

    monkeypatch.setattr(otel_module, "OTelExporter", Unavailable)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
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


# -- red team ---------------------------------------------------------------

def test_the_protocol_is_never_guessed_from_the_port(monkeypatch, fake_otel, caplog):
    """Port 4317 used to select gRPC. A port is a convention; with no
    protocol variable the OpenTelemetry default applies, and the mismatch
    is pointed out instead of acted on."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        exporter = briefcase.observe()
    assert (exporter.endpoint, exporter.protocol) == ("http://collector:4317/v1/traces", "http")
    assert "OTEL_EXPORTER_OTLP_PROTOCOL=grpc" in caplog.text


def test_the_traces_protocol_overrides_the_general_one(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    exporter = briefcase.observe()
    assert (exporter.endpoint, exporter.protocol) == ("http://collector:4317", "grpc")


@pytest.mark.parametrize("name,value", [
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4318"),          # no scheme
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "collector"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "ftp://collector:4318"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:notaport"),
    ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "//collector/v1/traces"),
])
def test_a_malformed_otlp_endpoint_fails_loudly(monkeypatch, fake_otel, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="not a valid http"):
        briefcase.observe()
    assert BriefcaseConfig.get().exporter is None  # nothing half-configured
    assert name in _observe.check_environment()


def test_an_unsupported_otlp_protocol_fails_loudly(monkeypatch, fake_otel):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
    with pytest.raises(ValueError, match="http/json"):
        briefcase.observe()
    assert "http/json" in _observe.check_environment()


def test_otel_sdk_disabled_exports_nothing_and_says_so(monkeypatch, fake_otel, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    with caplog.at_level(logging.WARNING, logger="briefcase"):
        exporter = briefcase.observe()
    assert not isinstance(exporter, (fake_otel, briefcase.ConsoleExporter))
    assert "OTEL_SDK_DISABLED=true" in caplog.text

    import asyncio

    assert asyncio.run(exporter.export({"decision_id": "x"})) is True
    # an explicit non-OTLP destination is not OpenTelemetry's to disable
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "memory")
    assert isinstance(briefcase.observe(), briefcase.MemoryExporter)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "otlp")
    assert isinstance(briefcase.observe(), fake_otel)


def test_secrets_never_reach_a_log_or_an_error_message(monkeypatch, fake_otel, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer HEADER-SECRET")
    monkeypatch.setenv("BRIEFCASE_EXPORTER_TOKEN", "TOKEN-SECRET")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://user:URL-SECRET@collector:4317")
    with caplog.at_level(logging.DEBUG, logger="briefcase"):
        briefcase.observe()
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "user:URL-SECRET@collector")
        with pytest.raises(ValueError) as otlp_error:
            briefcase.observe()
        monkeypatch.setenv("BRIEFCASE_EXPORTER", "htps://user:URL-SECRET@host/x?key=QUERY-SECRET")
        with pytest.raises(ValueError) as typo_error:
            briefcase.observe()
    text = caplog.text + str(otlp_error.value) + str(typo_error.value) + str(
        _observe.check_environment())
    for secret in ("HEADER-SECRET", "TOKEN-SECRET", "QUERY-SECRET"):
        assert secret not in text
    assert "URL-SECRET" not in caplog.text and "URL-SECRET" not in str(typo_error.value)
    assert _observe._safe_url("https://u:p@h:1/x?k=v") == "https://***@h:1/x?***"


@pytest.mark.parametrize("value", ["consol", "Console", "jsonl", "otlp-http", "s3://bucket/x"])
def test_a_typo_in_briefcase_exporter_is_an_error_not_the_console(monkeypatch, value):
    monkeypatch.setenv("BRIEFCASE_EXPORTER", value)
    with pytest.raises(ValueError, match="Unknown exporter shorthand"):
        briefcase.observe()
    assert BriefcaseConfig.get().exporter is None
    assert "BRIEFCASE_EXPORTER" in _observe.check_environment()


def test_check_environment_accepts_every_valid_destination(monkeypatch, tmp_path):
    for value in ("", "console", "memory", "otlp", "OTEL", str(tmp_path / "r.jsonl"),
                  "https://collector.example.com/v1/decisions"):
        assert _observe.check_environment({"BRIEFCASE_EXPORTER": value}) is None, value
    assert not (tmp_path / "r.jsonl").exists()  # nothing was opened
    assert _observe.check_environment(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
         "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}) is None


def test_strict_mode_raises_instead_of_falling_back_to_the_console(monkeypatch):
    import briefcase.exporters.otel as otel_module

    class Unavailable:
        def __init__(self, *args, **kwargs):
            raise ImportError("opentelemetry-sdk is required.")

    monkeypatch.setattr(otel_module, "OTelExporter", Unavailable)
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "otlp")
    with pytest.raises(ImportError, match=r"briefcase-ai\[otel\]"):
        _observe._exporter_from_env(strict=True)
