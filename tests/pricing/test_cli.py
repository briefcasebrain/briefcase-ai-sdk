import argparse
import io
import json
import sys

import pytest

from briefcase import pricing as pr
from briefcase.cli import pricing_commands as pc

URL = "https://acme.example/pricing"


def run(*argv):
    p = argparse.ArgumentParser(prog="briefcase")
    pc.register(p.add_subparsers(dest="command"))
    args = p.parse_args(["pricing", *argv])
    return args.func(args, None, None, None)


def test_contract_surface():
    assert pc.GROUP == "review" and callable(pc.register) and callable(pc.status)


def test_no_subcommand_is_a_usage_error(home, capsys):
    assert run() == 2
    assert "check" in capsys.readouterr().err


def test_list_and_json(home, capsys):
    assert run("list", "--provider", "amazon") == 0
    out = capsys.readouterr().out
    assert "nova-pro" in out and "gpt-4o" not in out and "Next:" in out
    assert run("list", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["models"]) == 39 and "UNVERIFIED" in data["notice"]
    assert run("list", "--provider", "nobody") == 1


def test_show_known_alias_and_unknown(home, capsys):
    assert run("show", "claude-3-5-haiku") == 0
    out = capsys.readouterr().out
    assert "claude-haiku-3-5" in out and "matched by alias" in out and "none (unverified)" in out
    assert "builtin-table@" in out and "layer   packaged" in out
    assert run("show", "gpt-5") == 1
    err = capsys.readouterr().err
    assert "known model with no price shipped" in err and "briefcase pricing set gpt-5 --input" in err
    assert run("show", "gpt-4oo", "--json") == 1
    info = json.loads(capsys.readouterr().out)
    assert "gpt-4o" in info["closest"] and info["known_unpriced"] is False


def test_set_requires_source_url_and_shows_diff_then_writes(home, capsys):
    with pytest.raises(SystemExit):
        run("set", "m", "--input", "1", "--output", "2")  # argparse: --source-url required
    capsys.readouterr()
    assert run("set", "m", "--input", "1", "--output", "2", "--source-url", "ftp://x") == 2
    capsys.readouterr()
    # new model needs context window and provider
    assert run("set", "acme-large", "--input", "2.5", "--output", "10", "--source-url", URL, "--yes") == 2
    assert "--context-window N and --provider NAME is required" in capsys.readouterr().err
    f = home / ".briefcase" / "pricing.json"
    assert run("set", "acme-large", "--input", "2.5", "--output", "10", "--context-window", "128000",
               "--provider", "acme", "--source-url", URL, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "+++" in out and '"acme-large"' in out and not f.exists()
    assert run("set", "acme-large", "--input", "2.5", "--output", "10", "--context-window", "128000",
               "--provider", "acme", "--source-url", URL, "--cached-input", "1.25", "--yes",
               ) == 0
    assert "Next: briefcase pricing show acme-large" in capsys.readouterr().out
    entry = json.loads(f.read_text())["models"][0]
    assert entry["input_cost_per_1k_tokens"] == 2.5 / 1000.0 and entry["source_url"] == URL
    assert entry["verified_on"] and entry["rate_card_extras"]["cache_rates"]["read_per_mtok"] == 1.25
    t = pr.load()
    assert t.estimate("acme-large", 1000, 1000)["total_cost"] == pytest.approx(0.0125)
    assert run("show", "acme-large") == 0
    assert f"layer   user ({f})" in capsys.readouterr().out
    # override a packaged model inherits its context window and provider
    assert run("set", "gpt-4", "--input", "30", "--output", "60", "--source-url", URL, "--yes") == 0
    assert pr.load().models["gpt-4"]["layer"] == "user"
    capsys.readouterr()
    assert run("unset", "gpt-4", "--yes") == 0 and pr.load().models["gpt-4"]["layer"] == "packaged"
    assert run("unset", "gpt-4", "--yes") == 1


def test_set_asks_and_refuses_without_a_terminal(home, capsys, monkeypatch):
    args = ["set", "gpt-4", "--input", "30", "--output", "60", "--source-url", URL]
    assert run(*args) == 1  # stdin is not a tty under pytest
    assert "Re-run with --yes" in capsys.readouterr().err

    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Tty("n\n"))
    assert run(*args) == 1 and "Cancelled" in capsys.readouterr().out
    assert not (home / ".briefcase" / "pricing.json").exists()
    monkeypatch.setattr(sys, "stdin", Tty("y\n"))
    assert run(*args) == 0 and (home / ".briefcase" / "pricing.json").exists()


