"""Policy file parsing, validation and layer merging."""

import pytest

from briefcase.policy import engine, model

GUARDED = 'version = 1\npreset = "guarded"\n'


def layer(text, source="user"):
    return model.parse_layer(text, source, f"/{source}/policy.toml")


def decide(policy, command=None, **kw):
    kind = kw.pop("kind", "shell")
    call = engine.ToolCall(kind, command=command, cwd="/proj", project_dir="/proj", **kw)
    return engine.evaluate(policy, call)


# --- parsing ------------------------------------------------------------------

def test_minimal_file_gets_preset_defaults():
    policy = model.merge([layer(GUARDED)])
    assert policy.settings["mode"] == "enforce" and policy.settings["fail_closed"] is False
    assert policy.settings["on_unparseable"] == "ask" and policy.settings["ask_fallback"] == "deny"
    assert policy.settings["protect_self"] is True
    assert {r.id for r in policy.groups[0]} >= {"secrets.files", "shell.rm-root", "pkg.install", "net.exfil"}
    assert [r.id for r in policy.protect] == ["self.files", "self.commands"]


def test_observe_preset_has_no_rules_and_never_blocks():
    policy = model.merge([layer('version = 1\npreset = "observe"\n')])
    assert policy.all_rules() == [] and policy.protect == []
    assert policy.settings["mode"] == "observe"
    assert decide(policy, "rm -rf /").effect == "allow"


def test_strict_is_a_superset_of_guarded():
    guarded = {r.id for r in model.merge([layer(GUARDED)]).all_rules()}
    strict = {r.id for r in model.merge([layer('version = 1\npreset = "strict"\n')]).all_rules()}
    assert guarded < strict
    assert {"shell.sudo", "write.outside-project", "mcp.any", "web.any"} <= strict


@pytest.mark.parametrize("text, needle", [
    ("preset = \"guarded\"\n", "missing `version"),
    ("version = 2\n", "must be 1"),
    ("version = 1\npresett = \"guarded\"\n", "unknown key 'presett'"),
    ("version = 1\npreset = \"paranoid\"\n", "must be one of: observe, guarded, strict"),
    ("version = 1\nfail_closed = \"yes\"\n", "must be true or false"),
    ("version = 1\n[[rule]]\ntool = \"shell\"\n", "missing `effect`"),
    ("version = 1\n[[rule]]\ntool = \"shel\"\neffect = \"deny\"\n", "unknown tool 'shel'"),
    ("version = 1\n[[rule]]\ntool = \"shell\"\neffect = \"block\"\n", "must be one of: allow, ask, deny"),
    ("version = 1\n[[rule]]\ntool = \"shell\"\neffect = \"deny\"\npaths = [\"x\"]\n", "`paths` does not apply"),
    ("version = 1\n[[rule]]\ntool = \"shell\"\neffect = \"deny\"\ncomand = \"rm\"\n", "unknown key 'comand'"),
    ("version = 1\n[[rule]]\ntool = \"shell\"\neffect = \"deny\"\ncommand = \"rm 'x\"\n", "command pattern"),
    ("version = 1\n[[rule]]\ntool = \"shell\"\neffect = \"deny\"\ncommand = \"| sh\"\n", "needs a command"),
    ("version = 1\n[[rule]]\nid = \"a\"\ntool = \"mcp\"\neffect = \"ask\"\n[[rule]]\nid = \"a\"\ntool = \"web\"\n"
     "effect = \"ask\"\n", "share the id"),
    ("version = 1\n[rule]\ntool = \"shell\"\n", "array of tables"),
    ("version = 1\nrules = []\n", "written as [[rule]]"),
    ("version = 1\npreset = \n", "not valid TOML"),
    ("version = 1\n[[rule]]\ntool = [\"shell\", \"mcp\"]\neffect = \"deny\"\n", "one rule per tool"),
])
def test_invalid_files_are_rejected_with_a_reason(text, needle):
    with pytest.raises(model.PolicyError) as error:
        layer(text)
    assert needle in str(error.value)


def test_a_rejected_file_contributes_nothing(home, write_policy):
    # The first rule is fine and the second has a typo: neither is applied.
    write_policy(GUARDED + '[[rule]]\ntool = "shell"\neffect = "deny"\ncommand = "make deploy"\n'
                           '[[rule]]\ntool = "shell"\neffect = "denny"\ncommand = "x"\n')
    loaded = model.load(str(home))
    assert loaded.policy is None
    assert len(loaded.errors) == 1 and "denny" in loaded.errors[0][2]


