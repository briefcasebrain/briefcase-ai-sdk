"""Datasets and evals end to end on provider records built from the specs.

Each record is produced by the real provider instrumentation (content on)
from a response body generated from that provider's spec (OpenAI chat,
Anthropic Messages, Bedrock Converse), so the records have exactly the
shape production records have. Then ``briefcase dataset from-records`` turns
them into a dataset and ``briefcase eval run`` scores a deterministic target
against it with the ``exact`` and ``json`` scorers. No model is involved.
"""

import argparse
import json
import textwrap

import pytest

pytest.importorskip("openai")
pytest.importorskip("anthropic")
pytest.importorskip("boto3")

from briefcase.cli import __main__ as cli_main  # noqa: E402
from briefcase.cli import dataset_versions, eval_commands  # noqa: E402
from briefcase.cli.state import Store  # noqa: E402
from briefcase.datasets import DatasetStore  # noqa: E402

from tests.contracts import _specs  # noqa: E402
from tests.contracts._examples import JsonSchemaExamples, ShapeExamples  # noqa: E402
from tests.contracts._harness import (  # noqa: E402
    anthropic_client, bedrock_wire_client, json_body, jsonify, openai_client,
)

#: (provider, ticket, the label the recorded model answered, as JSON text)
TICKETS = [
    ("openai", "The app crashes on login", {"label": "bug"}),
    ("openai", "How do I export my data?", {"label": "question"}),
    ("openai", "I was charged twice", {"label": "billing"}),
    ("anthropic", "Error 500 when saving", {"label": "bug"}),
    ("anthropic", "Can I get a refund?", {"label": "billing"}),
    ("bedrock", "Where is the API key page?", {"label": "question"}),
]


def _openai_body(answer):
    op = _specs.openai_operation("/chat/completions")
    body = JsonSchemaExamples(_specs.openai_spec().doc).generate({"$ref": "#" + op["json"]})
    message = body["choices"][0]["message"]
    message.update(content=answer, refusal=None, tool_calls=None)
    assert _specs.openai_spec().errors(op["json"], body) == []
    return body


def _anthropic_body(answer):
    doc = _specs.anthropic_spec()
    body = JsonSchemaExamples(doc.doc, prefer=("text",)).generate({"$ref": "#/$defs/Message"})
    body["content"] = [dict(body["content"][0], text=answer, citations=None)]
    assert doc.errors("/$defs/Message", body) == []
    return body


def _bedrock_body(answer):
    shape = _specs.bedrock_service().operation_model("Converse").output_shape
    body = ShapeExamples(prefer=("text",)).generate(shape)
    body["output"]["message"] = {"role": "assistant", "content": [{"text": answer}]}
    return json.dumps(jsonify(body)).encode()


@pytest.fixture
def provider_records(tmp_path, instrument, mem):
    for provider in ("openai", "anthropic", "bedrock"):
        instrument(provider)
    for provider, ticket, label in TICKETS:
        answer = json.dumps(label)
        if provider == "openai":
            client, _ = openai_client(json_body(_openai_body(answer)))
            client.chat.completions.create(
                model="gpt-contract-1", messages=[{"role": "user", "content": ticket}])
        elif provider == "anthropic":
            client, _ = anthropic_client(json_body(_anthropic_body(answer)))
            client.messages.create(model="claude-contract-1", max_tokens=16,
                                   messages=[{"role": "user", "content": ticket}])
        else:
            client, _ = bedrock_wire_client(_bedrock_body(answer))
            client.converse(modelId="anthropic.claude-contract-1",
                            messages=[{"role": "user", "content": [{"text": ticket}]}])
    assert len(mem.records) == len(TICKETS)
    path = tmp_path / "runs.jsonl"
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in mem.records))
    return path


