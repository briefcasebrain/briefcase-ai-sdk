"""File rules: globs, ``..`` traversal, symlinks, MCP names and URL hosts."""

import os

import pytest

from briefcase.policy import engine, model

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX paths and symlinks")

GUARDED = 'version = 1\npreset = "guarded"\n'


def policy_of(text=GUARDED):
    return model.merge([model.parse_layer(text, "user")])


@pytest.fixture
def decide(home, project):
    def run(path, kind="read", text=GUARDED, cwd=None):
        call = engine.ToolCall(kind, paths=[str(path)], cwd=str(cwd or project), project_dir=str(project))
        return engine.evaluate(policy_of(text), call)
    return run


@pytest.mark.parametrize("relative", [
    ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/keys/deploy", ".aws/credentials", ".config/gcloud/credentials.db",
    ".kube/config", ".netrc", ".npmrc", ".git-credentials", ".docker/config.json", ".gnupg/private-keys-v1.d/x.key",
    ".claude/.credentials.json", ".codex/auth.json", ".azure/msal_token_cache.json",
])
def test_credential_files_in_home_are_denied_for_read_and_write(decide, home, relative):
    for kind in ("read", "write"):
        result = decide(home / relative, kind)
        assert (result.effect, result.rule_id) == ("deny", "secrets.files"), (relative, kind)


@pytest.mark.parametrize("relative", [
    ".env", ".env.local", ".env.production", "api/.env", "deep/er/.env.staging", "certs/server.pem",
    "keys/id_rsa", "secrets.yaml", "config/credentials.json", "service-account-prod.json", "store.p12",
])
def test_secret_names_match_at_any_depth_in_a_project(decide, project, relative):
    assert decide(project / relative).rule_id == "secrets.files", relative


@pytest.mark.parametrize("relative", [
    ".env.example", ".env.sample", ".env.template", "README.md", "src/environment.py", "src/env.py",
    ".envrc.md", "docs/secrets.md", "id_rsa.pub.txt", ".ssh-notes", "my.env.txt",
])
def test_lookalikes_are_not_secrets(decide, project, relative):
    assert decide(project / relative).effect == "allow", relative


def test_public_ssh_files_are_readable(decide, home):
    for name in ("id_rsa.pub", "known_hosts", "config"):
        assert decide(home / ".ssh" / name).effect == "allow", name


def test_directory_itself_matches(decide, home):
    # Grep or Glob aimed at the directory reads what is inside it.
    assert decide(home / ".ssh").effect == "deny"
    assert decide(str(home / ".ssh") + "/").effect == "deny"


@pytest.mark.parametrize("spelling", [
    "{home}/.ssh/../.ssh/id_rsa",
    "{home}/./.ssh//id_rsa",
    "{project}/../home/.ssh/id_rsa",
    "{project}/a/b/../../../home/.ssh/id_rsa",
    "{home}/.aws/../.ssh/id_rsa",
    "//{home}/.ssh/id_rsa",
])
def test_traversal_spellings_are_folded_before_matching(decide, home, project, spelling):
    path = spelling.format(home=home, project=project)
    assert decide(path).rule_id == "secrets.files", path


def test_relative_paths_resolve_against_the_working_directory(decide, home):
    assert decide("id_rsa", cwd=home / ".ssh").effect == "deny"
    assert decide("../.ssh/id_rsa", cwd=home / ".aws").effect == "deny"
    assert decide("~/.ssh/id_rsa").effect == "deny"


def test_symlink_to_a_secret_is_the_secret(decide, home, project):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("KEY")
    link = project / "innocent.txt"
    link.symlink_to(home / ".ssh" / "id_rsa")
    result = decide(link)
    assert result.rule_id == "secrets.files"
    assert "resolves to" in result.match.detail


def test_symlinked_directory_into_a_secret_directory(decide, home, project):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("KEY")
    (project / "data").symlink_to(home / ".ssh")
    assert decide(project / "data" / "id_rsa").rule_id == "secrets.files"
    assert decide(project / "data" / "anything-else").rule_id == "secrets.files"   # still inside ~/.ssh


def test_chain_of_symlinks(decide, home, project):
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("x")
    (project / "one").symlink_to(home / ".aws" / "credentials")
    (project / "two").symlink_to(project / "one")
    assert decide(project / "two").effect == "deny"


