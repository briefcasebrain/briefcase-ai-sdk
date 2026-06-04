"""briefcase stack: docker-compose invocation via an injected runner (RecordingRunner style), the
scorer-profile gating + tag pinning, the retry-without-scorer fallback, run-logs delegation, and that
the bundled compose + compat.json actually ship and are image-based."""
from argparse import Namespace
from contextlib import contextmanager

import pytest

from briefcase.cli import commands, stack_commands
from briefcase.cli.stack import compose_path, load_compat
from briefcase.cli.stack_commands import Stack
from briefcase.cli.state import Store


class RecordingRunner:
    def __init__(self, returncodes=None):
        self._rcs = list(returncodes or [])
        self.calls = []

    def __call__(self, argv, env=None, **kw):
        self.calls.append({"argv": argv, "env": env})
        rc = self._rcs.pop(0) if self._rcs else 0
        return Namespace(returncode=rc)


@pytest.fixture
def stack_with(tmp_path):
    def make(returncodes=None):
        runner = RecordingRunner(returncodes)

        @contextmanager
        def resolver():
            yield tmp_path / "docker-compose.yml"

        return Stack(runner=runner, compose_resolver=resolver), runner

    return make


def test_stack_up_default_enables_scorer_profile_and_pins_tag(stack_with):
    stack, runner = stack_with()
    assert getattr(stack.up(), "returncode", 0) == 0
    call = runner.calls[-1]
    assert call["argv"][:3] == ["docker", "compose", "-f"]
    assert "--profile" in call["argv"] and "scorer" in call["argv"]
    assert call["argv"][-2:] == ["up", "-d"]
    assert call["env"]["OCI_JJ_IMAGE_TAG"] == "0.1.0"  # pinned from compat.json


def test_stack_up_no_scorer_omits_profile(stack_with):
    stack, runner = stack_with()
    stack.up(scorer=False)
    assert "--profile" not in runner.calls[-1]["argv"]


def test_stack_up_retries_without_scorer_on_failure(stack_with):
    # scorer 'up' fails (private image unpullable); the handler retries without it and succeeds.
    stack, runner = stack_with(returncodes=[1, 0])
    rc = stack_commands.cmd_stack_up(Namespace(no_scorer=False), None, None, stack)
    assert rc == 0
    assert len(runner.calls) == 2
    assert "--profile" in runner.calls[0]["argv"]
    assert "--profile" not in runner.calls[1]["argv"]


def test_stack_down_status_logs(stack_with):
    stack, runner = stack_with()
    stack.down()
    assert runner.calls[-1]["argv"][-1] == "down"
    stack.down(volumes=True)
    assert runner.calls[-1]["argv"][-1] == "-v"
    stack.status()
    assert runner.calls[-1]["argv"][-1] == "ps"
    stack.logs(service="verdict-worker", follow=True)
    assert runner.calls[-1]["argv"][-3:] == ["logs", "-f", "verdict-worker"]


def test_stack_logs_all_services_when_no_service(stack_with):
    stack, runner = stack_with()
    stack.logs()
    assert runner.calls[-1]["argv"][-1] == "logs"


def test_compose_override_env_wins(monkeypatch, stack_with, tmp_path):
    override = tmp_path / "override-compose.yml"
    override.write_text("x")
    monkeypatch.setenv("BRIEFCASE_OCIJJ_COMPOSE", str(override))
    stack, runner = stack_with()
    stack.status()
    assert runner.calls[-1]["argv"][:4] == ["docker", "compose", "-f", str(override)]


def test_run_logs_delegates_to_stack(tmp_path):
    store = Store(home=tmp_path)
    store.record_run({"name": "demo", "status": "submitted"})
    runner = RecordingRunner()

    @contextmanager
    def resolver():
        yield tmp_path / "docker-compose.yml"

    stack = Stack(runner=runner, compose_resolver=resolver)
    rc = commands.cmd_run_logs(Namespace(id="demo", follow=True), store, None, stack)
    assert rc == 0
    assert runner.calls[-1]["argv"][-3:] == ["logs", "-f", "verdict-worker"]


def test_run_logs_unknown_run_returns_2(tmp_path):
    store = Store(home=tmp_path)
    assert commands.cmd_run_logs(Namespace(id="missing", follow=False), store, None, None) == 2


def test_bundled_compose_is_image_based_not_build():
    with compose_path() as p:
        text = p.read_text()
    assert "image: ghcr.io/briefcasebrain/oci-jj-server" in text
    # no service uses a build: context (the indented directive); the word may appear in a comment
    assert "\n    build:" not in text


def test_compat_manifest_loads():
    compat = load_compat()
    assert compat["oci_jj_api_min"] == "v1"
    assert compat["oci_jj_image_tag"] == "0.1.0"
    assert compat["grpc_service"] == "oci_jj.v1.VcsService"