@pytest.fixture
def target(tmp_path, monkeypatch):
    """A deterministic stand-in for the model: reads the ticket out of the
    recorded request (whatever the provider's shape) and labels it."""
    mod = tmp_path / "contract_target.py"
    mod.write_text(textwrap.dedent('''\
        import json

        LABELS = {
            "The app crashes on login": "bug",
            "How do I export my data?": "question",
            "I was charged twice": "bug",            # disagrees with the recording
            "Error 500 when saving": "bug",
            "Can I get a refund?": "billing",
            "Where is the API key page?": "question",
        }

        def ticket(request):
            content = request["messages"][-1]["content"]
            return content if isinstance(content, str) else content[0]["text"]

        def classify(request):
            # Same JSON value as the recorded answer, different spacing:
            # json passes where exact does not.
            return json.dumps({"label": LABELS[ticket(request)]}, separators=(",", ":"))

        def classify_exact(request):
            return json.dumps({"label": LABELS[ticket(request)]})
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    return "contract_target"


@pytest.fixture(autouse=True)
def no_native_sanitizer(monkeypatch):
    monkeypatch.setattr(dataset_versions, "sanitizer_factory", lambda: None)


def _subparsers(parser):
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def run(argv, home, monkeypatch, capsys):
    builtin = cli_main.build_parser

    def parser():
        p = builtin()
        dataset_versions.extend_from_top_level(_subparsers(p))
        eval_commands.register(_subparsers(p))
        return p

    monkeypatch.setattr(cli_main, "build_parser", parser)
    rc = cli_main.main(argv, store=Store(home=home))
    out = capsys.readouterr()
    monkeypatch.setattr(cli_main, "build_parser", builtin)
    return rc, out.out, out.err


def test_dataset_from_spec_generated_provider_records_then_eval(
        tmp_path, provider_records, target, monkeypatch, capsys):
    rc, out, err = run(["dataset", "from-records", str(provider_records), "--name", "contracts",
                        "--allow-unsanitized", "--where", "status=ok"], tmp_path, monkeypatch, capsys)
    assert rc == 0, out + err
    version = DatasetStore(tmp_path).get("contracts")
    assert len(version.cases) == len(TICKETS)
    references = sorted(case["reference"] for case in version.cases)
    assert references == sorted(json.dumps(label) for _, _, label in TICKETS)
    for case in version.cases:
        assert case["input"]["messages"][-1]["role"] == "user"

    def summary(*extra):
        rc, out, err = run(["eval", "run", "--dataset", "contracts", "--json",
                            "--to", str(tmp_path / "evals.jsonl"), *extra], tmp_path, monkeypatch, capsys)
        return rc, json.loads(out)

    rc, result = summary("--target", f"{target}:classify", "--scorer", "exact")
    assert result["targets"][0]["passed"] == 0  # same values, different JSON text
    rc, result = summary("--target", f"{target}:classify", "--scorer", "json")
    assert result["targets"][0]["passed"] == len(TICKETS) - 1
    worst = result["targets"][0]["worst"][0]
    assert json.loads(worst["expected"]) == {"label": "billing"}
    assert json.loads(worst["got"]) == {"label": "bug"}
    rc, result = summary("--target", f"{target}:classify_exact", "--scorer", "exact",
                         "--fail-under", "0.8")
    assert rc == 0, result
    assert result["targets"][0]["passed"] == len(TICKETS) - 1
    assert result["targets"][0]["pass_rate"] == pytest.approx(5 / 6)
    rc, result = summary("--target", f"{target}:classify_exact", "--scorer", "exact",
                         "--fail-under", "0.9")
    assert rc == 1

    lines = [json.loads(line) for line in (tmp_path / "evals.jsonl").read_text().splitlines()]
    cases = [r for r in lines if r["decision_type"] == "eval.case"]
    assert len(cases) == 4 * len(TICKETS)
    # Eval records hold digests unless --capture-content: the tickets are not in them.
    assert "charged twice" not in (tmp_path / "evals.jsonl").read_text()
