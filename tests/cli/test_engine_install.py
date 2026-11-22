"""`briefcase engine install|up|down|status|logs|doctor|uninstall|upgrade`, offline.

A fake platform (tests/cli/engine_fakes.py) answers the credentials call and hosts the release
asset behind a GitHub-style 302 to a signed URL; a fake `docker` on PATH records every call."""
import json
import os
import stat

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main
from briefcase.engine import compose, install

from tests.cli.engine_fakes import (
    DIGESTS, DOWNLOAD_TOKEN, REGISTRY_PASSWORD, FakePlatform, docker_calls, install_fake_docker,
)


@pytest.fixture
def env(shell_home, tmp_path, monkeypatch):
    platform = FakePlatform(target={"os": "linux", "arch": "x86_64"})
    monkeypatch.setattr(install, "host_target", lambda: {"os": "linux", "arch": "x86_64"})
    log = tmp_path / "docker.log"
    install_fake_docker(tmp_path / "bin", log, monkeypatch)
    monkeypatch.setenv("BRIEFCASE_LICENSE_TOKEN", "good")
    monkeypatch.setenv("BRIEFCASE_PLATFORM_URL", platform.url)
    for name in ("OCI_JJ_SERVER", "OCI_JJ_AUTH_TOKEN", "OCI_JJ_TLS_CA", "OCI_JJ_REPO"):
        monkeypatch.delenv(name, raising=False)
    yield {"platform": platform, "log": log, "home": shell_home / ".briefcase"}
    platform.close()


def _install(*extra):
    return main(["engine", "install", "--yes", *extra])


def test_install_downloads_verifies_pulls_and_records(env, capsys):
    assert _install() == 0
    out = capsys.readouterr().out
    assert "engine 0.2.0 installed" in out and "Next: briefcase engine up" in out
    assert "not signed; only its sha256 was checked" in out

    record = _engine.current_install()
    assert record["version"] == "0.2.0" and record["images"] == DIGESTS
    binary = env["home"] / "engine" / "0.2.0" / "bin" / "oci-bai"
    assert record["binary"] == str(binary) and os.access(binary, os.X_OK)

    # The asset request carried the short-lived download token; the signed URL did not.
    gets = [r for r in env["platform"].requests if r[0] == "GET"]
    assert gets[0][2] == f"Bearer {DOWNLOAD_TOKEN}"
    assert env["platform"].downloads() and all(r[2] == "" for r in env["platform"].downloads())

    calls = [c["argv"] for c in docker_calls(env["log"])]
    login = next(c for c in docker_calls(env["log"]) if c["argv"][0] == "login")
    assert login["argv"] == ["login", "ghcr.io", "--username", "x-access-token", "--password-stdin"]
    assert login["stdin"] == REGISTRY_PASSWORD
    assert not any(REGISTRY_PASSWORD in " ".join(c) for c in calls)  # never in argv
    pulls = [c[1] for c in calls if c[0] == "pull"]
    assert sorted(pulls) == sorted(DIGESTS.values())
    assert calls[-1] == ["logout", "ghcr.io"]
    assert calls.index(["logout", "ghcr.io"]) > max(i for i, c in enumerate(calls) if c[0] == "pull")

    text = (env["home"] / "engine" / "0.2.0" / "docker-compose.yml").read_text()
    for ref in DIGESTS.values():
        assert f"image: {ref}" in text
    assert "oci_jj_secret" not in text and "POSTGRES_PASSWORD: oci_jj\n" not in text
    assert "OCI_JJ_AUTH_TOKENS: ${OCI_JJ_WORKER_TOKEN}:platform:admin,${OCI_JJ_TENANT_TOKENS}\n" in text
    assert '127.0.0.1:50051:50051' in text and 'OCI_JJ_ALLOW_PLAINTEXT: "1"' in text

    secrets = env["home"] / "engine" / "engine.env"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    values = _engine.read_env_file(secrets)
    assert set(compose.SECRET_KEYS) <= set(values) and all(values[k] for k in compose.SECRET_KEYS)
    assert REGISTRY_PASSWORD not in secrets.read_text() and DOWNLOAD_TOKEN not in secrets.read_text()

    # Settings: the CLI now finds the local engine and its tenant token on its own.
    settings = _engine.client_settings()
    assert settings["addr"] == "http://127.0.0.1:50051" and settings["source"] == "local install"
    tenant, reader = values["OCI_JJ_TENANT_TOKENS"].split(",")
    assert tenant.endswith(":local") and reader.endswith(":local:reader")
    assert settings["token"] == tenant.split(":")[0]


