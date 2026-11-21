"""Decode OTLP/HTTP request bodies into flat signals.

OTLP/JSON is parsed with the stdlib. OTLP/protobuf needs ``opentelemetry-proto``
(the ``receiver`` extra); it is converted to the same JSON shape first, so one
code path flattens both. Malformed input raises ``ValueError``.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

PROTOBUF_HINT = (
    "this receiver cannot decode application/x-protobuf: install the optional "
    "dependency with `pip install 'briefcase-ai[receiver]'`, or configure the "
    "sender for OTLP http/json"
)


class ProtobufUnavailable(Exception):
    pass


_REQUESTS = {
    "logs": ("opentelemetry.proto.collector.logs.v1.logs_service_pb2", "ExportLogsServiceRequest"),
    "metrics": ("opentelemetry.proto.collector.metrics.v1.metrics_service_pb2", "ExportMetricsServiceRequest"),
    "traces": ("opentelemetry.proto.collector.trace.v1.trace_service_pb2", "ExportTraceServiceRequest"),
}


def _proto(kind: str) -> Tuple[Any, Any]:
    """``(RequestClass, MessageToDict)``, imported lazily: both come from the
    optional ``receiver`` extra."""
    module, name = _REQUESTS[kind]
    try:
        request = getattr(importlib.import_module(module), name)
        to_dict = importlib.import_module("google.protobuf.json_format").MessageToDict
    except Exception as exc:
        raise ProtobufUnavailable(PROTOBUF_HINT) from exc
    return request, to_dict


def protobuf_available() -> bool:
    try:
        _proto("logs")
    except ProtobufUnavailable:
        return False
    return True


def decode_protobuf(kind: str, body: bytes) -> Dict[str, Any]:
    """Parse an Export{Logs,Metrics,Trace}ServiceRequest into the OTLP/JSON shape
    (trace/span ids stay base64; pass ``ids_base64=True`` when flattening)."""
    request, to_dict = _proto(kind)
    message = request()
    try:
        message.ParseFromString(body)
    except Exception as exc:
        raise ValueError(f"malformed OTLP protobuf body: {exc}")
    return to_dict(message)


def decode_json(body: bytes) -> Dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed OTLP JSON body: {exc}")
    if not isinstance(payload, dict):
        raise ValueError("OTLP JSON body must be an object")
    return payload


def any_value(value: Any) -> Any:
    """An OTLP AnyValue as a plain Python value."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [any_value(v) for v in (value["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in value:
        return attributes((value["kvlistValue"] or {}).get("values", []))
    return None


def attributes(items: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = any_value(item.get("value"))
    return out


def _nanos(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _hex_id(value: Any, ids_base64: bool, width: int) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if ids_base64:
        try:
            value = base64.b64decode(value).hex()
        except (binascii.Error, ValueError):
            return None
    value = value.lower()
    if len(value) != width or any(c not in "0123456789abcdef" for c in value) or not value.strip("0"):
        return None
    return value


def _dicts(value: Any) -> Iterator[Dict[str, Any]]:
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            yield item


def _resource(container: Dict[str, Any]) -> Dict[str, Any]:
    resource = container.get("resource")
    return attributes(resource.get("attributes")) if isinstance(resource, dict) else {}


def parse_logs(payload: Dict[str, Any], ids_base64: bool = False) -> List[Dict[str, Any]]:
    signals = []
    for resource_logs in _dicts(payload.get("resourceLogs")):
        resource = _resource(resource_logs)
        for scope_logs in _dicts(resource_logs.get("scopeLogs")):
            for record in _dicts(scope_logs.get("logRecords")):
                signals.append({
                    "kind": "log",
                    "resource": resource,
                    "attrs": attributes(record.get("attributes")),
                    "name": record.get("eventName") if isinstance(record.get("eventName"), str) else None,
                    "body": any_value(record.get("body")),
                    "time_ns": _nanos(record.get("timeUnixNano")) or _nanos(record.get("observedTimeUnixNano")),
                    "trace_id": _hex_id(record.get("traceId"), ids_base64, 32),
                    "span_id": _hex_id(record.get("spanId"), ids_base64, 16),
                })
    return signals


def parse_traces(payload: Dict[str, Any], ids_base64: bool = False) -> List[Dict[str, Any]]:
    signals = []
    for resource_spans in _dicts(payload.get("resourceSpans")):
        resource = _resource(resource_spans)
        for scope_spans in _dicts(resource_spans.get("scopeSpans")):
            for span in _dicts(scope_spans.get("spans")):
                status = span.get("status") if isinstance(span.get("status"), dict) else {}
                signals.append({
                    "kind": "span",
                    "resource": resource,
                    "attrs": attributes(span.get("attributes")),
                    "name": span.get("name") if isinstance(span.get("name"), str) else None,
                    "start_ns": _nanos(span.get("startTimeUnixNano")),
                    "time_ns": _nanos(span.get("endTimeUnixNano")),
                    "trace_id": _hex_id(span.get("traceId"), ids_base64, 32),
                    "span_id": _hex_id(span.get("spanId"), ids_base64, 16),
                    "parent_span_id": _hex_id(span.get("parentSpanId"), ids_base64, 16),
                    "status_code": (status or {}).get("code"),
                })
    return signals


def parse_metrics(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    signals = []
    for resource_metrics in _dicts(payload.get("resourceMetrics")):
        resource = _resource(resource_metrics)
        for scope_metrics in _dicts(resource_metrics.get("scopeMetrics")):
            for metric in _dicts(scope_metrics.get("metrics")):
                for shape in ("sum", "gauge", "histogram"):
                    data = metric.get(shape)
                    if not isinstance(data, dict):
                        continue
                    for point in _dicts(data.get("dataPoints")):
                        value: Any = None
                        if "asInt" in point or "asDouble" in point:
                            value = any_value({"intValue": point["asInt"]} if "asInt" in point
                                              else {"doubleValue": point["asDouble"]})
                        elif shape == "histogram":
                            value = any_value({"doubleValue": point.get("sum")})
                        signals.append({
                            "kind": "metric",
                            "resource": resource,
                            "attrs": attributes(point.get("attributes")),
                            "name": metric.get("name") if isinstance(metric.get("name"), str) else None,
                            "unit": metric.get("unit") if isinstance(metric.get("unit"), str) else None,
                            "value": value,
                            "time_ns": _nanos(point.get("timeUnixNano")),
                        })
    return signals


def parse(kind: str, payload: Dict[str, Any], ids_base64: bool = False) -> List[Dict[str, Any]]:
    if kind == "logs":
        return parse_logs(payload, ids_base64)
    if kind == "traces":
        return parse_traces(payload, ids_base64)
    return parse_metrics(payload)
