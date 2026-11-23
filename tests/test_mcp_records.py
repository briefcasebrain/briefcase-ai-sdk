"""The briefcase-ai-mcp tools over recorded decisions.

Fixture records are produced by the real code paths where one exists: the receiver turns the
captured Claude Code 2.1.330 OTLP payload and the synthetic coding-tool payloads into records,
and its policy endpoint turns hook decisions into ``policy.tool_call`` records. Provider and
framework records follow the documented layout (``parent_run_id`` on the provider record).
The hash chain is written with ``HashChainAppender`` and then tampered with.

Every tool is called through an in-process MCP client session against ``build_server()``,
plus direct calls for the path-confinement and pagination edge cases.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="briefcase-ai[mcp] not installed")

from briefcase.mcp import records as R  # noqa: E402
from briefcase.mcp.server import RECORD_TOOLS, build_server  # noqa: E402

REAL_LOGS = (Path(__file__).resolve().parent / "receiver" / "fixtures" / "real" / "claude-code" / "2.1.330"
             / "00-logs.json")
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


# -- fixtures ---------------------------------------------------------------------------------

def _iso(offset_ms):
    return (T0 + timedelta(milliseconds=offset_ms)).isoformat()


def _receiver_records():
    from briefcase.exporters.memory import MemoryExporter
    from briefcase.receiver.server import Receiver
    from tests.receiver import payloads

    memory = MemoryExporter()
    receiver = Receiver(memory, async_export=False)
    receiver.handle_post("/v1/logs", "application/json", REAL_LOGS.read_bytes())
    receiver.handle_post("/v1/logs", "application/json", json.dumps(payloads.claude_code_logs()).encode())
    deny = {"tool": "claude-code", "effect": "deny", "would": "deny", "mode": "enforce", "kind": "shell",
            "event": "PreToolUse", "tool_name": "Bash", "session": "sess-0001", "rule": "shell.rm-root",
            "source": "preset", "reason": "Deleting the root or home directory is never intended.",
            "target_hmac_sha256": "a" * 64, "ms": 2}
    receiver.handle_post("/v1/policy/decisions", "application/json", json.dumps(deny).encode())
    observe = dict(deny, effect="allow", would="ask", mode="observe", rule="pkg.install",
                   reason="Installing packages runs third-party code.")
    receiver.handle_post("/v1/policy/decisions", "application/json", json.dumps(observe).encode())
    receiver.close()
    # One decision whose target was captured (hook --capture-content + receiver --keep-content).
    keep = Receiver(memory, async_export=False, keep_content=True)
    captured = dict(deny, target="rm -rf /", session="sess-0002")
    keep.handle_post("/v1/policy/decisions", "application/json", json.dumps(captured).encode())
    return list(memory.records)


def _provider_records():
    """A LangChain-style chain run with an LLM run under it, and the provider record of the same
    model call nested under the LLM run by parent_run_id (briefcase.auto(dedupe="none"))."""
    usage = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}
    chain = {"schema_version": "1.2", "decision_id": "run-chain", "decision_type": "chain",
             "function_name": "RunnableSequence", "inputs": {}, "outputs": {}, "status": "ok",
             "started_at": _iso(0), "ended_at": _iso(2000), "execution_time_ms": 2000.0,
             "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "workflow_id": "wf-1"}
    llm = {"schema_version": "1.2", "decision_id": "run-llm", "decision_type": "llm",
           "function_name": "ChatOpenAI", "inputs": {}, "outputs": {}, "status": "ok",
           "parent_run_id": "run-chain", "model_parameters": {"model_name": "gpt-4o"},
           "token_usage": usage, "started_at": _iso(100), "ended_at": _iso(1900), "execution_time_ms": 1800.0,
           "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}
    provider = {"schema_version": "1.2", "decision_id": "prov-1", "decision_type": "llm.openai.chat",
                "function_name": "openai.chat.completions.create", "status": "ok",
                "parent_run_id": "run-llm", "parent_framework": "langchain",
                "inputs": {"messages": [{"role": "user", "content": "SECRET-PROMPT summarise the memo"}]},
                "outputs": {"content": "SECRET-RESPONSE " + "x" * 5000},
                "model_parameters": {"provider": "openai", "model_name": "gpt-4o", "max_tokens": 400},
                "token_usage": usage, "finish_reason": "stop", "stream": False,
                "started_at": _iso(150), "ended_at": _iso(1850), "execution_time_ms": 1700.0}
    failed = {"schema_version": "1.2", "decision_id": "prov-2", "decision_type": "llm.anthropic.messages",
              "function_name": "anthropic.messages.create", "status": "error", "error": "RateLimitError",
              "inputs": {}, "outputs": {}, "model_parameters": {"provider": "anthropic",
                                                                "model_name": "made-up-model-9"},
              "token_usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
              "started_at": _iso(5000), "ended_at": _iso(5100), "execution_time_ms": 100.0}
    return [chain, llm, provider, failed]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HOME with receiver records, an app records file and a policy file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / ".briefcase"))
    for name in ("BRIEFCASE_MCP_ROOTS", "BRIEFCASE_MCP_RECORDS", "BRIEFCASE_EXPORTER",
                 "BRIEFCASE_PLATFORM_URL", "BRIEFCASE_EXPORTER_TOKEN", "BRIEFCASE_PRICING_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    receiver_dir = tmp_path / ".briefcase" / "receiver"
    receiver_dir.mkdir(parents=True)
    _write(receiver_dir / "records.jsonl", _receiver_records())
    _write(tmp_path / "app" / "runs.jsonl", _provider_records())
    from briefcase.policy import model, template

    policy = Path(model.user_policy_path())
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(template.render("guarded"))
    return tmp_path


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, default=str) + "\n")


@asynccontextmanager
async def mcp_client():
    """An in-process MCP client session on the installed mcp (2.x Client, or 1.x memory streams)."""
    server = build_server()
    try:
        from mcp import Client  # mcp 2.x
    except ImportError:
        Client = None
    if Client is not None:
        async with Client(server) as client:
            yield client
    else:  # mcp 1.x
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            yield session


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)
    text = "".join(getattr(item, "text", "") for item in result.content)
    if is_error:
        return {"__error__": text}
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    return structured if isinstance(structured, dict) and "result" not in structured else json.loads(text)


# -- the tools over MCP -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_tools_are_listed_with_descriptions():
    async with mcp_client() as client:
        listed = await client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert set(RECORD_TOOLS) <= set(tools)
    assert {"sanitize_text", "estimate_cost", "analyze_drift", "how_to"} <= set(tools)
    for name in RECORD_TOOLS:
        assert len(tools[name].description) > 60, name


@pytest.mark.asyncio
async def test_search_reads_receiver_records_by_default_with_pagination(home):
    async with mcp_client() as client:
        first = await call(client, "search_decisions", limit=3)
        assert first["count"] == 3 and first["next_cursor"]
        assert first["sources"][0]["path"] == "~/.briefcase/receiver/records.jsonl"
        seen = [row["id"] for row in first["items"]]
        cursor = first["next_cursor"]
        while cursor:
            page = await call(client, "search_decisions", limit=3, cursor=cursor)
            seen += [row["id"] for row in page["items"]]
            cursor = page["next_cursor"]
        assert len(seen) == len(set(seen)) == first["matched"]
        starts = [row.get("started_at") for row in first["items"]]
        assert starts == sorted(starts, reverse=True)
        bad = await call(client, "search_decisions", cursor="nonsense")
        assert "cursor is not one this server returned" in bad["__error__"]


@pytest.mark.asyncio
async def test_search_filters(home):
    async with mcp_client() as client:
        policy = await call(client, "search_decisions", decision_type="policy.tool_call", status="deny")
        assert policy["count"] == 2 and all(row["rule_id"] == "shell.rm-root" for row in policy["items"])
        model_calls = await call(client, "search_decisions", decision_type="model_call", agent="claude_code")
        assert model_calls["count"] >= 1 and all(row["type"] == "model_call" for row in model_calls["items"])
        app = await call(client, "search_decisions", path="app/runs.jsonl", model="gpt-*", since="2026-11-20T00:00:00Z")
        assert {row["id"] for row in app["items"]} == {"run-llm", "prov-1"}
        assert all("SECRET" not in json.dumps(row) for row in app["items"])
        text = await call(client, "search_decisions", path="app/runs.jsonl", text="anthropic")
        assert [row["id"] for row in text["items"]] == ["prov-2"]
        tool = await call(client, "search_decisions", tool="Bash")
        assert tool["count"] >= 1


@pytest.mark.asyncio
async def test_get_decision_gates_content(home):
    async with mcp_client() as client:
        meta = await call(client, "get_decision", decision_id="prov-1", path="app/runs.jsonl")
        assert meta["found"] and meta["content_captured"] and meta["content"] is None
        assert "include_content=true" in meta["content_note"]
        assert "SECRET" not in json.dumps(meta)
        full = await call(client, "get_decision", decision_id="prov-1", path="app/runs.jsonl",
                          include_content=True, content_chars=200)
        assert "SECRET-PROMPT" in json.dumps(full["content"])
        assert "more characters]" in full["content"]["outputs"]["content"]
        assert "strings cut to 200" in full["content_note"]
        none = await call(client, "get_decision", decision_id="run-chain", path="app/runs.jsonl",
                          include_content=True)
        assert none["content_captured"] is False and "no prompt, response or tool content" in none["content_note"]
        missing = await call(client, "get_decision", decision_id="nope")
        assert missing["found"] is False


@pytest.mark.asyncio
async def test_get_trace_dedupes_provider_record_under_framework_run(home):
    async with mcp_client() as client:
        trace = await call(client, "get_trace", id="4bf92f3577b34da6a3ce929d0e0e4736", path="app/runs.jsonl")
    assert trace["kind"] == "trace" and trace["totals"]["decisions"] == 3
    assert trace["totals"]["total_tokens"] == 1500  # not 3000: the pair is one model call
    assert trace["totals"]["deduplicated_model_calls"] == 1
    (root,) = trace["roots"]
    assert root["id"] == "run-chain" and root["duration_ms"] == 2000.0
    (llm,) = root["children"]
    assert llm["counted_in"] == "prov-1" and llm["children"][0]["id"] == "prov-1"
    assert llm["children"][0]["offset_ms"] == 150.0
    assert llm["children"][0]["cost_basis"] == "estimated"
    assert trace["totals"]["cost_estimated_usd"] > 0 and "briefcase.pricing" in trace["cost_note"]


@pytest.mark.asyncio
async def test_get_trace_by_session_on_receiver_records(home):
    async with mcp_client() as client:
        trace = await call(client, "get_trace", id="sess-0001")
    assert trace["found"] and trace["kind"] == "session" and trace["totals"]["decisions"] >= 3


@pytest.mark.asyncio
async def test_summarize_usage_says_which_costs_are_estimates(home):
    async with mcp_client() as client:
        usage = await call(client, "summarize_usage", path="app/runs.jsonl", group_by="model")
        by_model = {b["key"]: b for b in usage["buckets"]}
        assert by_model["gpt-4o"]["count"] == 2 and by_model["gpt-4o"]["total_tokens"] == 1500
        assert by_model["gpt-4o"]["estimated"] == 1 and by_model["gpt-4o"]["cost_estimated_usd"] > 0
        assert by_model["made-up-model-9"]["unpriced"] == 1 and by_model["made-up-model-9"]["errors"] == 1
        assert "made-up-model-9" in usage["unpriced_models"]
        assert usage["deduplicated_model_calls"] == 1 and "cost_estimated_usd" in usage["cost_note"]
        days = await call(client, "summarize_usage", group_by="day")
        assert days["totals"]["count"] >= 5
        tools = await call(client, "summarize_usage", group_by="tool")
        assert any(b["key"] == "Bash" for b in tools["buckets"])


@pytest.mark.asyncio
async def test_explain_policy_decision(home):
    async with mcp_client() as client:
        rows = await call(client, "search_decisions", decision_type="policy.tool_call")
        by_session = {}
        for row in rows["items"]:
            by_session.setdefault((row.get("session_id"), row["status"]), row["id"])
        denied = await call(client, "explain_policy_decision", decision_id=by_session[("sess-0001", "deny")])
        assert "was denied by rule 'shell.rm-root' from the preset policy" in denied["explanation"]
        assert denied["current_policy"]["rule"]["id"] == "shell.rm-root"
        assert "cannot be re-evaluated" in denied["current_policy"]["today_note"]
        observed = await call(client, "explain_policy_decision", decision_id=by_session[("sess-0001", "allow")])
        assert "observe mode" in observed["explanation"]
        captured = await call(client, "explain_policy_decision", decision_id=by_session[("sess-0002", "deny")])
        assert captured["current_policy"]["today"]["would"] == "deny"
        not_policy = await call(client, "explain_policy_decision", decision_id="prov-1", path="app/runs.jsonl")
        assert "not a policy decision" in not_policy["__error__"]


def _chain(home):
    from briefcase.integrity import HashChainAppender, JsonlHashChainStore

    records = _provider_records()
    chain_file = home / "audit" / "chain.jsonl"
    store = JsonlHashChainStore(chain_file)
    appender = HashChainAppender(store)
    for record in records:
        appender.append_row("decisions", record["decision_id"], None, T0, T0, record)
    store.close()
    return chain_file


@pytest.mark.asyncio
async def test_verify_records_intact_and_tampered(home):
    chain_file = _chain(home)
    async with mcp_client() as client:
        good = await call(client, "verify_records", path=str(chain_file), records_path="app/runs.jsonl")
        assert good["ok"] is True and good["entries"] == 4 and good["payloads"]["checked"] == 4
        assert good["verdict"].startswith("intact")

        lines = chain_file.read_text().splitlines()
        entry = json.loads(lines[1])
        entry["payload_hash"] = "0" * 64
        lines[1] = json.dumps(entry)
        chain_file.write_text("\n".join(lines) + "\n")
        edited = await call(client, "verify_records", path=str(chain_file))
        assert edited["ok"] is False and edited["problems"][0]["line"] == 2
        assert "edited after it was written" in edited["problems"][0]["problem"]

        chain_file.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
        removed = await call(client, "verify_records", path=str(chain_file))
        assert "removed, inserted or reordered" in removed["problems"][0]["problem"]

        records = await call(client, "verify_records", path="app/runs.jsonl")
        assert records["ok"] is False and "not tamper-evident" in records["verdict"]


@pytest.mark.asyncio
async def test_replay_plan_never_runs(home, monkeypatch):
    from briefcase.replay._recorded import executors

    def boom(*args, **kwargs):  # pragma: no cover - the test fails if reached
        raise AssertionError("replay_plan executed a call")

    monkeypatch.setattr(executors.AutoExecutor, "execute", boom)
    async with mcp_client() as client:
        plan = await call(client, "replay_plan", path="app/runs.jsonl")
    assert plan["plan"]["replayable"] == 1 and plan["plan"]["calls"] == 1
    assert plan["plan"]["tokens"]["estimate_tokens"] == 1600  # 1200 prompt + max_tokens 400
    reasons = {c["decision_id"]: c["reasons"] for c in plan["calls"]}
    assert reasons["prov-1"] == [] and reasons["prov-2"]
    assert "nothing was replayed" in plan["note"]


@pytest.mark.asyncio
async def test_drift_check_against_saved_baseline(home):
    from briefcase.drift import baseline

    base = [dict(r, started_at=_iso(i)) for i, r in enumerate(_provider_records() * 10)]
    baseline.save_profile(baseline.build_profile(base, name="prod"))
    async with mcp_client() as client:
        result = await call(client, "drift_check", baseline="prod", path="app/runs.jsonl")
        assert result["baseline"] == "prod" and result["window_records"] == 4
        assert result["summary"] and all("reading" in s for s in result["signals"])
        missing = await call(client, "drift_check", baseline="nope")
        assert "no saved baseline 'nope'" in missing["__error__"]


# -- safety -----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, message", [
    ("/etc/passwd.jsonl", "outside"),
    ("../outside.jsonl", "may not contain '..'"),
    ("app/runs.json", "must end in .jsonl"),
    ("app/missing.jsonl", "does not exist"),
])
def test_paths_are_confined(home, raw, message):
    with pytest.raises(R.ToolError, match=message):
        R.resolve_path(raw)


def test_symlink_out_of_the_roots_is_refused(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.jsonl"
    outside.write_text("{}\n")
    (home / "link.jsonl").symlink_to(outside)
    with pytest.raises(R.ToolError, match="outside"):
        R.resolve_path(str(home / "link.jsonl"))


def test_explicit_roots_replace_home(home, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_MCP_ROOTS", str(home / "app"))
    assert R.resolve_path("app/runs.jsonl").name == "runs.jsonl"
    with pytest.raises(R.ToolError, match="BRIEFCASE_MCP_ROOTS"):
        R.resolve_path(str(home / ".briefcase" / "receiver" / "records.jsonl"))


@pytest.mark.asyncio
async def test_refused_path_over_mcp_and_token_never_echoed(home, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_PLATFORM_URL", "https://observe.example.invalid")
    monkeypatch.setenv("BRIEFCASE_EXPORTER_TOKEN", "bc_live_SECRET_TOKEN")
    async with mcp_client() as client:
        refused = await call(client, "search_decisions", path="/etc/hosts.jsonl")
        assert "outside" in refused["__error__"]
        result = await call(client, "search_decisions", limit=1)
    assert any("browser session" in note for note in result["notes"])
    assert "SECRET_TOKEN" not in json.dumps(result)


def test_non_record_lines_are_not_returned(home):
    odd = home / "odd.jsonl"
    odd.write_text('{"auths": {"registry": "secret"}}\nnot json\n' + json.dumps(_provider_records()[0]) + "\n")
    result = R.search_decisions(path=str(odd))
    assert result["count"] == 1 and "secret" not in json.dumps(result)
    assert result["sources"][0]["not_decision_records"] == 1 and result["sources"][0]["skipped_lines"] == 1


def test_limits_are_bounded(home):
    with pytest.raises(R.ToolError, match="limit"):
        R.search_decisions(limit=0)
    assert R.search_decisions(limit=10_000)["count"] <= R.MAX_LIMIT
    with pytest.raises(R.ToolError, match="unknown filter"):
        R.search_decisions(colour="red")


def test_no_sources_says_how_to_get_some(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / ".briefcase"))
    monkeypatch.delenv("BRIEFCASE_EXPORTER", raising=False)
    monkeypatch.delenv("BRIEFCASE_MCP_RECORDS", raising=False)
    result = R.search_decisions()
    assert result["count"] == 0 and "briefcase-ai receive --detach" in result["notes"][0]


def test_tools_do_not_write(home):
    chain = _chain(home)
    before = sorted(str(p) for p in home.rglob("*"))
    R.search_decisions()
    R.get_decision("prov-1", path="app/runs.jsonl", include_content=True)
    R.get_trace("sess-0001")
    R.summarize_usage()
    R.verify_records(str(chain))
    R.replay_plan("app/runs.jsonl")
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.asyncio
async def test_mcp_install_lists_the_server_tools():
    from briefcase.cli import mcp_commands

    async with mcp_client() as client:
        listed = await client.list_tools()
    assert {name for name, _ in mcp_commands.SERVER_TOOLS} == {tool.name for tool in listed.tools}
    assert "search_decisions" in mcp_commands.link_text("claude-code")
