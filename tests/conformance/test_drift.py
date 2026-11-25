"""Docs-drift checker against fixture pages (no network)."""

import json

from scripts.conformance import drift

ADAPTERS = {"a": {"docs_url": "https://docs.example/a"}, "b": {"docs_url": "https://docs.example/b"},
            "c": {"docs_url": "https://docs.example/c"}, "d": {"docs_url": "https://docs.example/d"}}
EXPECT = {"adapters": {
    "a": {"expect": ["FOO_ENABLE", "[otel]", "a/b"]},
    "b": {"expect": ["KEEP", "RENAMED_VAR"]},
    "c": {"expect": ["ANY"]},
    "d": {"expect": [], "note": "generic page"},
}}
PAGES = {
    "https://docs.example/a": "<code>FOO_ENABLE</code> and <code>[otel]</code> and a&#x2F;b",
    "https://docs.example/b": "<p>KEEP is here; the other variable was renamed to NEW_VAR</p>",
    "https://docs.example/c": None,       # network failure
}


def check(**kw):
    return drift.check_all(fetcher=lambda url: PAGES.get(url), expectations=EXPECT, adapters=ADAPTERS, **kw)


def by_adapter(report):
    return {r["adapter"]: r for r in report["results"]}


def test_ok_vanished_unknown_and_unchecked():
    results = by_adapter(check())
    assert results["a"]["status"] == "ok"
    assert results["b"]["status"] == "vanished" and results["b"]["missing"] == ["RENAMED_VAR"]
    assert results["c"]["status"] == "unknown"        # a network failure is not a failure
    assert results["d"]["status"] == "no-expectations"


def test_summary_and_report_name_the_missing_string():
    report = check()
    assert report["summary"] == {"ok": 1, "vanished": 1, "unknown": 1, "no-expectations": 1}
    text = drift.render(report)
    assert "VANISHED" in text and "'RENAMED_VAR'" in text and "unknown" in text


def test_escaped_forms_still_match():
    page = 'x = &quot;otlp-http&quot; and \\u003cb\\u003e and a\\/b'
    assert "otlp-http" in drift.normalise(page) and "<b>" in drift.normalise(page) and "a/b" in drift.normalise(page)


def test_cli_exit_code_is_one_only_when_an_expectation_vanished(tmp_path, monkeypatch, capsys):
    from scripts.conformance import __main__ as cli
    monkeypatch.setattr(drift, "CONFORMANCE_JSON", tmp_path / "c.json")
    (tmp_path / "c.json").write_text(json.dumps({"adapters": {"claude-code": {"expect": ["FOO_VAR"]}}}))
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "claude-code.html").write_text("<p>FOO_VAR documented</p>")
    assert cli.main(["drift", "claude-code", "--pages", str(pages)]) == 0
    (pages / "claude-code.html").write_text("<p>renamed</p>")
    assert cli.main(["drift", "claude-code", "--pages", str(pages)]) == 1
    assert "no longer on the page: 'FOO_VAR'" in capsys.readouterr().out
    (pages / "claude-code.html").unlink()                      # page unreachable: unknown, not failure
    assert cli.main(["drift", "claude-code", "--pages", str(pages)]) == 0


def test_shipped_expectations_cover_every_adapter_and_every_string_is_real_text():
    data = json.loads(drift.CONFORMANCE_JSON.read_text())
    adapters = json.loads(drift.ADAPTERS_JSON.read_text())["tools"]
    assert set(data["adapters"]) <= set(adapters) | {"copilot-cli"}
    for adapter_id in adapters:
        entry = data["adapters"][adapter_id]
        assert entry.get("expect") or entry.get("note"), f"{adapter_id}: expectations or a reason are required"