def test_import(home, tmp_path, capsys):
    src = tmp_path / "team.json"
    src.write_text(json.dumps({"models": [
        {"model_name": "team-a", "provider": "t", "input_cost_per_1k_tokens": 0.001,
         "output_cost_per_1k_tokens": 0.002, "context_window": 1000}]}))
    assert run("import", str(src), "--yes") == 0
    out = capsys.readouterr().out
    assert "no source_url" in out and "Wrote" in out
    assert "team-a" in pr.load().models
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"models": [{"model_name": "x", "provider": "p", "input_cost_per_1k_tokens": -1,
                                           "output_cost_per_1k_tokens": 1, "context_window": 1}]}))
    assert run("import", str(bad), "--yes") == 1
    assert f"{bad}:" in capsys.readouterr().err


def test_check_records_reports_unpriced_models(home, tmp_path, capsys):
    rec = tmp_path / "runs.jsonl"
    rec.write_text("\n".join(json.dumps({"inputs": {"model": m}}) for m in
                             ["gpt-4o", "gpt-4o", "o3", "o3", "o3", "llama-3.1-70b", "mystery-1"]))
    assert run("check", "--records", str(rec)) == 0
    out = capsys.readouterr().out
    assert "o3  (3 records)  [known model, no price shipped]" in out
    assert "mystery-1  (1 records)" in out and "gpt-4o  (" not in out
    assert "briefcase pricing set o3 --input" in out
    assert "39 of 39 models have no provenance" in out and "Next: briefcase pricing set o3" in out
    assert run("check", "--records", str(rec), "--strict") == 1
    capsys.readouterr()
    assert run("check", "--records", str(rec), "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert [u["model"] for u in report["unpriced"]] == ["o3", "llama-3.1-70b", "mystery-1"]
    assert run("check", "--records", str(tmp_path / "missing.jsonl")) == 1


def test_check_flags_age_and_invalid_layers(home, monkeypatch, capsys):
    import datetime
    monkeypatch.setattr(pc, "_today", lambda: datetime.date(2027, 6, 1))
    assert run("check") == 0
    assert "packaged data is" in capsys.readouterr().out
    f = home / ".briefcase" / "pricing.json"
    f.parent.mkdir()
    f.write_text('{"models": [{"model_name": "x", "provider": "p", "input_cost_per_1k_tokens": -1,\n'
                 '"output_cost_per_1k_tokens": 1, "context_window": 5}]}')
    assert run("check") == 1
    err = capsys.readouterr().err
    assert f"{f}:1: model 'x': input_cost_per_1k_tokens must be finite" in err
    assert run("list") == 1  # other commands refuse a broken layer with the same errors
    assert "Pricing files are invalid" in capsys.readouterr().err


def test_status_hook(home, monkeypatch):
    import datetime
    monkeypatch.setattr(pc, "_today", lambda: datetime.date.fromisoformat(pr.load().generated_at))
    s = pc.status()
    assert s["state"] == "ok" and s["summary"] == "39 models priced, 39 unverified, data is 0 days old"
    rec = home / ".briefcase" / "receiver" / "records.jsonl"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"inputs": {"model": "o3"}}) + "\n")
    s = pc.status()
    assert s["state"] == "attention" and s["unpriced"] == ["o3"] and "o3" in s["summary"]
    assert "pricing check --records" in s["next"]
