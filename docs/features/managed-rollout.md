# Managed rollout for a team

Generate the files an administrator deploys with MDM, Group Policy or configuration management
so every developer machine sends coding-tool telemetry to your receiver and, optionally, enforces
one policy that developers cannot switch off. Nothing is installed by the command: it writes a
folder of files and a README that says where each one goes on each operating system.

Status: **experimental**. The file formats come from the vendors' documentation (sources and
dates below) and are pinned by golden-file tests. No generated file has been installed on a
managed macOS, Windows or Linux machine by this project. The Claude Code hook entry inside the
pack is the same entry `briefcase-ai policy init` writes, which was run against the real Claude Code
2.1.330.

## The one command

```bash
briefcase-ai connect --managed --out ./briefcase-rollout --endpoint https://otel.example.com --policy fleet-policy.toml
```

Real output (temp `HOME`):

```text
$ briefcase-ai connect --managed --out rollout --endpoint https://otel.example.com --policy fleet-policy.toml
Rollout pack for: claude-code, cursor, codex, cowork (with policy enforcement)
  ready    briefcase/policy.toml
  ready    claude-code/managed-settings.d/10-briefcase.json
  ready    claude-code/macos/briefcase-claude-code.mobileconfig
  TEMPLATE claude-code/windows/claude-code-policy.reg.template
  ready    cursor/hooks.json
  ready    codex/hooks.json
  TEMPLATE codex/requirements-hooks.toml.template
  TEMPLATE codex/config-otel.toml.template
  TEMPLATE cowork/CHECKLIST.md
  ready    README.md, manifest.json

Wrote 11 file(s) to rollout. Nothing outside that directory was touched.
Next: read rollout/README.md
```

Options: `--tools claude-code cursor codex cowork` (default: all four), `--policy FILE` (without
it the pack sets up telemetry only and enforces nothing), `--headers KEY=VALUE` (OTLP headers,
for example a bearer token), `--capture-content`, `--dry-run`, `--force` (write into a folder
that is not empty). Write the policy first with `briefcase-ai policy init --dry-run` or copy your own
`~/.briefcase/policy.toml`, and check it with `briefcase-ai policy check`.

## What it writes, and where each file goes

