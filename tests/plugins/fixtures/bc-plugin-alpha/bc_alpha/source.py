"""briefcase.receiver_sources: POST /v1/sources/alpha on `briefcase-ai receive`."""

BRIEFCASE_PLUGIN_API = 1


def normalize(payload):
    """One tool-call event per item in ``payload["calls"]``. Return an event, a list of
    events, or None. ``type`` and ``attrs`` use briefcase.semantic_conventions.cowork names:
    ``tool_result`` with ``tool_name``, ``success`` and ``duration_ms`` becomes a tool_call
    record."""
    if not isinstance(payload, dict):
        return None
    events = []
    for call in payload.get("calls", []):
        events.append({
            "type": "tool_result",
            "attrs": {
                "session.id": payload.get("session"),
                "tool_name": call.get("tool"),
                "success": call.get("ok", True),
                "duration_ms": call.get("ms"),
            },
        })
    return events
