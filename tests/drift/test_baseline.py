"""Baseline profiles: no content, signals, small-n refusal, and the CLI gate."""

import argparse
import json
import random

import pytest

from briefcase.cli import drift_commands
from briefcase.drift import baseline as bl

SECRET = "SECRET-OUTPUT-4b1e"


def rec(i, latency, label, *, model="gpt-a", status="ok", finish="stop", tokens=5,
        hour=0, prompt=SECRET):
    return {
        "decision_type": "llm.openai.chat", "decision_id": f"d{i}",
        "execution_time_ms": latency, "status": status,
        "token_usage": {"prompt_tokens": 100, "completion_tokens": tokens, "total_tokens": 100 + tokens},
        "inputs": {"messages": [{"role": "user", "content": prompt}]},
        "outputs": {"content": label}, "finish_reason": finish,
        "model_parameters": {"provider": "openai", "model_name": model},
        "started_at": f"2026-11-{1 + hour // 24:02d}T{hour % 24:02d}:{i % 60:02d}:00+00:00",
    }


def baseline_records(n=200, seed=0):
    r = random.Random(seed)
    return [rec(i, r.gauss(1200, 100), r.choice(["positive", "negative", "neutral"]), hour=i % 48)
            for i in range(n)]


def drifted_records(n=120, seed=1):
    r = random.Random(seed)
    return [rec(i, r.gauss(2900, 300), r.choice(["positive", "negative", "unsure"]),
                model=r.choice(["gpt-a", "gpt-b"]),
                status="error" if r.random() < 0.15 else "ok", hour=i % 48) for i in range(n)]


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_profile_holds_no_content():
    records = baseline_records()
    for r in records:
        r["outputs"]["content"] = r["outputs"]["content"] + " " + SECRET
    profile = bl.build_profile(records, "p", embed=lambda texts: [[1.0, 0.0] for _ in texts])
    blob = json.dumps(profile)
    assert SECRET not in blob
    for word in ("positive", "negative", "neutral"):
        assert word not in blob
    assert profile["embedding"]["centroid"] == [1.0, 0.0]


def test_categorical_labels_are_hashed():
    profile = bl.build_profile(baseline_records(), "p")
    cats = profile["categories"]
    assert len(cats["counts"]) == 3
    assert bl.label_hash("Positive.") in cats["counts"]  # normalized before hashing
    assert "positive" not in json.dumps(cats)


def test_same_distribution_is_ok_and_drift_is_flagged():
    profile = bl.build_profile(baseline_records(400, seed=0), "p")
    same = bl.check(profile, baseline_records(150, seed=7))
    assert same["state"] == "ok", same["attention"]

    result = bl.check(profile, drifted_records())
    flagged = set(result["attention"])
    assert {"latency_ms.psi", "latency_ms.ks", "error.rate", "model_mix",
            "output_categories"} <= flagged
    latency = next(s for s in result["signals"] if s["signal"] == "latency_ms.psi")
    assert latency["reading"].startswith("p95 latency rose from 1.")
    assert " s to 3." in latency["reading"] or " s to 2." in latency["reading"]
    assert latency["n_baseline"] == 400 and latency["n_window"] == 120
    assert latency["threshold"] == bl.DEFAULT_THRESHOLDS["psi"]
    error = next(s for s in result["signals"] if s["signal"] == "error.rate")
    assert "two-proportion z" in error["reading"]
    mix = next(s for s in result["signals"] if s["signal"] == "model_mix")
    assert mix["details"]["new_models"] == ["gpt-b"]


def test_small_samples_are_refused_not_scored():
    profile = bl.build_profile(baseline_records(200), "p")
    result = bl.check(profile, drifted_records(12))
    assert result["state"] == "insufficient_data"
    scored = [s for s in result["signals"] if s["state"] in ("ok", "attention")]
    assert scored == []
    psi = next(s for s in result["signals"] if s["signal"] == "latency_ms.psi")
    assert "needs at least 30" in psi["reading"] and "12 in the window" in psi["reading"]
    lowered = bl.check(profile, drifted_records(12), thresholds={"min_n": 10})
    assert lowered["state"] == "attention"


def test_lexical_signal_and_embed_hook():
    rng = random.Random(3)
    words = "the cat sat on a mat while dogs ran in the park".split()
    base = [rec(i, 100, " ".join(rng.choice(words) for _ in range(12))) for i in range(120)]
    other = "invoice total amount due payment received thank you account".split()
    window = [rec(i, 100, " ".join(rng.choice(other) for _ in range(12))) for i in range(60)]

    def embed(texts):
        return [[1.0, 0.0] if "cat" in t or "the" in t else [0.0, 1.0] for t in texts]

    profile = bl.build_profile(base, "p", embed=embed)
    assert profile["categories"] is None  # free text, not labels
    result = bl.check(profile, window, embed=embed)
    by = {s["signal"]: s for s in result["signals"]}
    assert by["output_lexical"]["state"] == "attention"
    assert by["output_embedding"]["state"] == "attention"
    assert bl.check(profile, base[:60], embed=embed)["state"] == "ok"
    assert {s["signal"]: s for s in bl.check(profile, window)["signals"]}[
        "output_embedding"]["state"] == "unavailable"


