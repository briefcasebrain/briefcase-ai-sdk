# Ask your assistant about your records

Let Claude Code, Cursor, Codex or any MCP client search, explain and verify the decisions
briefcase recorded for you: which model calls cost what, why a coding tool's command was
denied, whether an audit chain was tampered with, what a replay would cost. Everything is
read-only and runs on your machine.

Flow: **Review**. Feature id: `mcp-records`. Status: experimental.

## The one command

```bash
briefcase-ai mcp install
```

It registers the `briefcase-ai-mcp` server with the coding tools on this machine (needs
`pip install 'briefcase-ai[mcp]'`). Then ask your assistant things like "what did my agents
spend yesterday, by model?" or "why was that rm command blocked?". `briefcase-ai mcp link <tool>`
prints the install payload for one tool instead. Both now end with the list of tools.

## The tools

| Tool | What it answers |
| --- | --- |
| `search_decisions` | Find records by time range, decision type, status, model, agent, tool, session/trace/workflow id, prompt name/version, or id/function-name text. Compact rows, newest first, `next_cursor` paging (≤ 200 per page). |
| `get_decision` | One record's metadata, and whether content was captured. Content only with `include_content=true`, capped per string. |
| `get_trace` | A trace, session, workflow or decision as an ordered tree: offsets, durations, tokens, cost. A provider record under a framework LLM run (`parent_run_id`) is one model call and is counted once. |
| `summarize_usage` | Count, errors, tokens and cost by model, tool, day, type or agent; recorded costs and costs estimated from `briefcase.pricing` are kept apart, unpriced models listed. |
| `explain_policy_decision` | For a `policy.tool_call` record: which rule fired, from which policy file, why, in which mode; and what the policy on this machine says today. |
| `verify_records` | Recompute a hash chain (and, given the records file, each record's payload hash) and say plainly whether anything was edited, removed or reordered. Also checks the per-row content hashes of a bitemporal SQLite store. |
| `replay_plan` | What `briefcase-ai replay run` would do: replayable or not and why, models, token estimate. **Plans only; never calls a model.** |
| `drift_check` | Recent records against a baseline saved with `briefcase-ai drift baseline save`. |

The four older tools (`sanitize_text`, `estimate_cost`, `analyze_drift`, `how_to`) and the
`briefcase://llms-full.txt` resource are unchanged.

## What it reads, and where

Read-only, local files only:

| Source | Path |
| --- | --- |
| The receiver's records (`briefcase-ai receive --detach`) | `~/.briefcase/receiver/records.jsonl` (`$BRIEFCASE_HOME/receiver/records.jsonl`) |
| Your app's records | `BRIEFCASE_EXPORTER` when it names a `.jsonl` file |
| More files | `BRIEFCASE_MCP_RECORDS=a.jsonl:b.jsonl` (`os.pathsep`-separated) |
| Per call | the `path` argument: a records `.jsonl`, a hash-chain `.jsonl`, a bitemporal `.db`/`.sqlite`, or a baseline `.json` |

**The platform is not queried.** Its read API (`decisions.list`, `.get`, `.tree`,
`.aggregate`, per `TELEMETRY_API.md` "Read API") is tRPC behind a signed-in browser session and
does not accept the exporter token. With `BRIEFCASE_PLATFORM_URL` set, every reply carries a
note saying so; the token is never read or echoed.

Nothing is written: no file, no config, no network call, no model call. The test suite checks
that the home directory is byte-for-byte unchanged after every tool has run.

## Defaults and how to change them

| Setting | Default | Change it with |
| --- | --- | --- |
| Paths a caller may name | under your home directory or `$BRIEFCASE_HOME`, after resolving symlinks; `..` refused | `BRIEFCASE_MCP_ROOTS=/data/records:/srv/audit` (replaces the default, does not add to it) |
| Content in replies | never, unless the call says `include_content=true` | per call |
| Content cap | 2,000 characters per string (max 20,000), 4x that for the whole content | `content_chars` |
| Page size | 20 rows (`search_decisions`), max 200 | `limit` |
| Records read per file | 100,000; lines over 1 MB skipped | not configurable |
| Reply size | trimmed to 60,000 characters, with a `trimmed` note | not configurable |

Only lines that look like decision records (a `decision_id`, `decision_type` or
`function_name`) are used; anything else in a file is counted, never returned.

## How to turn it off or undo

`briefcase-ai connect --undo` removes the server registration `briefcase-ai mcp install` wrote (it
shares `connect`'s journal). The tools themselves change nothing, so there is nothing else to undo.

## Real tool calls

Captured on 2026-11-22: `briefcase-ai-mcp` run over **stdio** by an `mcp` 2.3.0 client, in a
temporary `HOME` holding (1) receiver records made by the receiver from the captured Claude Code
2.1.330 OTLP payload in `tests/receiver/fixtures/real/`, the synthetic Claude Code payloads in
`tests/receiver/payloads.py` and three policy decisions posted to `/v1/policy/decisions`, (2)
`~/app/runs.jsonl`, four hand-made records in the layout LangChain and `briefcase.auto` write
(a chain run, an LLM run under it, the provider record of the same call under that, and a
failed Anthropic call), (3) a hash chain over those four written with `HashChainAppender`, and
(4) the `guarded` policy preset. The same tools are exercised in `tests/test_mcp_records.py`
with mcp 2.3.0 and with mcp 1.30.0.

Policy decisions from the receiver:

```text
→ search_decisions {"decision_type": "policy.tool_call", "limit": 2}
← is_error: False
{
  "items": [
    {
      "id": "b9a922d3-9c1b-480c-947d-cd06f3931516",
      "type": "policy.tool_call",
      "function": "claude-code.shell",
      "status": "deny",
      "started_at": "2026-11-22T05:47:52.438903+00:00",
      "duration_ms": 2.0,
      "tool": "Bash",
      "agent": "claude-code",
      "session_id": "sess-0002",
      "content_captured": true,
      "file": "~/.briefcase/receiver/records.jsonl",
      "line": 19,
      "rule_id": "shell.rm-root"
    },
    {
      "id": "852e6032-f9b0-43ac-8c47-240aeb237e7d",
      "type": "policy.tool_call",
      "function": "claude-code.shell",
      "status": "allow",
      "started_at": "2026-11-22T05:47:52.438199+00:00",
      "duration_ms": 2.0,
      "tool": "Bash",
      "agent": "claude-code",
      "session_id": "sess-0001",
      "file": "~/.briefcase/receiver/records.jsonl",
      "line": 18,
      "rule_id": "pkg.install"
    }
  ],
  "count": 2,
  "matched": 3,
  "next_cursor": "WyIyMDI2LTExLTIyVDA1OjQ3OjUyLjQzODE5OSswMDowMCIsICI4NTJlNjAzMi1mOWIwLTQzYWMtOGM0Ny0yNDBhZWIyMzdlN2QiXQ",
  "sources": [
    {
      "path": "~/.briefcase/receiver/records.jsonl",
      "records": 19
    }
  ]
}
```

```text
→ explain_policy_decision {"decision_id": "b9a922d3-9c1b-480c-947d-cd06f3931516"}
← is_error: False
{
  "found": true,
  "decision_id": "b9a922d3-9c1b-480c-947d-cd06f3931516",
  "explanation": "claude-code's shell call (Bash) was denied by rule 'shell.rm-root' from the preset policy: Deleting the root or home directory is never intended.",
  "recorded": {
    "effect": "deny",
    "would": "deny",
    "mode": "enforce",
    "enforced": true,
    "rule_id": "shell.rm-root",
    "rule_source": "preset",
    "reason": "Deleting the root or home directory is never intended.",
    "tool": "claude-code",
    "kind": "shell",
    "tool_name": "Bash",
    "session_id": "sess-0002",
    "target_captured": true,
    "at": "2026-11-22T05:47:52.438903+00:00",
    "hook_error": false
  },
  "current_policy": {
    "available": true,
    "mode": "enforce",
    "preset": "guarded",
    "rule": {
      "id": "shell.rm-root",
      "effect": "deny",
      "source": "preset",
      "matches": "built-in check: rm_root",
      "reason": "Recursive delete of a root, home or system directory."
    },
    "today": {
      "effect": "deny",
      "would": "deny",
      "rule_id": "shell.rm-root",
      "reason": "Recursive delete of a root, home or system directory."
    }
  },
  "sources": [
    {
      "path": "~/.briefcase/receiver/records.jsonl",
      "records": 19
    }
  ]
}
```

A records file, metadata only by default:

```text
→ search_decisions {"path": "app/runs.jsonl", "limit": 2}
← is_error: False
{
  "items": [
    {
      "id": "prov-2",
      "type": "llm.anthropic.messages",
      "function": "anthropic.messages.create",
      "status": "error",
      "started_at": "2026-11-20T12:00:05+00:00",
      "duration_ms": 100.0,
      "model": "made-up-model-9",
      "tokens": 10,
      "file": "~/app/runs.jsonl",
      "line": 4
    },
    {
      "id": "prov-1",
      "type": "llm.openai.chat",
      "function": "openai.chat.completions.create",
      "status": "ok",
      "started_at": "2026-11-20T12:00:00.150000+00:00",
      "duration_ms": 1700.0,
      "model": "gpt-4o",
      "agent": "langchain",
      "parent_run_id": "run-llm",
      "tokens": 1500,
      "content_captured": true,
      "file": "~/app/runs.jsonl",
      "line": 3
    }
  ],
  "count": 2,
  "matched": 4,
  "next_cursor": "WyIyMDI2LTExLTIwVDEyOjAwOjAwLjE1MDAwMCswMDowMCIsICJwcm92LTEiXQ",
  "sources": [
    {
      "path": "~/app/runs.jsonl",
      "records": 4
    }
  ]
}
```

```text
→ get_decision {"decision_id": "prov-1", "path": "app/runs.jsonl"}
← is_error: False
{
  "found": true,
  "summary": {
    "id": "prov-1",
    "type": "llm.openai.chat",
    "function": "openai.chat.completions.create",
    "status": "ok",
    "started_at": "2026-11-20T12:00:00.150000+00:00",
    "duration_ms": 1700.0,
    "model": "gpt-4o",
    "agent": "langchain",
    "parent_run_id": "run-llm",
    "tokens": 1500,
    "content_captured": true,
    "file": "~/app/runs.jsonl",
    "line": 3
  },
  "metadata": {
    "schema_version": "1.2",
    "decision_id": "prov-1",
    "decision_type": "llm.openai.chat",
    "function_name": "openai.chat.completions.create",
    "status": "ok",
    "parent_run_id": "run-llm",
    "parent_framework": "langchain",
    "model_parameters": {
      "provider": "openai",
      "model_name": "gpt-4o",
      "max_tokens": 400
    },
    "token_usage": {
      "prompt_tokens": 1200,
      "completion_tokens": 300,
      "total_tokens": 1500
    },
    "finish_reason": "stop",
    "stream": false,
    "started_at": "2026-11-20T12:00:00.150000+00:00",
    "ended_at": "2026-11-20T12:00:01.850000+00:00",
    "execution_time_ms": 1700.0,
    "inputs_keys": [
      "messages"
    ],
    "outputs_keys": [
      "content"
    ]
  },
  "content_captured": true,
  "content": null,
  "content_note": "content was captured; call again with include_content=true to see it (capped at 2000 characters per string)",
  "sources": [
    {
      "path": "~/app/runs.jsonl",
      "records": 4
    }
  ]
}
```

The same record with content, capped at 100 characters per string (`metadata` shortened here):

```text
→ get_decision {"decision_id": "prov-1", "path": "app/runs.jsonl", "include_content": true, "content_chars": 100}
← is_error: False
{
  "found": true,
  "summary": {
    "id": "prov-1",
    "type": "llm.openai.chat",
    "function": "openai.chat.completions.create",
    "status": "ok",
    "started_at": "2026-11-20T12:00:00.150000+00:00",
    "duration_ms": 1700.0,
    "model": "gpt-4o",
    "agent": "langchain",
    "parent_run_id": "run-llm",
    "tokens": 1500,
    "content_captured": true,
    "file": "~/app/runs.jsonl",
    "line": 3
  },
  "metadata": { ...same as above... },
  "content_captured": true,
    "file": "~/app/runs.jsonl",
    "line": 3
  },
  "metadata": {
    "schema_version": "1.2",
    "decision_id": "prov-1",
    "decision_type": "llm.openai.chat",
    "function_name": "openai.chat.completions.create",
    "status": "ok",
    "parent_run_id": "run-llm",
    "parent_framework": "langchain",
    "model_parameters": {
      "provider": "openai",
      "model_name": "gpt-4o",
      "max_tokens": 400
    },
    "token_usage": {
      "prompt_tokens": 1200,
      "completion_tokens": 300,
      "total_tokens": 1500
    },
    "finish_reason": "stop",
    "stream": false,
    "started_at": "2026-11-20T12:00:00.150000+00:00",
    "ended_at": "2026-11-20T12:00:01.850000+00:00",
    "execution_time_ms": 1700.0,
    "inputs_keys": [
      "messages"
    ],
    "outputs_keys": [
      "content"
    ]
  },
  "content_captured": true,
  "content": {
    "inputs": {
      "messages": [
        {
          "role": "user",
          "content": "SECRET-PROMPT summarise the memo"
        }
      ]
    },
    "outputs": {
      "content": "SECRET-RESPONSE xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx... [4916 more characters]"
    }
  },
  "content_note": "strings cut to 100 characters; this is recorded content and may be sensitive",
  "sources": [
    {
      "path": "~/app/runs.jsonl",
      "records": 4
    }
  ]
}
```

The framework run, its LLM run and the provider record of the same model call; tokens are
counted once:

```text
→ get_trace {"id": "4bf92f3577b34da6a3ce929d0e0e4736", "path": "app/runs.jsonl"}
← is_error: False
{
  "found": true,
  "kind": "trace",
  "id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "started_at": "2026-11-20T12:00:00+00:00",
  "duration_ms": 2000.0,
  "totals": {
    "decisions": 3,
    "errors": 0,
    "input_tokens": 1200,
    "output_tokens": 300,
    "total_tokens": 1500,
    "tokens_unknown": 0,
    "cost_usd": 0.0,
    "cost_estimated_usd": 0.0105,
    "unpriced": 0,
    "deduplicated_model_calls": 1
  },
  "truncated": false,
  "roots": [
    {
      "id": "run-chain",
      "type": "chain",
      "function": "RunnableSequence",
      "status": "ok",
      "started_at": "2026-11-20T12:00:00+00:00",
      "duration_ms": 2000.0,
      "offset_ms": 0.0,
      "tokens": null,
      "children": [
        {
          "id": "run-llm",
          "type": "llm",
          "function": "ChatOpenAI",
          "status": "ok",
          "started_at": "2026-11-20T12:00:00.100000+00:00",
          "duration_ms": 1800.0,
          "model": "gpt-4o",
          "parent_run_id": "run-chain",
          "tokens": {
            "input": 1200,
            "output": 300,
            "total": 1500
          },
          "offset_ms": 100.0,
          "counted_in": "prov-1",
          "note": "same model call as its provider child; counted once, there",
          "children": [
            {
              "id": "prov-1",
              "type": "llm.openai.chat",
              "function": "openai.chat.completions.create",
              "status": "ok",
              "started_at": "2026-11-20T12:00:00.150000+00:00",
              "duration_ms": 1700.0,
              "model": "gpt-4o",
              "agent": "langchain",
              "parent_run_id": "run-llm",
              "tokens": {
                "input": 1200,
                "output": 300,
                "total": 1500
              },
              "content_captured": true,
              "offset_ms": 150.0,
              "cost_usd": 0.0105,
              "cost_basis": "estimated",
              "children": []
            }
          ]
        }
      ]
    }
  ],
  "cost_note": "cost_estimated_usd comes from briefcase.pricing (packaged rates are unverified; `briefcase-ai pricing check`), not from the records",
  "sources": [
    {
      "path": "~/app/runs.jsonl",
      "records": 4
    }
  ]
}
```

Usage by model, with estimated and unpriced costs kept apart:

```text
→ summarize_usage {"path": "app/runs.jsonl", "group_by": "model"}
← is_error: False
{
  "group_by": "model",
  "buckets": [
    {
      "count": 2,
      "errors": 0,
      "input_tokens": 1200,
      "output_tokens": 300,
      "total_tokens": 1500,
      "cost_recorded_usd": 0.0,
      "cost_estimated_usd": 0.0105,
      "priced": 0,
      "estimated": 1,
      "unpriced": 0,
      "key": "gpt-4o",
      "error_rate": 0.0
    },
    {
      "count": 1,
      "errors": 0,
      "input_tokens": 0,
      "output_tokens": 0,
      "total_tokens": 0,
      "cost_recorded_usd": 0.0,
      "cost_estimated_usd": 0.0,
      "priced": 0,
      "estimated": 0,
      "unpriced": 0,
      "key": "(no model)",
      "error_rate": 0.0
    },
    {
      "count": 1,
      "errors": 1,
      "input_tokens": 10,
      "output_tokens": 0,
      "total_tokens": 10,
      "cost_recorded_usd": 0.0,
      "cost_estimated_usd": 0.0,
      "priced": 0,
      "estimated": 0,
      "unpriced": 1,
      "key": "made-up-model-9",
      "error_rate": 1.0
    }
  ],
  "bucket_count": 3,
  "totals": {
    "count": 4,
    "errors": 1,
    "input_tokens": 1210,
    "output_tokens": 300,
    "total_tokens": 1510,
    "cost_recorded_usd": 0.0,
    "cost_estimated_usd": 0.0105,
    "priced": 0,
    "estimated": 1,
    "unpriced": 1
  },
  "unpriced_models": {
    "made-up-model-9": "model not in the pricing table"
  },
  "deduplicated_model_calls": 1,
  "cost_note": "cost_recorded_usd is what the records carry; cost_estimated_usd is computed here from token counts with briefcase.pricing (packaged rates are unverified unless `briefcase-ai pricing check` says otherwise); unpriced calls are counted in `unpriced` and add nothing",
  "sources": [
    {
      "path": "~/app/runs.jsonl",
      "records": 4
    }
  ]
}
```

Verifying the chain, then again after one entry's `payload_hash` was edited by hand:

```text
→ verify_records {"path": "audit/chain.jsonl", "records_path": "app/runs.jsonl"}
← is_error: False
{
  "ok": true,
  "verdict": "intact: 4 entries in 1 chain segment(s) link up and every entry hash matches its fields",
  "path": "~/audit/chain.jsonl",
  "entries": 4,
  "segments": 1,
  "problems": [],
  "payloads": {
    "records_path": "~/app/runs.jsonl",
    "records": 4,
    "checked": 4,
    "mismatched": 0,
    "records_not_in_chain": 0
  },
  "checked": "chain linkage and entry hashes and record payload hashes; this proves the chain file is self-consistent, not who wrote it"
}
```

```text
→ verify_records {"path": "audit/chain.jsonl"}
← is_error: False
{
  "ok": false,
  "verdict": "NOT intact: 1 problem(s); the first is at line 2",
  "path": "~/audit/chain.jsonl",
  "entries": 4,
  "segments": 1,
  "problems": [
    {
      "line": 2,
      "row_id": "run-llm",
      "segment": "decisions",
      "position": 1,
      "problem": "its hash does not match its own fields: this entry was edited after it was written"
    }
  ],
  "checked": "chain linkage and entry hashes; this proves the chain file is self-consistent, not who wrote it"
}
```

A replay plan (nothing runs):

```text
→ replay_plan {"path": "app/runs.jsonl"}
← is_error: False
{
  "plan": {
    "records": 4,
    "replayable": 1,
    "not_replayable": 3,
    "selected": 1,
    "blocked_by_executor": 0,
    "blocked_reasons": {},
    "calls": 1,
    "per_model": {
      "gpt-4o": 1
    },
    "tokens": {
      "recorded_prompt_tokens": 1200,
      "upper_bound_tokens": 1600,
      "estimate_tokens": 1600,
      "calls_without_usage": 0,
      "calls_without_output_cap": 0
    }
  },
  "calls": [
    {
      "index": 0,
      "decision_id": "run-chain",
      "decision_type": "chain",
      "surface": null,
      "model": null,
      "replayable": false,
      "reasons": [
        "decision_type 'chain' is not a provider call recorded by briefcase.auto (openai, anthropic, google-genai or bedrock), so there is no request to rebuild"
      ],
      "warnings": []
    },
    {
      "index": 1,
      "decision_id": "run-llm",
      "decision_type": "llm",
      "surface": null,
      "model": null,
      "replayable": false,
      "reasons": [
        "decision_type 'llm' is not a provider call recorded by briefcase.auto (openai, anthropic, google-genai or bedrock), so there is no request to rebuild"
      ],
      "warnings": []
    },
    {
      "index": 2,
      "decision_id": "prov-1",
      "decision_type": "llm.openai.chat",
      "surface": "openai.chat",
      "model": "gpt-4o",
      "replayable": true,
      "reasons": [],
      "warnings": [
        "capture does not record response_format, tool_choice, stop, parallel_tool_calls, logit_bias, prediction, reasoning_effort (non-string), web_search_options, modalities, audio, functions, function_call, moderation; if the original call set any of them, pass it with --param"
      ]
    },
    {
      "index": 3,
      "decision_id": "prov-2",
      "decision_type": "llm.anthropic.messages",
      "surface": "anthropic.messages",
      "model": "made-up-model-9",
      "replayable": false,
      "reasons": [
        "the original call did not complete (status=error, error), so there is no recorded output to compare against",
        "content capture was off when this call was recorded (the default), so the request (messages) is not in the record; re-capture with briefcase.auto(capture_content=True)"
      ],
      "warnings": []
    }
  ],
  "missing_ids": [],
  "note": "plan only: nothing was replayed and no model was called. To run it yourself: `briefcase-ai replay run ~/app/runs.jsonl` (it asks before spending money)",
  "sources": [
    {
      "path": "~/app/runs.jsonl",
      "records": 4
    }
  ]
}
```

A path outside the allowed roots:

```text
→ search_decisions {"path": "/etc/passwd.jsonl"}
← is_error: True
Error executing tool search_decisions: '/etc/passwd.jsonl' is outside your home directory or $BRIEFCASE_HOME (or set BRIEFCASE_MCP_ROOTS); refused
```

## Limits and failure modes

- Local files only (see above). No chained SQLite store exists in the SDK: the native
  `SqliteBackend` stores snapshots without a chain, so `verify_records` on such a file says
  there is nothing to verify. The bitemporal SQLite store has per-row content hashes, which are
  checked; a deleted row is not detectable that way.
- `verify_records` proves a chain file is self-consistent. It does not check Ed25519 signatures
  (it reports how many entries carry one) and cannot tell who wrote the file.
- `explain_policy_decision` can re-evaluate a call against today's policy only when the call's
  target was captured (hook `--capture-content` and receiver `--keep-content`); by default only
  a keyed hash of it is recorded.
- Costs: `cost_recorded_usd` is what a record carries (Claude Code reports it);
  `cost_estimated_usd` is computed from token counts with `briefcase.pricing`, whose packaged
  rates are unverified unless `briefcase-ai pricing check` says otherwise; prompt-cache tokens and
  long-context tiers are left unpriced rather than guessed.
- The cursor is a position (`started_at`, id), so new records appended between pages do not
  shift a page; edits to a file between pages can.
- With mcp 2.x an unexpected error reaches the assistant only as "Error executing tool ..."; the
  details stay in the server's log. Argument and path errors carry their message.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| The assistant says it has no briefcase tools | the server is not registered with that tool | `briefcase-ai mcp install` (then restart the tool) |
| `search_decisions` returns nothing and a note "no record files found" | no receiver records and no `.jsonl` exporter | `briefcase-ai receive --detach`, or `BRIEFCASE_EXPORTER=runs.jsonl` in your app |
| "... is outside your home directory ... refused" | the path is outside the allowed roots (or a symlink out of them) | move the file, or set `BRIEFCASE_MCP_ROOTS` for the server |
| `get_decision` says no content was captured | content capture was off when the record was written (the default) | re-capture with content on (`briefcase.auto(capture_content=True)`, receiver `--keep-content`) if you need it |
| Costs show up as `unpriced` | the model is not in the pricing table | `briefcase-ai pricing set MODEL ...`, then ask again |
| `verify_records` says "decision records, not hash-chain entries" | you passed the records file | pass the chain file as `path` and the records file as `records_path` |
| `drift_check` says "no saved baseline" | none saved under that name | `briefcase-ai drift baseline save NAME --from FILE` |