def test_secret_directory_that_is_itself_a_symlink(decide, home, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "id_rsa").write_text("KEY")
    (home / ".ssh").symlink_to(vault)
    assert decide(home / ".ssh" / "id_rsa").effect == "deny"       # the spelling the rule uses
    assert decide(vault / "id_rsa").effect == "deny"               # the real location: the rule's prefix resolves too
    assert decide(vault / "other").effect == "deny"


def test_dangling_symlink_and_missing_files_do_not_crash(decide, project):
    (project / "dangling").symlink_to(project / "nowhere" / ".env")
    assert decide(project / "dangling").effect == "deny"
    assert decide(project / "does" / "not" / "exist.txt").effect == "allow"


def test_symlink_loop_does_not_hang(decide, project):
    (project / "a").symlink_to(project / "b")
    (project / "b").symlink_to(project / "a")
    assert decide(project / "a").effect in ("allow", "deny")


def test_allow_rule_does_not_cover_a_symlink_out_of_its_directory(home, project):
    text = GUARDED + '[[rule]]\nid = "fixtures"\ntool = "file"\neffect = "allow"\npaths = ["${project}/fixtures"]\n'
    (project / "fixtures").mkdir()
    (project / "fixtures" / ".env").write_text("A=1")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("KEY")
    (project / "fixtures" / "key").symlink_to(home / ".ssh" / "id_rsa")

    def decide(path):
        call = engine.ToolCall("read", paths=[str(path)], cwd=str(project), project_dir=str(project))
        return engine.evaluate(policy_of(text), call)

    assert decide(project / "fixtures" / ".env").rule_id == "fixtures"          # the carve-out works
    assert decide(project / "fixtures" / "key").rule_id == "secrets.files"      # the symlink does not borrow it


def test_protected_files_deny_writes_but_not_reads(decide, home, project):
    for path in (home / ".briefcase" / "policy.toml", home / ".briefcase" / "policy" / "decisions.jsonl",
                 home / ".claude" / "settings.json", project / ".claude" / "settings.json",
                 project / ".claude" / "settings.local.json", home / ".cursor" / "hooks.json",
                 project / ".cursor" / "hooks.json", home / ".codex" / "hooks.json",
                 project / ".briefcase" / "policy.toml"):
        assert decide(path, "write").rule_id == "self.files", path
        assert decide(path, "read").effect == "allow", path


def test_protected_file_reached_through_a_symlink(decide, home, project):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    (project / "s.json").symlink_to(home / ".claude" / "settings.json")
    assert decide(project / "s.json", "write").rule_id == "self.files"


def test_observe_preset_protects_nothing(decide, home):
    text = 'version = 1\npreset = "observe"\n'
    assert decide(home / ".briefcase" / "policy.toml", "write", text=text).effect == "allow"