def test_select_records_where_time_and_last(tmp_path):
    path = tmp_path / "runs.jsonl"
    records = [rec(i, 100, "x", model="gpt-a" if i % 2 else "gpt-b", hour=i) for i in range(10)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "garbage\n")
    assert len(bl.select_records(path)) == 10
    assert len(bl.select_records(path, where={"model_parameters.model_name": "gpt-a"})) == 5
    assert len(bl.select_records(path, where={"model_parameters.model_name": "gpt-*"})) == 10
    assert [r["decision_id"] for r in bl.select_records(path, last=3)] == ["d7", "d8", "d9"]
    assert len(bl.select_records(path, since="2026-11-01T05:00:00Z")) == 5
    assert len(bl.select_records(path, since="2026-11-01T05:00:00Z",
                                 until="2026-11-01T06:30:00Z")) == 2


def test_profile_storage_round_trip(home):
    profile = bl.build_profile(baseline_records(40), "prod")
    path = bl.save_profile(profile)
    assert path == home / "baselines" / "prod.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert bl.load_profile("prod")["n"] == 40
    assert [p["name"] for p in bl.list_profiles()] == ["prod"]
    with pytest.raises(ValueError):
        bl.profile_path("../evil")


# -- CLI ---------------------------------------------------------------------

def cli(argv):
    parser = argparse.ArgumentParser(prog="briefcase")
    sub = parser.add_subparsers(dest="command")
    drift_commands.register(sub)
    args = parser.parse_args(argv)
    return args.func(args, None, None, None)


def write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


def test_cli_gate_exit_codes(tmp_path, capsys):
    base = write(tmp_path / "base.jsonl", baseline_records(300))
    same = write(tmp_path / "same.jsonl", baseline_records(150, seed=9))
    drifted = write(tmp_path / "drift.jsonl", drifted_records())
    small = write(tmp_path / "small.jsonl", drifted_records(10))

    assert drift_commands.GROUP == "review"
    assert drift_commands.status()["state"] == "off"
    assert cli(["drift", "baseline", "save", "prod", "--from", base]) == 0
    out = capsys.readouterr().out
    assert "Saved baseline 'prod': 300 records" in out and "Next: briefcase drift check" in out

    assert cli(["drift", "check", "prod", "--from", same, "--fail-on", "attention"]) == 0
    assert cli(["drift", "check", "prod", "--from", drifted, "--fail-on", "attention"]) == 1
    out = capsys.readouterr().out
    assert "ATTENTION" in out and "p95 latency rose" in out
    assert drift_commands.status()["state"] == "attention"
    assert cli(["drift", "check", "prod", "--from", drifted]) == 0  # default: report only

    assert cli(["drift", "check", "prod", "--from", small, "--fail-on", "attention"]) == 0
    assert "Nothing was scored" in capsys.readouterr().out
    assert cli(["drift", "check", "prod", "--from", small, "--fail-on", "insufficient"]) == 1
    capsys.readouterr()

    assert cli(["drift", "check", "prod", "--from", drifted, "--json",
                "--threshold", "psi=100", "--threshold", "ks=1", "--threshold", "rate_alpha=0",
                "--threshold", "model_mix_jsd=1", "--threshold", "model_new_share=1",
                "--threshold", "category_jsd=1", "--threshold", "lexical_jsd=1",
                "--fail-on", "attention"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["thresholds"]["psi"] == 100


def test_cli_errors_and_listing(tmp_path, capsys):
    assert cli(["drift", "check", "missing", "--from", "x"]) == 1
    assert "drift baseline save missing" in capsys.readouterr().err
    assert cli(["drift", "baselines"]) == 0
    assert "No baselines" in capsys.readouterr().out
    base = write(tmp_path / "base.jsonl", baseline_records(50))
    assert cli(["drift", "baseline", "save", "bad/name", "--from", base]) == 1
    assert cli(["drift", "baseline", "save", "p2", "--from", base, "--where",
                "model_parameters.model_name=nothing"]) == 1
    assert cli(["drift", "baseline", "save", "p2", "--from", base, "--json"]) == 0
    capsys.readouterr()
    assert cli(["drift", "check", "p2", "--from", base, "--threshold", "nope=1"]) == 1
    assert "unknown threshold" in capsys.readouterr().err
    assert cli(["drift", "baselines", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [b["name"] for b in listed["baselines"]] == ["p2"]
    saved = (tmp_path / "home" / "baselines" / "p2.json").read_text()
    assert str(tmp_path) not in saved  # host path dropped: basename only