def test_install_is_idempotent_and_keeps_secrets(env, capsys, monkeypatch):
    assert _install() == 0
    first = (env["home"] / "engine" / "engine.env").read_text()
    downloads = len(env["platform"].downloads())
    env["log"].unlink()
    monkeypatch.setenv("FAKE_DOCKER_HAVE", "all")
    assert _install() == 0
    assert "already installed" in capsys.readouterr().out
    assert len(env["platform"].downloads()) == downloads  # the verified file was reused
    assert not any(c["argv"][0] in ("login", "pull") for c in docker_calls(env["log"]))
    assert (env["home"] / "engine" / "engine.env").read_text() == first


def test_interrupted_download_resumes_with_range(env):
    asset = env["platform"].asset
    part = env["home"] / "engine" / "0.2.0" / "download" / "oci-bai-0.2.0-linux-x86_64.tar.gz.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(asset[:40])
    assert _install() == 0
    signed = env["platform"].downloads()[-1]
    assert signed[3].get("Range") == "bytes=40-"


def test_server_ignoring_range_restarts_the_download(env):
    env["platform"].honour_range = False
    part = env["home"] / "engine" / "0.2.0" / "download" / "oci-bai-0.2.0-linux-x86_64.tar.gz.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"garbage")
    assert _install() == 0


def test_sha256_mismatch_stops_and_removes_the_partial_file(env, capsys):
    env["platform"].wrong_sha = True
    assert _install() == 1
    err = capsys.readouterr().err
    assert "sha256 mismatch" in err and "Fix: briefcase engine install" in err
    assert not list((env["home"] / "engine" / "0.2.0" / "download").glob("*"))
    assert _engine.current_install() is None


