"""briefcase.connect_adapters: `briefcase-ai connect alphatool`."""
from typing import Any, List

from briefcase.connect.adapters import Adapter, Context
from briefcase.connect.edits import Manual

BRIEFCASE_PLUGIN_API = 1


class AlphaToolAdapter(Adapter):
    id = "alphatool"
    # The keys of an adapters.json entry. Only a format checked against the tool's own
    # documentation may be written; anything else is a Manual item (printed, never written).
    DATA = {
        "name": "Alpha Tool",
        "mode": "print-only",
        "docs_url": "https://example.invalid/alpha-tool/telemetry",
        "verified_on": "2026-11-22",
        "detect": {"dirs": ["~/.alphatool"], "bins": ["alphatool"], "globs": []},
        "files": {},
    }

    def _plan(self, ctx: Context) -> List[Any]:
        if not ctx.telemetry:
            return []
        env = {"ALPHATOOL_OTLP_ENDPOINT": ctx.endpoint}
        return [Manual(self.id, "Set in the environment Alpha Tool starts from",
                       self.env_exports(env), "example adapter: printed, never written")]
