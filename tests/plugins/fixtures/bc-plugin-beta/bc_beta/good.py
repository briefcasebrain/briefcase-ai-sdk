"""An exporter offered under names that are already taken."""
from briefcase.exporters.base import BaseExporter


class BetaExporter(BaseExporter):
    async def export(self, decision) -> bool:
        return True

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


NOT_A_CLASS = "a guardrail entry point must name a class"
