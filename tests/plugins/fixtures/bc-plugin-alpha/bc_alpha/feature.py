"""briefcase.features: a manifest `briefcase-ai features` lists (same keys as briefcase/_features/*.json)."""

BRIEFCASE_PLUGIN_API = 1

MANIFEST = {
    "id": "alpha-export",
    "title": "Alpha exporter",
    "verb": "capture",
    "one_line": "Send decision records to Alpha.",
    "command": "briefcase-ai alpha hello",
    "docs_slug": "capture/alpha-export",
    "status": "experimental",
    "requires_extra": None,
    "verified_against": "tests/plugins only",
    "docs_path": "https://example.invalid/bc-plugin-alpha#readme",
}