def test_file_and_tool_aliases():
    rule = layer(GUARDED + '[[rule]]\ntool = "file"\neffect = "deny"\npaths = ["x"]\n').rules[0]
    assert rule.kinds == ("read", "write") and rule.id == "user.1"


def test_declares_fail_closed_survives_a_syntax_error(tmp_path):
    broken = tmp_path / "policy.toml"
    broken.write_text('version = 1\nfail_closed = true   # keep\npreset = \n')
    assert model.declares_fail_closed(str(broken))
    broken.write_text('version = 1\n# fail_closed = true\nfail_closed = false\n')
    assert not model.declares_fail_closed(str(broken))


# --- rule order inside the base policy ------------------------------------------

def test_user_rules_are_checked_before_the_preset_and_first_match_wins():
    policy = model.merge([layer(GUARDED + '[[rule]]\nid = "ok-pip"\ntool = "shell"\neffect = "allow"\n'
                                          'command = "pip install -r"\n')])
    assert decide(policy, "pip install -r requirements.txt").rule_id == "ok-pip"
    assert decide(policy, "pip install -r requirements.txt").effect == "allow"
    assert decide(policy, "pip install requests").effect == "ask"


def test_an_allow_rule_for_a_command_does_not_allow_the_secret_file_it_reads():
    policy = model.merge([layer(GUARDED + '[[rule]]\ntool = "shell"\neffect = "allow"\ncommand = "cat"\n')])
    assert decide(policy, "cat README.md").effect == "allow"
    assert decide(policy, "cat ~/.ssh/id_rsa").rule_id == "secrets.files"


def test_disable_switches_a_preset_rule_off():
    policy = model.merge([layer(GUARDED + 'disable = ["pkg.install", "no-such-rule"]\n')])
    assert decide(policy, "pip install requests").effect == "allow"
    assert any("no-such-rule" in note for note in policy.notes)


def test_protection_cannot_be_preempted_or_disabled_by_a_rule(home):
    policy = model.merge([layer(GUARDED + 'disable = ["self.files"]\n'
                                          '[[rule]]\ntool = "file"\neffect = "allow"\n')])
    result = decide(policy, kind="write", paths=[str(home / ".briefcase" / "policy.toml")])
    assert result.effect == "deny" and result.rule_id == "self.files"
    off = model.merge([layer(GUARDED + "protect_self = false\n")])
    assert decide(off, kind="write", paths=[str(home / ".briefcase" / "policy.toml")]).effect == "allow"


def test_default_applies_to_calls_no_rule_matches():
    policy = model.merge([layer(GUARDED + 'default = "ask"\n[[rule]]\ntool = "shell"\neffect = "allow"\n'
                                          'command = "git status"\n')])
    assert decide(policy, "git status").effect == "allow"
    assert decide(policy, "ls").rule_id == "default"
    assert decide(policy, "git status && ls").effect == "ask"


# --- merging: a weaker file may only make things stricter ----------------------------

USER = GUARDED + '''
[[rule]]
id = "user-allow-npm"
tool = "shell"
effect = "allow"
command = "npm install"
'''


def merged(project_text, user_text=USER):
    return model.merge([layer(user_text, "user"), layer(project_text, "project")])


def test_project_deny_wins_over_a_user_allow_that_matched_first():
    policy = merged('version = 1\n[[rule]]\nid = "no-npm"\ntool = "shell"\neffect = "deny"\n'
                    'command = "npm install"\n')
    result = decide(policy, "npm install left-pad")
    assert result.effect == "deny" and result.rule_id == "no-npm"
    assert {m.rule_id for m in result.matches} == {"user-allow-npm", "no-npm"}


def test_project_allow_rules_are_ignored_and_reported():
    policy = merged('version = 1\n[[rule]]\nid = "sneaky"\ntool = "file"\neffect = "allow"\npaths = ["**"]\n')
    assert decide(policy, kind="read", paths=["/proj/.env"]).rule_id == "secrets.files"
    assert any("'sneaky' is ignored" in note for note in policy.notes)
    assert "sneaky" not in {r.id for r in policy.all_rules()}


@pytest.mark.parametrize("project, key, expected, noted", [
    ('mode = "observe"', "mode", "enforce", True),
    ('fail_closed = true', "fail_closed", True, False),
    ('fail_closed = false', "fail_closed", False, False),
    ('on_unparseable = "allow"', "on_unparseable", "ask", True),
    ('on_unparseable = "deny"', "on_unparseable", "deny", False),
    ('ask_fallback = "allow"', "ask_fallback", "deny", True),
    ('protect_self = false', "protect_self", True, True),
    ('preset = "observe"', "preset", "guarded", True),
    ('preset = "strict"', "preset", "strict", False),
    ('default = "deny"', "default", "deny", False),
])
def test_project_settings_only_move_towards_stricter(project, key, expected, noted):
    policy = merged(f"version = 1\n{project}\n")
    assert policy.settings[key] == expected
    assert any(f"`{key} = " in note for note in policy.notes) is noted


