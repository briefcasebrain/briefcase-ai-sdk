"""Cross-language checks run by test/contract.test.ts (skipped when Python
or the briefcase package is not importable). Reads a JSON request on stdin:

  {"op": "otel_attrs", "record": {...}}       -> Python OTelExporter span for a TS record
  {"op": "otlp_parse", "body": {...}}         -> briefcase.receiver.otlp.parse("traces", body)
  {"op": "http_post", "url": ..., "token": ..., "record": {...}}
                                              -> Python HTTPExporter POSTs the record to url
"""
import asyncio
import json
import sys


def main() -> None:
    request = json.load(sys.stdin)
    op = request["op"]
    if op == "otel_attrs":
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from briefcase.exporters.otel import OTelExporter

        memory = InMemorySpanExporter()
        exporter = OTelExporter(span_exporter=memory, service_name="briefcase-ai")
        ok = asyncio.run(exporter.export(request["record"]))
        asyncio.run(exporter.flush())
        spans = memory.get_finished_spans()
        span = spans[0]
        out = {
            "ok": ok,
            "name": span.name,
            "kind": span.kind.value,
            "attributes": dict(span.attributes),
            "status": span.status.status_code.name,
            "trace_id": format(span.context.trace_id, "032x"),
            "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
            "start": str(span.start_time),
            "end": str(span.end_time),
            "resource": dict(span.resource.attributes),
            "scope": span.instrumentation_scope.name,
        }
    elif op == "otlp_parse":
        from briefcase.receiver import otlp

        out = {"signals": otlp.parse("traces", request["body"])}
    elif op == "http_post":
        from briefcase.exporters.http import HTTPExporter

        exporter = HTTPExporter(request["url"], token=request["token"], flush_interval=None, max_retries=0)
        asyncio.run(exporter.export(request["record"]))
        asyncio.run(exporter.close())
        out = {"sent": exporter.sent_records}
    else:
        raise SystemExit(f"unknown op {op}")
    json.dump(out, sys.stdout, default=str)


if __name__ == "__main__":
    main()
