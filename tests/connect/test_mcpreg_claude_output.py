"""The Claude registry lookup against the exact text `claude mcp get` prints (Claude Code 2.1.330)."""
import pytest

from briefcase.connect import mcpreg


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """The lookup also reads Claude's own config file: never the real one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


ADD = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--",
       "uvx", "--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]

# Captured from `claude mcp get` on a stdio server with no environment variables: the CLI prints an empty
# "Environment:" heading, a blank line, then a removal hint.
NO_ENV = """briefcase:
  Scope: User config (available in all your projects)
  Status: ✔ Connected
  Type: stdio
  Command: uvx
  Args: --from briefcase-ai[mcp] briefcase-ai-mcp
  Environment:

To remove this server, run: claude mcp remove briefcase -s user
"""

WITH_ENV = NO_ENV.replace("  Environment:\n", "  Environment:\n    TOKEN=abc\n")

# A server that failed to start adds an "Issue:" line and may omit the Environment heading.
FAILED = """briefcase:
  Scope: User config (available in all your projects)
  Status: ✘ Failed to connect
  Issue: ENOENT: no such file or directory
  Type: stdio
  Command: uvx
  Args: --from briefcase-ai[mcp] briefcase-ai-mcp

To remove this server, run: claude mcp remove briefcase -s user
"""


def runner_for(text, code=0):
    return lambda argv: (code, text)


def test_our_registration_with_no_environment_is_ours():
    assert mcpreg.owned(ADD, runner_for(NO_ENV)) == mcpreg.OURS


def test_a_registration_with_an_environment_variable_is_not_ours():
    assert mcpreg.owned(ADD, runner_for(WITH_ENV)) == mcpreg.DIFFERS


def test_a_registration_that_failed_to_connect_is_still_recognised():
    assert mcpreg.owned(ADD, runner_for(FAILED)) == mcpreg.OURS


def test_the_cli_s_not_found_message_is_absent():
    msg = 'No MCP server named "briefcase". Configured servers: auth0, aws-mcp (and 43 more)'
    assert mcpreg.lookup(ADD, runner_for(msg, code=1))[0] == mcpreg.ABSENT