def test_project_cannot_loosen_a_fail_closed_user_policy():
    policy = merged('version = 1\nfail_closed = false\n', GUARDED + "fail_closed = true\n")
    assert policy.settings["fail_closed"] is True
    assert any("would loosen" in note for note in policy.notes)


def test_project_disable_is_ignored():
    policy = merged('version = 1\ndisable = ["secrets.files"]\n')
    assert "secrets.files" in {r.id for r in policy.all_rules()}
    assert any("`disable` is ignored" in note for note in policy.notes)


def test_stricter_project_preset_brings_its_rules():
    policy = merged('version = 1\npreset = "strict"\n')
    assert decide(policy, kind="mcp", server="github", name="create_issue").effect == "ask"


def test_project_adds_protected_branches():
    policy = merged('version = 1\nprotected_branches = ["develop"]\n')
    assert "develop" in policy.settings["protected_branches"] and "main" in policy.settings["protected_branches"]
    assert decide(policy, "git push --force origin develop").effect == "deny"


def test_system_policy_is_the_base_and_the_user_policy_can_only_restrict(home, write_policy):
    write_policy(GUARDED + '[[rule]]\nid = "sys"\ntool = "shell"\neffect = "deny"\ncommand = "terraform apply"\n',
                 "system")
    write_policy('version = 1\npreset = "observe"\nmode = "observe"\nprotect_self = false\n'
                 '[[rule]]\nid = "mine"\ntool = "shell"\neffect = "allow"\ncommand = "terraform apply"\n')
    loaded = model.load(str(home))
    assert [source for source, _, _ in loaded.paths] == ["system", "user"]
    policy = loaded.policy
    assert policy.settings["mode"] == "enforce" and policy.settings["protect_self"] is True
    assert decide(policy, "terraform apply").rule_id == "sys"


# --- loading from disk -----------------------------------------------------------

def test_load_finds_the_project_policy_from_a_subdirectory(home, project, write_policy):
    write_policy(GUARDED)
    write_policy('version = 1\n[[rule]]\nid = "p"\ntool = "shell"\neffect = "deny"\ncommand = "make deploy"\n',
                 "project")
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    loaded = model.load(str(nested))
    assert [source for source, _, ok in loaded.paths if ok] == ["user", "project"]
    assert decide(loaded.policy, "make deploy").rule_id == "p"


def test_the_user_policy_is_never_also_the_project_policy(home, write_policy):
    write_policy(GUARDED)
    loaded = model.load(str(home))        # cwd is the home directory itself
    assert [source for source, _, _ in loaded.paths] == ["user"]


def test_invalid_project_policy_leaves_the_user_policy_whole(home, project, write_policy):
    write_policy(GUARDED)
    write_policy("version = 1\nnonsense = true\n", "project")
    loaded = model.load(str(project))
    assert loaded.policy is not None and len(loaded.errors) == 1
    assert decide(loaded.policy, "rm -rf /").effect == "deny"


def test_invalid_user_policy_means_no_policy_even_with_a_valid_project_policy(home, project, write_policy):
    # Promoting the project file to base would let it allow what the user policy denied.
    write_policy("version = 1\npreset = \n")
    write_policy('version = 1\n[[rule]]\ntool = "file"\neffect = "allow"\n', "project")
    loaded = model.load(str(project))
    assert loaded.policy is None and loaded.errors[0][0] == "user"


def test_fail_closed_is_seen_in_a_file_that_does_not_parse(home, write_policy):
    write_policy("version = 1\nfail_closed = true\npreset = \n")
    loaded = model.load(str(home))
    assert loaded.policy is None and loaded.fail_closed is True


def test_no_files_means_no_policy_and_no_error(home):
    loaded = model.load(str(home))
    assert loaded.policy is None and loaded.errors == [] and loaded.paths == []


def test_template_is_a_valid_policy_for_every_preset():
    from briefcase.policy import template

    for preset in model.PRESETS:
        text = template.render(preset, fail_closed=preset == "strict")
        parsed = layer(text)
        assert parsed.settings["preset"] == preset and parsed.rules == []
        assert parsed.settings["fail_closed"] is (preset == "strict")
        assert model.merge([parsed]).settings == dict(model._defaults(preset), fail_closed=preset == "strict")