def test_dry_run_shows_the_plan_and_writes_nothing(env, capsys):
    assert main(["engine", "install", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "docker pull " + DIGESTS["server"] in out and "dry run: nothing" in out
    assert not (env["home"] / "engine").exists()
    assert main(["engine", "install", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] and plan["plan"]["version"] == "0.2.0" and plan["plan"]["signed"] is False
    assert not any(c["argv"][0] in ("login", "pull") for c in docker_calls(env["log"]))


def test_needs_confirmation_without_a_terminal(env, capsys):
    assert main(["engine", "install"]) == 2
    assert "re-run with --yes" in capsys.readouterr().err
    assert _engine.current_install() is None


@pytest.mark.parametrize("token, message, code", [
    ("noent", "your account isn't entitled to the engine", 1),
    ("expired", "the licence token has expired", 1),
    ("bogus", "did not accept the licence token", 1),
    ("slow", "retry in 42 seconds", 1),
    ("noconfig", "cannot issue engine credentials right now", 1),
])
def test_platform_refusals_are_explained(env, capsys, monkeypatch, token, message, code):
    monkeypatch.setenv("BRIEFCASE_LICENSE_TOKEN", token)
    assert _install() == code
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err


def test_missing_token_and_platform_are_usage_errors(env, capsys, monkeypatch):
    monkeypatch.delenv("BRIEFCASE_LICENSE_TOKEN")
    assert _install() == 2
    assert "no licence token" in capsys.readouterr().err
    monkeypatch.delenv("BRIEFCASE_PLATFORM_URL")
    assert _install() == 2
    assert "--platform-url" in capsys.readouterr().err


def test_no_docker(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_DOWN", "1")
    assert _install() == 1
    assert "Docker is not installed or its daemon is not running" in capsys.readouterr().err
    # The client binary alone still installs.
    assert _install("--no-images") == 0


def test_unsupported_platform(env, capsys, monkeypatch):
    monkeypatch.setattr(install, "host_target", lambda: {"os": "windows", "arch": "x86_64"})
    env["platform"].target = {"os": "linux", "arch": "x86_64"}
    assert _install() == 1
    err = capsys.readouterr().err
    assert "no oci-bai build for windows/x86_64" in err and "linux/x86_64" in err


def test_login_failure_still_logs_out(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_LOGIN_FAIL", "1")
    assert _install() == 1
    assert "docker login ghcr.io failed" in capsys.readouterr().err


def test_undigested_image_is_refused():
    with pytest.raises(install.InstallError, match="not pinned by digest"):
        install._check_credentials({"registry": "r", "username": "u", "password": "p", "expires_at": "x",
                                    "release": {"assets": []}, "images": {"server": "ghcr.io/x:latest"}})
    with pytest.raises(ValueError):
        compose.render({"server": "x:1", "gateway": "x", "workers": "x", "postgres": "x"}, "0.1.0")


def _service(text, name):
    return text.split(f"\n  {name}:\n", 1)[1].split("\n\n", 1)[0]


def test_compose_matches_the_engine_security_model():
    """oci-jj e89ddbf/8a52297/be043d9: only the workers hold a (worker) token; the gateway and
    reconstructor forward callers' tokens; the verdict worker has no object-store credentials;
    datasets live in the tenant-prefixed bucket; the app role password comes from the env."""
    images = dict(DIGESTS, verdict_worker="ghcr.io/briefcasebrain/oci-jj-verdict-worker@sha256:" + "6" * 64)
    text = compose.render(images, "0.2.0")
    assert "OCI_JJ_AUTH_TOKEN:" not in _service(text, "gateway")
    assert "OCI_JJ_AUTH_TOKEN:" not in _service(text, "reconstructor")
    assert "OCI_JJ_AUTH_TOKEN: ${OCI_JJ_WORKER_TOKEN}" in _service(text, "workers")
    verdict = _service(text, "verdict-worker")
    assert "OCI_JJ_AUTH_TOKEN: ${OCI_JJ_WORKER_TOKEN}" in verdict and "MINIO" not in verdict
    server = _service(text, "oci-jj-server")
    assert "OCI_JJ_DATASET_S3_BUCKET: oci-jj-datasets" in server
    assert "oci_jj_app:${OCI_JJ_APP_PASSWORD}@postgres" in server
    assert "OCI_JJ_APP_PASSWORD: ${OCI_JJ_APP_PASSWORD}" in _service(text, "postgres")
    assert "MINIO_DEFAULT_BUCKETS: oci-jj-blobs,oci-jj-datasets" in _service(text, "minio")
    for line in text.splitlines():
        if line.strip().startswith("- \"") and ":" in line and line.count(":") >= 2:
            assert "127.0.0.1:" in line, line  # every published port is loopback-only


def test_tls_compose_variant():
    text = compose.render(dict(DIGESTS), "0.2.0", tls=True)
    assert "OCI_JJ_TLS_CERT: /tls/server.crt" in text and "OCI_JJ_ALLOW_PLAINTEXT" not in text
    assert "OCI_JJ_SERVER_GRPC: https://oci-jj-server:50051" in text and "OCI_JJ_TLS_CA: /tls/server.crt" in text


def test_up_status_logs_down_drive_the_installed_compose(env, capsys, monkeypatch):
    assert _install() == 0
    capsys.readouterr()
    env["log"].unlink()
    assert main(["engine", "up"]) == 0
    assert main(["engine", "logs", "oci-jj-server", "--tail", "5"]) == 0
    assert main(["engine", "down"]) == 0
    compose_calls = [c["argv"] for c in docker_calls(env["log"]) if c["argv"][0] == "compose"]
    for argv in compose_calls:
        assert argv[1:3] == ["--project-name", "briefcase-engine"] and "--env-file" in argv
    verbs = [a[a.index("--env-file") + 2] for a in compose_calls]
    assert verbs == ["up", "logs", "down"]


def test_status_json(env, capsys):
    assert _install() == 0
    capsys.readouterr()
    assert main(["engine", "status", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["installed"] and state["version"] == "0.2.0"
    assert state["services"] == [{"service": "oci-jj-server", "state": "running", "health": "healthy"}]
    assert state["addr_source"] == "local install"


def test_doctor_reports_each_check(env, capsys, monkeypatch):
    from briefcase.cli import engine_commands

    monkeypatch.setattr(engine_commands, "port_open", lambda addr, timeout=0.3: False)  # no engine here
    assert _install() == 0
    monkeypatch.setenv("FAKE_DOCKER_HAVE", "all")
    capsys.readouterr()
    rc = main(["engine", "doctor", "--json"])
    checks = {row["check"]: row for row in json.loads(capsys.readouterr().out)}
    assert checks["docker"]["severity"] == "ok" and checks["client binary"]["severity"] == "ok"
    assert checks["image server"]["severity"] == "ok" and checks["secrets"]["severity"] == "ok"
    assert checks["gRPC"]["severity"] == "warn"  # nothing listens in the test
    assert rc == 0


def test_uninstall_removes_install_and_purge_removes_secrets(env, capsys):
    assert _install() == 0
    assert main(["engine", "uninstall", "--dry-run"]) == 0
    assert _engine.current_install() is not None
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    assert _engine.current_install() is None
    assert not (env["home"] / "engine" / "0.2.0").exists()
    assert not (env["home"] / "engine" / "engine.env").exists()
    rm = [c["argv"] for c in docker_calls(env["log"]) if c["argv"][:2] == ["image", "rm"]]
    assert sorted(c[2] for c in rm) == sorted(DIGESTS.values())


def test_upgrade_restarts_a_running_engine(env, capsys, monkeypatch):
    assert _install() == 0
    state = _engine.load_state()
    state["installs"]["0.1.0"] = dict(state["installs"]["0.2.0"], version="0.1.0")
    state["current"] = "0.1.0"
    from briefcase.engine.install import save_state

    save_state(state)
    monkeypatch.setenv("FAKE_DOCKER_RUNNING", "abc123")
    monkeypatch.setenv("FAKE_DOCKER_HAVE", "all")
    env["log"].unlink()
    assert main(["engine", "upgrade", "--yes"]) == 0
    assert _engine.current_install()["version"] == "0.2.0"
    ups = [c["argv"] for c in docker_calls(env["log"]) if c["argv"][0] == "compose" and "up" in c["argv"]]
    assert ups and "0.2.0" in ups[-1][ups[-1].index("-f") + 1]


def test_stack_alias_prints_one_line_and_uses_the_install(env, capsys):
    assert _install() == 0
    capsys.readouterr()
    env["log"].unlink()
    assert main(["stack", "status"]) == 0
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "use `briefcase engine status`" in err
    argv = docker_calls(env["log"])[0]["argv"]
    assert argv[:3] == ["compose", "--project-name", "briefcase-engine"]


# --- ungated behaviour -----------------------------------------------------------------

GATED = [
    ["graph", "log", "rl-gym-env"],
    ["graph", "diff", "a", "b"],
    ["env", "push", "img:1"],
    ["env", "checkout", "ref"],
    ["env", "gc"],
    ["verify", "--graph", "ref"],
    ["run", "attach", "x", "--result", "v.json", "--repository", "r"],
    ["engine", "up"],
    ["engine", "status"],
]


@pytest.mark.parametrize("argv", GATED)
def test_engine_commands_without_an_install_exit_3(shell_home, capsys, monkeypatch, argv, tmp_path):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    (tmp_path / "project" / "v.json").write_text("{}")
    assert main(argv) == 3
    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert "engine not installed — `briefcase engine install`" in text and "Traceback" not in text


def test_gated_json_error(shell_home, capsys, monkeypatch):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    assert main(["graph", "log", "x", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "engine_not_installed"


def test_dataset_publish_without_engine(shell_home, capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    data = tmp_path / "project" / "d.csv"
    data.write_text("a,target\n1,0\n")
    assert main(["dataset", "publish", "d", str(data)]) == 3
    assert main(["dataset", "publish", "d", str(data), "--dry-run"]) == 0
    assert "s3://oci-jj-datasets/tenants/local/datasets/" in capsys.readouterr().out


def test_help_status_and_features_mark_the_engine(shell_home, capsys, monkeypatch):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    with pytest.raises(SystemExit):
        main(["--help"])
    flat = " ".join(capsys.readouterr().out.split())
    for help_text in ("Install and run the Briefcase engine (oci-jj) locally.",
                      "Engine artifact graph: history, diffs, provenance, search.",
                      "Engine environments: push, compose, check out, bookmark, undo."):
        assert f"{help_text} (engine not installed — `briefcase engine install`)" in flat
    assert main(["status", "--json"]) == 0
    items = {i["id"]: i for i in json.loads(capsys.readouterr().out)["items"]}
    for ident in ("engine", "graph", "env-artifacts"):
        assert items[ident]["state"] == "off"
        assert items[ident]["detail"] == _engine.NOT_INSTALLED and items[ident]["next"] is None
    assert main(["features", "--json"]) == 0
    ids = {f["id"] for g in json.loads(capsys.readouterr().out)["groups"] for f in g["features"]}
    assert {"engine", "graph", "env-artifacts"} <= ids


def test_remote_engine_counts_as_available(shell_home, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "https://engine.example:443")
    assert _engine.available() and _engine.client_settings()["addr"] == "https://engine.example:443"
    assert _engine.client_settings()["token"] is None  # the local install's token is not sent elsewhere


def _grpc_missing():
    try:
        import grpc  # noqa: F401
    except ImportError:
        return True
    return False


@pytest.mark.skipif(not _grpc_missing(), reason="checks the message shown when grpcio is absent")
def test_engine_command_without_grpcio_names_the_extra(shell_home, capsys, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "http://127.0.0.1:50051")
    assert main(["graph", "log", "x"]) == 1
    err = capsys.readouterr().err
    assert "pip install 'briefcase-ai[engine]'" in err and "Traceback" not in err


def test_engine_commands_render_over_an_injected_transport(shell_home, capsys, monkeypatch):
    """Without grpcio too: the commands only see plain dicts from the client."""
    from briefcase.engine import cli_support
    from briefcase.engine.client import EngineClient

    class Transport:
        def unary(self, service, method, request):
            return {"ok": True, "trees_checked": 1, "inodes_checked": 2, "segments_checked": 3}

        def server_stream(self, service, method, request):
            return iter([])

    monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "http://127.0.0.1:50051")
    monkeypatch.setattr(cli_support, "CLIENT_FACTORY",
                        lambda server=None, repo=None: EngineClient(server=server, repo=repo, transport=Transport()))
    assert main(["verify", "--graph", "cand"]) == 0
    assert "intact (1 trees" in capsys.readouterr().out
    assert main(["graph", "log", "cand"]) == 0
    assert "no commits for 'cand'" in capsys.readouterr().out
