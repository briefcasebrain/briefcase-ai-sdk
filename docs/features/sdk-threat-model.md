# SDK release threat model

## Policy hooks

The protected decision is the host's decision to run a tool call. The SDK trusts the installed host and Python executable. It treats tool input, paths, the working directory, policy files and hook output delivery as potential sources of error. A hook judges the tool call presented by the host. It cannot inspect every action taken later by a subprocess.

With `--fail-closed`, defective events and evaluation failures must deny. A valid event follows the configured policy, including observe mode. A hook bound with `--require-policy-sha256` must load the specified policy bytes and find an explicit allow for every unit, including an explicit allow default. A host must receive its own denial format or a blocking exit status with the required reason. Real host runs must check whether the requested action took place.

Codex can allow a call when the executable never starts, the host times out, or both output channels are unavailable. The installer requires acceptance of the Codex startup risk for strict installations. See [Policy hooks](policy-hooks.md).

## Configuration and telemetry

Configuration may contain unrelated settings and credentials. Preserve user edits between preview, write and undo. Previews show only SDK changes. Content collection and export require explicit selection. A global CLI operand is data even if its spelling matches a hook option. The configuration writer documents ownership, exposure and journaling requirements in `briefcase/connect/edits.py`.

## Engine lifecycle

Verify signed release material and establish daemon, service, image and volume ownership before mutation. A missing Docker response is an unknown inventory. Dry runs and declined actions preserve state. Preserve TLS across upgrades and retain secrets while a dependent volume might exist. See [Engine](engine.md) and the lifecycle invariants in `tests/cli/test_engine_invariants.py`.

The SDK assumes the endpoint continues to identify the same daemon between the final identity check and Compose's connection. On an empty stack, another Compose client racing startup can cause Compose to scale down a container. The SDK cannot guarantee detection of that removal. It must never create or delete a foreign tracking container or volume.

Down, upgrade and uninstall require exclusive administration of the engine project and its image tags on the selected Docker daemon from the start of the command until it exits. The SDK install lock serializes cooperating SDK commands. Docker clients outside that lock can replace a checked resource before Compose or Docker removes it. The operator must quiesce those clients for these operations.

## Release evidence

Record the installed versions and observed outcomes for Codex, Claude Code, Cursor and Docker Compose. Host tests use isolated configuration and harmless observable actions. Compose tests use disposable projects and data. Keep deterministic fake-host tests for failure paths. SDK 4.5.0 requires clean independent review and green CI on the exact release commit.

On 2026-11-25, Docker Compose 5.5.0 resolved all six stock services. The disposable startup fixture created a server, stopped it, and reused the same container ID on the second SDK startup.

On 2026-11-25, Claude Code 2.1.332, Codex 0.170.0 and Cursor 2026.09.18-9a7762b each attempted the requested marker command in separate allow and deny projects. The fixture checked the hook response for that command. Each allow created the marker and each deny left it absent. Cursor used temporary configuration and data directories.