def test_strict_asks_for_writes_outside_the_project(home, project, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    policy = policy_of('version = 1\npreset = "strict"\n')

    def decide(path, kind="write"):
        call = engine.ToolCall(kind, paths=[str(path)], cwd=str(project), project_dir=str(project))
        return engine.evaluate(policy, call)

    assert decide(project / "src" / "a.py").effect == "allow"
    assert decide(tmp_path / "tmp" / "scratch.txt").effect == "allow"
    assert decide(home / "notes.txt").rule_id == "write.outside-project"
    assert decide(project / ".." / "elsewhere.txt").rule_id == "write.outside-project"
    assert decide(home / "notes.txt", "read").effect == "allow"


def test_user_rule_globs(home, project):
    text = GUARDED + '''
[[rule]]
id = "gen"
tool = "write"
effect = "deny"
paths = ["${project}/generated/**", "*.lock", "migrations/*.sql", "/etc"]
except = ["${project}/generated/README.md"]
'''
    policy = policy_of(text)

    def effect(path, kind="write"):
        call = engine.ToolCall(kind, paths=[str(path)], cwd=str(project), project_dir=str(project))
        return engine.evaluate(policy, call).effect

    assert effect(project / "generated" / "a" / "b.py") == "deny"
    assert effect(project / "generated" / "README.md") == "allow"
    assert effect(project / "poetry.lock") == "deny" and effect(project / "sub" / "yarn.lock") == "deny"
    assert effect(project / "migrations" / "001.sql") == "deny"
    assert effect(project / "migrations" / "old" / "001.sql") == "allow"      # `*` stops at a slash
    assert effect("/etc/hosts") == "deny" and effect("/etcetera") == "allow"
    assert effect(project / "generated" / "x.py", "read") == "allow"         # a write rule


def test_glob_translation():
    import re

    def matches(pattern, path):
        return re.match(engine.glob_to_regex(pattern) + r"\Z", path) is not None

    assert matches("/a/*.py", "/a/x.py") and not matches("/a/*.py", "/a/b/x.py")
    assert matches("/a/**/x.py", "/a/x.py") and matches("/a/**/x.py", "/a/b/c/x.py")
    assert matches("/a/**", "/a/b/c") and matches("/a/?.txt", "/a/b.txt") and not matches("/a/?.txt", "/a/bb.txt")
    assert matches("/a/[ab].txt", "/a/a.txt") and not matches("/a/[!ab].txt", "/a/a.txt")
    assert matches("/a/x+y(1).txt", "/a/x+y(1).txt") and not matches("/a/x.txt", "/a/xatxt")


# --- MCP and web ------------------------------------------------------------------

def test_mcp_rules(home, project):
    text = GUARDED + '''
[[rule]]
id = "gh-writes"
tool = "mcp"
effect = "ask"
server = "github"
name = ["create_*", "delete_*"]

[[rule]]
id = "no-shell-mcp"
tool = "mcp"
effect = "deny"
server = ["*shell*", "exec"]
'''
    policy = policy_of(text)

    def decide(server, name):
        return engine.evaluate(policy, engine.ToolCall("mcp", server=server, name=name, cwd=str(project)))

    assert decide("github", "create_issue").rule_id == "gh-writes"
    assert decide("github", "list_issues").effect == "allow"
    assert decide("gitlab", "create_issue").effect == "allow"
    assert decide("my-shell-server", "run").rule_id == "no-shell-mcp"
    assert decide(None, "create_issue").effect == "allow"       # the server is unknown: a server rule cannot match
    strict = policy_of('version = 1\npreset = "strict"\n[[rule]]\ntool = "mcp"\neffect = "allow"\nserver = "docs"\n')
    assert engine.evaluate(strict, engine.ToolCall("mcp", server="docs", name="search")).effect == "allow"
    assert engine.evaluate(strict, engine.ToolCall("mcp", server="other", name="search")).rule_id == "mcp.any"


@pytest.mark.parametrize("url, host", [
    ("https://example.com/path", "example.com"),
    ("https://EXAMPLE.com:8443/x", "example.com"),
    ("https://allowed.com@evil.example/x", "evil.example"),
    ("https://user:pw@evil.example:443/", "evil.example"),
    ("http://example.com.", "example.com"),
    ("https://example.com?next=https://other.org", "example.com"),
    ("https://example.com#@evil.example", "example.com"),
    ("https://evil.example\\@example.com/", "evil.example"),
    ("example.com/path", "example.com"),
    ("https://[::1]:8080/x", "::1"),
    ("", None), (None, None),
])
def test_host_extraction(url, host):
    assert engine.host_of(url) == host


def test_web_rules(home, project):
    text = GUARDED + '''
[[rule]]
id = "paste"
tool = "web"
effect = "deny"
hosts = ["pastebin.com", "*.ngrok.io"]
'''
    policy = policy_of(text)

    def effect(url):
        return engine.evaluate(policy, engine.ToolCall("web", url=url, cwd=str(project))).effect

    assert effect("https://pastebin.com/raw/x") == "deny"
    assert effect("https://a.b.ngrok.io/") == "deny" and effect("https://ngrok.io/") == "deny"
    assert effect("https://notpastebin.com/") == "allow"
    assert effect("https://pastebin.com@example.org/") == "allow"       # the host is example.org
    assert effect("https://example.org/?u=pastebin.com") == "allow"
    assert effect(None) == "allow"                                       # a web search has no host
    strict = policy_of('version = 1\npreset = "strict"\n[[rule]]\ntool = "web"\neffect = "allow"\n'
                       'hosts = "docs.python.org"\n')
    assert engine.evaluate(strict, engine.ToolCall("web", url="https://docs.python.org/3/")).effect == "allow"
    assert engine.evaluate(strict, engine.ToolCall("web", url=None)).rule_id == "web.any"


def test_guardrail_adapter_speaks_the_framework_types(home, project):
    from briefcase.guardrails import Effect, EvalRequest, GuardrailEnv, GuardrailPipeline
    from briefcase.policy.guardrail import ToolPolicyEnv

    env = ToolPolicyEnv.from_text(GUARDED)
    assert isinstance(env, GuardrailEnv)

    def run(action, resource):
        return env.evaluate(EvalRequest("claude-code", action, resource, {"cwd": str(project)}))

    denied = run("shell", "rm -rf /")
    assert denied.effect is Effect.DENY and denied.policy_id == "shell.rm-root"
    assert denied.metadata["decision"] == "deny" and not denied.is_allowed
    asked = run("shell", "pip install x")
    assert asked.effect is Effect.DENY and asked.metadata["decision"] == "ask"   # a pipeline has nobody to ask
    assert run("shell", "ls").effect is Effect.ALLOW
    assert run("read", str(project / ".env")).policy_id == "secrets.files"
    assert run("mcp", "github.create_issue").is_allowed and run("web", "https://example.com").is_allowed
    assert env.explain(denied).policy_applied["name"] == "shell.rm-root"

    pipeline = GuardrailPipeline([env])
    assert pipeline.evaluate(EvalRequest("cursor", "shell", "rm -rf /")).is_allowed is False

    audit = ToolPolicyEnv.from_text(GUARDED + 'mode = "observe"\n')
    logged = audit.evaluate(EvalRequest("claude-code", "shell", "rm -rf /"))
    assert logged.effect is Effect.ALLOW
    assert logged.metadata["violation_mode"] == "audit" and logged.metadata["original_effect"] == "deny"


def test_a_symlinked_directory_before_a_parent_component_resolves_the_way_the_kernel_does(decide, home, project):
    """`<project>/link/../notes.txt` with link -> ~/.ssh/deep opens ~/.ssh/notes.txt: `..` applies after the link is
    followed, not to the spelling (which would give <project>/notes.txt)."""
    (home / ".ssh" / "deep").mkdir(parents=True)
    (home / ".ssh" / "notes.txt").write_text("KEY")
    (project / "notes.txt").write_text("harmless")
    (project / "link").symlink_to(home / ".ssh" / "deep")
    assert decide(f"{project}/link/../notes.txt").rule_id == "secrets.files"


@pytest.mark.parametrize("command", [
    "cat {project}/link/../notes.txt",
    "echo x > {project}/link/../notes.txt",
    "cp {project}/link/../notes.txt /tmp/out",
])
def test_shell_operands_and_redirections_resolve_a_symlink_before_the_parent_component(home, project, command):
    (home / ".ssh" / "deep").mkdir(parents=True)
    (home / ".ssh" / "notes.txt").write_text("KEY")
    (project / "notes.txt").write_text("harmless")
    (project / "link").symlink_to(home / ".ssh" / "deep")
    call = engine.ToolCall("shell", command=command.format(project=project), cwd=str(project),
                           project_dir=str(project))
    result = engine.evaluate(policy_of(), call)
    assert result.effect == "deny", (command, result.rule_id)


@pytest.mark.parametrize("command", [
    "cat {project}/link/../spec*.txt",
    "cat {project}/link/../specifi?.txt",
    "cat < {project}/link/../spec*.txt",
])
def test_wildcards_are_listed_after_the_symlink_is_followed(home, project, command):
    """`<project>/link/../spec*.txt` lists the *target's* parent (~/safe), not <project>: folding `..` first lists the
    wrong directory, finds nothing, and a deny rule on the physical file is never consulted."""
    (home / "safe" / "deep").mkdir(parents=True)
    (home / "safe" / "specific.txt").write_text("x")
    (project / "link").symlink_to(home / "safe" / "deep")
    rule = f'\n[[rule]]\nid = "no-specific"\ntool = "file"\neffect = "deny"\npaths = ["{home}/safe/specific.txt"]\n'
    text = GUARDED + rule
    call = engine.ToolCall("shell", command=command.format(project=project), cwd=str(project),
                           project_dir=str(project))
    result = engine.evaluate(policy_of(text), call)
    assert result.effect == "deny", (command, result.rule_id)