| File | Status | Goes to | Format from |
| --- | --- | --- | --- |
| `briefcase/policy.toml` | ready | macOS `/Library/Application Support/Briefcase/policy.toml`, Linux/WSL `/etc/briefcase/policy.toml`, Windows `C:\ProgramData\Briefcase\policy.toml` | briefcase |
| `claude-code/managed-settings.d/10-briefcase.json` | ready | macOS `/Library/Application Support/ClaudeCode/managed-settings.d/`, Linux/WSL `/etc/claude-code/managed-settings.d/`, Windows `C:\Program Files\ClaudeCode\managed-settings.d\` | [managed settings](https://code.claude.com/docs/en/managed-settings) |
| `claude-code/macos/briefcase-claude-code.mobileconfig` | ready | your MDM, as a custom configuration profile (device channel). Use it **or** the drop-in, not both | the `com.anthropic.claudecode` domain from the page above, in Apple's [managed preferences](https://developer.apple.com/documentation/devicemanagement/managedpreferences) payload |
| `claude-code/windows/claude-code-policy.reg.template` | TEMPLATE | `HKLM\SOFTWARE\Policies\ClaudeCode`, value `Settings` (REG_SZ) | the registry location is documented; the `.reg` syntax was not checked or imported |
| `cursor/hooks.json` | ready | macOS `/Library/Application Support/Cursor/hooks.json`, Linux/WSL `/etc/cursor/hooks.json`, Windows `C:\ProgramData\Cursor\hooks.json` | [Cursor hooks](https://cursor.com/docs/hooks) |
| `codex/hooks.json` | ready (only with `--policy`) | macOS and Linux `/etc/codex/hooks.json`, Windows `%ProgramData%\OpenAI\Codex\hooks.json` | [Codex hooks](https://developers.openai.com/codex/hooks) |
| `codex/requirements-hooks.toml.template` | TEMPLATE | merge into your organization's `requirements.toml` instead of `hooks.json` | same page; where `requirements.toml` lives is not documented there |
| `codex/config-otel.toml.template` | TEMPLATE | each user's `~/.codex/config.toml` (no documented system location) | [Codex config](https://developers.openai.com/codex/config-advanced) |
| `cowork/CHECKLIST.md` | TEMPLATE | steps for the organization admin console | not checked against the current console |
| `README.md`, `manifest.json` | ready | read them; `manifest.json` lists every file, its status and its source for your tooling | |

The Claude Code drop-in, as generated:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com"
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^(Bash|PowerShell|Read|Write|Edit|MultiEdit|NotebookEdit|Glob|Grep|WebFetch|WebSearch)$|^mcp__",
        "hooks": [
          {
            "type": "command",
            "command": "briefcase-ai hook claude-code --enforce --endpoint https://otel.example.com",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

The hooks run `briefcase-ai hook <tool> --enforce`, so `briefcase-ai` must be installed on every
machine with `briefcase-ai` on the PATH the coding tools see. Each hook decides on the machine (no
network hop) and then posts a decision record (rule id and effect; the command or path only as a
keyed hash) to `<endpoint>/v1/policy/decisions` without waiting. If your receiver needs a token,
set `BRIEFCASE_RECEIVER_TOKEN` in the environment the tools run in; without it records are
refused and enforcement is unaffected.

## Defaults and how to change them

- **Lock settings.** The drop-in contains only `env` and `hooks`. Managed hooks already cannot be
  removed or disabled by a user (`disableAllHooks` outside managed settings does not reach them,
  and `claude --safe-mode` keeps them). Two further switches are documented managed settings, and
  the pack README lists them, but they are not added because they change what developers can do:
  `"allowManagedHooksOnly": true` (only managed hooks run) and
  `"permissions": {"disableBypassPermissionsMode": "disable"}`.
- **Failure behaviour** follows the policy's `fail_closed`. With `fail_closed = true` the Claude
  Code handler gets `"onFailure": "block"` (Claude Code 2.1.295+) and the Cursor entries
  `"failClosed": true`, so a machine where `briefcase-ai` is missing blocks tool calls instead of
  allowing them. Codex has no such switch.
- **Policy layering.** The system policy is the base on each machine. A user's
  `~/.briefcase/policy.toml` and a project's `.briefcase/policy.toml` can only add `ask` and
  `deny` rules or stricter settings; `briefcase-ai policy check` on a machine lists what it ignored.
- **One Claude Code delivery per OS.** Claude Code uses the highest-ranked managed source that has
  a policy (MDM profile or HKLM above the files) unless `managedSourcesBehavior` is `merge`.

## How to turn it off or undo

Remove the files from the machines with the same tooling that placed them (or remove the MDM
profile). Nothing else was changed: the command never writes outside `--out`, and it refuses a
system path as `--out`.

## Limits and failure modes

- Files in system locations cannot be changed by an agent running as the developer, which is what
  makes this stronger than `briefcase-ai policy init`. A developer who is an administrator on their
  own machine can still change them.
- The policy judges tool calls, not their effects; see "What a local policy cannot guarantee" in
  [policy-hooks.md](policy-hooks.md). Cursor has no hook for web requests and Codex none for file
  reads or web requests, so those rules are not enforced there.
- Codex cannot ask from a hook, and Cursor can ask only for shell and MCP calls; elsewhere an
  `ask` rule follows the policy's `ask_fallback` (default `deny`).
- Claude Desktop / Cowork telemetry is an organization setting, and there is no hook there the
  policy can use.

## Verify a machine

```bash
briefcase-ai doctor --connect          # receiver reachable, tool versions, what connect wrote for this user
briefcase-ai policy check              # the system policy is listed as `ok system`
briefcase-ai policy test -- rm -rf /   # DENY ... shell.rm-root
```

In Claude Code, `/status` should list `Enterprise managed settings` on the `Setting sources` line,
with `(drop-ins)`, `(file + drop-ins)`, `(plist)` or `(HKLM)`; `claude doctor` lists entries it
dropped. In Cursor, check Customize > Hooks and the Hooks output channel.

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| `error: ... is a system path` | Pass a working directory as `--out`; let your MDM place the files. |
| `error: ... is not empty` | Choose a new folder, or pass `--force` to overwrite files in it. |
| `error: ... is not a valid policy` | `briefcase-ai policy check` on that file (copy it to `~/.briefcase/policy.toml` on a test machine) and fix the line it names. |
| Claude Code does not show `Enterprise managed settings` in `/status` | The file is in the wrong directory or not readable; `claude doctor` names entries it dropped. On macOS check you used the profile or the drop-in, not both. |
| Tool calls run although the policy should deny them | On that machine: `briefcase-ai policy test -- <command>`, then `briefcase-ai policy check`. If `briefcase-ai` is not on the tools' PATH the hook fails and, without `fail_closed`, the call is allowed. |
| Every tool call is blocked after the rollout | `fail_closed = true` and `briefcase-ai` is missing or the policy is broken. Install `briefcase-ai`, or fix the policy with `briefcase-ai policy check`. |
| No records arrive at the receiver | Check the endpoint in the files, TLS, and `BRIEFCASE_RECEIVER_TOKEN` on the machines; `briefcase-ai receive --status` on the receiver host. |
| Codex hooks do not run | Make sure `/etc/codex/hooks.json` is readable; system hooks are trusted by policy and skip the `/hooks` review. |

## Sources

Read 2026-11-21; the Claude Code managed-settings and hooks pages and the Cursor and Codex hooks pages
were fetched again on 2026-11-22 and were byte-for-byte unchanged:
https://code.claude.com/docs/en/managed-settings, https://code.claude.com/docs/en/hooks,
https://code.claude.com/docs/en/settings-reference (`allowManagedHooksOnly`,
`disableAllHooks`), https://cursor.com/docs/hooks, https://developers.openai.com/codex/hooks,
https://developers.openai.com/codex/config-advanced,
https://developer.apple.com/documentation/devicemanagement/managedpreferences.
