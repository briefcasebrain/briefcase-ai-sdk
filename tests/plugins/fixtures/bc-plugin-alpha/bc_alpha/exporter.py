"""briefcase.exporters: briefcase.observe("alpha:/tmp/out.jsonl") or BRIEFCASE_EXPORTER=alpha:/tmp/out.jsonl."""
import json

from briefcase.exporters.base import BaseExporter, get_field

BRIEFCASE_PLUGIN_API = 1


class AlphaExporter(BaseExporter):
    """Appends one line per decision record: its id and function name."""

    def __init__(self, path: str = "alpha.jsonl") -> None:
        self.path = path

    @classmethod
    def from_target(cls, target: str) -> "AlphaExporter":
        # Everything after "alpha:" in the shorthand.
        return cls(target or "alpha.jsonl")

    async def export(self, decision) -> bool:
        line = {"id": get_field(decision, "decision_id"), "fn": get_field(decision, "function_name")}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        return True

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None
