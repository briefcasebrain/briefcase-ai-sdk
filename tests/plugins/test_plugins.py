"""Plugin entry points, end to end.

Two throwaway distributions (``tests/plugins/fixtures/bc-plugin-alpha`` and ``-beta``) are
built with pip into a temporary ``--target`` directory, then every ``briefcase.*`` group is
exercised against them: in process (the target directory on ``sys.path``) and through the
real CLI in a subprocess (the target directory on ``PYTHONPATH``).

alpha provides one working plugin per group. beta provides a plugin that fails on import, a
name a built-in already uses, a name alpha already uses, a plugin declaring a future plugin
API, and an entry point naming something that is not a class.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from briefcase import plugins

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DISTS = ("bc-plugin-alpha", "bc-plugin-beta")


@pytest.fixture(scope="session")
def plugin_site(tmp_path_factory):
    """pip-install both fixture distributions into a fresh --target directory."""
    work = tmp_path_factory.mktemp("plugin-src")
    target = tmp_path_factory.mktemp("plugin-site")
    sources = []
    for name in DISTS:
        shutil.copytree(FIXTURES / name, work / name)
        sources.append(str(work / name))
    try:
        import setuptools  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("setuptools is needed to build the fixture plugins offline")
    done = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation",
         "--no-index", "--target", str(target), *sources],
        capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stderr
    return target


@pytest.fixture
def site(plugin_site, monkeypatch):
    """The fixture plugins importable in this process, with a fresh plugin cache."""
    monkeypatch.syspath_prepend(str(plugin_site))
    monkeypatch.delenv(plugins.ENV_SWITCH, raising=False)
    monkeypatch.delenv(plugins.ENV_DISABLE, raising=False)
    for name in [m for m in sys.modules if m.split(".")[0] in ("bc_alpha", "bc_beta")]:
        monkeypatch.delitem(sys.modules, name)
    plugins.clear_cache()
    yield plugin_site
    plugins.clear_cache()
    for name in [m for m in sys.modules if m.split(".")[0] in ("bc_alpha", "bc_beta")]:
        sys.modules.pop(name, None)


def _cli(site_dir, *args, home, env=None):
    environment = dict(os.environ)
    environment.update({
        "PYTHONPATH": os.pathsep.join([str(site_dir), str(ROOT)]),
        "HOME": str(home), "BRIEFCASE_HOME": str(home / ".briefcase"), "NO_COLOR": "1",
        "BRIEFCASE_NO_INPUT": "1",
    })
    environment.pop(plugins.ENV_SWITCH, None)
    environment.pop(plugins.ENV_DISABLE, None)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "briefcase.cli", *args], capture_output=True, text=True,
                          env=environment, cwd=str(home), timeout=120)


def _state(group, name, dist=None):
    for plugin in plugins.entries(group):
        if plugin.name == name and (dist is None or plugin.dist == dist):
            return plugin.state
    raise AssertionError(f"{group}:{name} not found")


# -- discovery --------------------------------------------------------------------------------

def test_discovery_reads_metadata_only_and_is_ordered(site):
    found = plugins.all_plugins()
    keys = [(p.group, p.name, p.dist) for p in found]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1], k[2]))
    assert {p.group for p in found} == set(plugins.GROUPS)
    assert {p.dist for p in found} == set(DISTS)
    assert all(p.version in ("0.1.0", "0.2.0") for p in found)
    assert not [m for m in sys.modules if m.startswith(("bc_alpha", "bc_beta"))]


def test_unknown_group_is_a_usage_error():
    with pytest.raises(ValueError, match="unknown plugin group"):
        plugins.entries("briefcase.nope")


# -- one test per group ---------------------------------------------------------------------

def test_exporter_plugin_resolves_from_observe_and_env(site, tmp_path, monkeypatch):
    import briefcase
    from briefcase._observe import _exporter_from_env, check_environment

    out = tmp_path / "alpha.jsonl"
    exporter = briefcase.observe(f"alpha:{out}")
    assert type(exporter).__name__ == "AlphaExporter" and exporter.path == str(out)

    @briefcase.capture(async_capture=False)
    def classify(text):
        return text.upper()

    classify("hi")
    assert json.loads(out.read_text().splitlines()[0])["fn"].endswith("classify")

    env = {"BRIEFCASE_EXPORTER": f"alpha:{out}"}
    assert check_environment(env) is None
    assert type(_exporter_from_env(env)).__name__ == "AlphaExporter"
    # check_environment imports nothing, so a plugin that has not been loaded yet passes.
    assert check_environment({"BRIEFCASE_EXPORTER": "broken"}) is None
    assert "is not a destination" in check_environment({"BRIEFCASE_EXPORTER": "nope"})
    briefcase.observe("memory")


def test_bitemporal_backend_plugin(site):
    from briefcase.bitemporal.backends import available_backends, get_backend, open_backend

    assert "alphadb" in available_backends()
    store = open_backend("alphadb")
    assert type(store).__name__ == "AlphaBackend" and len(store) == 0
    assert get_backend("sqlite").__name__ == "SqliteBitemporalBackend"
    with pytest.raises(KeyError, match="no bitemporal backend 'nope'"):
        get_backend("nope")


def test_vcs_provider_plugin_and_api_refusal(site):
    from briefcase.integrations.vcs import available_providers, get_provider

    client = get_provider("alphavcs")("repo")
    client.write_object("a.txt", b"hello")
    assert client.read_object("a.txt") == b"hello"
    assert "alphavcs" in available_providers()
    with pytest.raises(KeyError, match="needs briefcase plugin API 99"):
        get_provider("future")
    assert _state("briefcase.vcs_providers", "future") == plugins.REFUSED


def test_receiver_source_plugin(site):
    from briefcase.exporters.memory import MemoryExporter
    from briefcase.receiver.server import Receiver

    memory = MemoryExporter()
    receiver = Receiver(memory, async_export=False)
    body = json.dumps({"session": "s1", "calls": [{"tool": "grep", "ok": True, "ms": 12}]}).encode()
    status, _, _ = receiver.handle_post("/v1/sources/alpha", "application/json", body)
    assert status == 200
    (record,) = memory.records
    assert record["decision_type"] == "tool_call"
    assert record["function_name"] == "alpha.tool.grep"
    assert record["inputs"]["session_id"] == "s1"
    status, payload, _ = receiver.handle_post("/v1/sources/nope", "application/json", b"{}")
    assert status == 404 and b"no receiver source" in payload
    status, _, _ = receiver.handle_post("/v1/sources/cursor", "application/json", b"{}")
    assert status == 404  # built-in names never route to a plugin


def test_connect_adapter_plugin(site, tmp_path, monkeypatch):
    from briefcase.connect.adapters import Context, registry, resolve_names
    from briefcase.connect.edits import Manual

    adapters = registry()
    assert "alphatool" in adapters and adapters["alphatool"].name == "Alpha Tool"
    assert "alphatool" not in registry(plugins=False)
    assert resolve_names(["alphatool"]) == ["alphatool"]
    (item,) = adapters["alphatool"].plan(Context(endpoint="http://127.0.0.1:4318"))
    assert isinstance(item, Manual) and "ALPHATOOL_OTLP_ENDPOINT" in item.text

    done = _cli(site, "connect", "alphatool", "--dry-run", "--no-mcp", home=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "ALPHATOOL_OTLP_ENDPOINT" in done.stdout


def test_guardrail_plugin(site):
    from briefcase.guardrails import EvalRequest
    from briefcase.guardrails.framework import Effect, list_registered, make

    env = make("alpha-check-v1", blocked=["delete"])
    assert env.evaluate(EvalRequest("a", "delete", "r")).effect == Effect.DENY
    assert env.evaluate(EvalRequest("a", "read", "r")).effect == Effect.ALLOW
    assert "alpha-check-v1" in [spec.id for spec in list_registered()]
    with pytest.raises(KeyError, match="is not a class"):
        make("not-a-class")
    import briefcase.guardrails.envs  # noqa: F401  built-ins register next to the plugins
    assert make("rbac-env-v1", agents=["a"], allowed_actions=["read"]) is not None


def test_cli_command_plugin(site, tmp_path):
    done = _cli(site, "alpha", "hello", "--name", "you", home=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "hello, you (from bc-plugin-alpha)" in done.stdout
    done = _cli(site, "alpha", "--help", home=tmp_path)
    assert done.returncode == 0 and "Say hello" not in done.stderr and "hello" in done.stdout


def test_cli_plugin_that_fails_to_import_exits_1_with_a_fix(site, tmp_path):
    done = _cli(site, "beta-broken", home=tmp_path)
    assert done.returncode == 1
    assert "import failed" in done.stderr and "Fix: briefcase-ai plugins show beta-broken" in done.stderr
    assert "Traceback" not in done.stderr


def test_builtin_command_wins_over_a_plugin(site, tmp_path):
    done = _cli(site, "status", "--json", home=tmp_path)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert any(item["id"] == "plugins" and item["state"] == "on" for item in report["items"])
    # A plugin command's status() hook gets a row, under the GROUP its module names.
    assert any(item["id"] == "alpha" and item["group"] == "review" for item in report["items"])


def test_help_lists_plugin_commands_without_importing_any_plugin(site, tmp_path):
    probe = (
        "import sys\n"
        "from briefcase.cli.__main__ import main\n"
        "try:\n    main(['--help'])\nexcept SystemExit:\n    pass\n"
        "print('IMPORTED', sorted(m for m in sys.modules if m.startswith(('bc_alpha', 'bc_beta'))))\n"
    )
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(site), str(ROOT)]), NO_COLOR="1")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=environment,
                          cwd=str(tmp_path), timeout=60)
    assert done.returncode == 0, done.stderr
    assert "Plugins: commands from installed packages" in done.stdout
    assert "Plugin command from bc-plugin-alpha 0.1.0." in done.stdout
    assert "IMPORTED []" in done.stdout


def test_features_plugin(site, tmp_path):
    from briefcase.features import load_catalog

    feature = load_catalog(plugins=True).get("alpha-export")
    assert feature is not None and feature.extra["plugin"] == "bc-plugin-alpha"
    assert feature.docs_url == "https://example.invalid/bc-plugin-alpha#readme"
    assert load_catalog().get("alpha-export") is None
    done = _cli(site, "features", "--json", home=tmp_path)
    ids = [f["id"] for g in json.loads(done.stdout)["groups"] for f in g["features"]]
    assert "alpha-export" in ids
    done = _cli(site, "features", "--markdown", home=tmp_path)
    assert "alpha-export" not in done.stdout and "Alpha exporter" not in done.stdout


# -- isolation, collisions, switches ---------------------------------------------------------

def test_broken_plugin_logs_one_warning_naming_the_distribution(site, caplog):
    import briefcase

    with caplog.at_level(logging.WARNING, logger="briefcase.plugins"):
        for _ in range(3):
            with pytest.raises(ValueError, match="import failed"):
                briefcase.observe("broken")
    warnings = [r for r in caplog.records if "exporters:broken" in r.getMessage()]
    assert len(warnings) == 1
    assert "bc-plugin-beta 0.2.0" in warnings[0].getMessage()
    assert _state("briefcase.exporters", "broken") == plugins.SKIPPED


def test_collisions_are_resolved_explicitly(site):
    import briefcase
    from briefcase.exporters.console import ConsoleExporter

    assert isinstance(briefcase.observe("console"), ConsoleExporter)
    plugins.get_all("briefcase.exporters", plugins.builtin_names("briefcase.exporters"))
    assert _state("briefcase.exporters", "console") == plugins.SHADOWED
    assert _state("briefcase.exporters", "alpha", "bc-plugin-alpha") == plugins.LOADED
    assert _state("briefcase.exporters", "alpha", "bc-plugin-beta") == plugins.SKIPPED
    briefcase.observe("memory")


def test_disable_list(site, monkeypatch):
    import briefcase

    monkeypatch.setenv(plugins.ENV_DISABLE, "exporters:alpha, bc-plugin-beta")
    plugins.clear_cache()
    assert _state("briefcase.exporters", "alpha", "bc-plugin-alpha") == plugins.DISABLED
    assert _state("briefcase.receiver_sources", "alpha") == plugins.AVAILABLE
    assert all(p.state == plugins.DISABLED for p in plugins.all_plugins() if p.dist == "bc-plugin-beta")
    with pytest.raises(ValueError, match="disabled"):
        briefcase.observe("alpha:x.jsonl")
    from briefcase._observe import check_environment

    assert "named in BRIEFCASE_PLUGINS_DISABLE" in check_environment({"BRIEFCASE_EXPORTER": "alpha:x"})


def test_plugins_off(site, monkeypatch, tmp_path):
    monkeypatch.setenv(plugins.ENV_SWITCH, "off")
    plugins.clear_cache()
    assert {p.state for p in plugins.all_plugins()} == {plugins.DISABLED}
    from briefcase.integrations.vcs import get_provider

    with pytest.raises(KeyError, match="BRIEFCASE_PLUGINS=off"):
        get_provider("alphavcs")
    done = _cli(site, "alpha", "hello", home=tmp_path, env={plugins.ENV_SWITCH: "off"})
    assert done.returncode == 2  # no such command when plugins are off
    done = _cli(site, "plugins", "list", home=tmp_path, env={plugins.ENV_SWITCH: "off"})
    assert "Plugins are turned off" in done.stdout


def test_api_version_check():
    class Declares:
        BRIEFCASE_PLUGIN_API = plugins.PLUGIN_API_VERSION

    class Future:
        BRIEFCASE_PLUGIN_API = plugins.PLUGIN_API_VERSION + 1

    class Bad:
        BRIEFCASE_PLUGIN_API = "1"

    class Silent:
        pass

    assert plugins.check_api(Declares) == (plugins.PLUGIN_API_VERSION, None)
    version, refusal = plugins.check_api(Future)
    assert version == plugins.PLUGIN_API_VERSION + 1 and "pip install -U briefcase-ai" in refusal
    assert "must be an integer" in plugins.check_api(Bad)[1]
    assert plugins.check_api(Silent) == (None, None)


# -- reporting -------------------------------------------------------------------------------

def test_plugins_list_and_show(site, tmp_path):
    done = _cli(site, "plugins", "list", "--json", home=tmp_path)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["plugin_api_version"] == plugins.PLUGIN_API_VERSION
    states = {(row["group"], row["name"], row["distribution"]): row["state"] for row in data["plugins"]}
    assert states[("briefcase.exporters", "alpha", "bc-plugin-alpha")] == "loaded"
    assert states[("briefcase.exporters", "broken", "bc-plugin-beta")] == "skipped"
    assert states[("briefcase.vcs_providers", "future", "bc-plugin-beta")] == "refused"
    assert states[("briefcase.cli_commands", "status", "bc-plugin-beta")] == "shadowed"
    assert all(state == "loaded" for (_, _, dist), state in states.items() if dist == "bc-plugin-alpha")

    done = _cli(site, "plugins", "show", "vcs_providers:future", home=tmp_path)
    assert done.returncode == 0
    assert "State:       refused (needs briefcase plugin API 99" in done.stdout
    done = _cli(site, "plugins", "show", "nope", home=tmp_path)
    assert done.returncode == 2 and "Fix: briefcase-ai plugins list" in done.stderr


def test_doctor_plugins_section(site, tmp_path):
    from briefcase.cli import doctor

    checks = {(label, detail.split(";")[0]): severity for severity, label, detail in doctor.plugin_checks()}
    assert checks[("plugin exporters:alpha", "bc-plugin-alpha 0.1.0")] == doctor.OK
    assert checks[("plugin exporters:alpha", "bc-plugin-beta 0.2.0")] == doctor.WARN
    assert checks[("plugin exporters:broken", "bc-plugin-beta 0.2.0")] == doctor.WARN
    assert checks[("plugin exporters:console", "bc-plugin-beta 0.2.0")] == doctor.OK  # shadowed
    done = _cli(site, "doctor", "--plugins", home=tmp_path)
    assert done.returncode == 0
    assert "[warn] plugin vcs_providers:future: bc-plugin-beta 0.2.0; refused" in done.stdout
    done = _cli(site, "doctor", "--plugins", "--strict", home=tmp_path)
    assert done.returncode == 1


def test_no_plugins_installed(monkeypatch, tmp_path):
    monkeypatch.delenv(plugins.ENV_SWITCH, raising=False)
    plugins.clear_cache()
    try:
        from briefcase.cli import doctor, plugins_commands

        assert [p for p in plugins.all_plugins() if p.dist in DISTS] == []
        if not plugins.all_plugins():
            assert plugins_commands.status() is None
            assert doctor.plugin_checks() == []
    finally:
        plugins.clear_cache()
