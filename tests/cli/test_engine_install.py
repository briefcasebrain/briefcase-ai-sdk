"""`briefcase-ai engine install|verify|up|down|status|logs|doctor|uninstall|upgrade`, offline.

A throwaway P-256 key signs a fixture release exactly like `cosign sign-blob` (base64 DER over
SHA-256); the test pins it by pointing `trust.KEYS_DIR` at a temp directory. A fake platform
(tests/cli/engine_fakes.py) serves the install API and presigned-style downloads; a fake
`docker` (and `skopeo`, `xattr`, `cosign`) on PATH records every call."""
import json
import os
import stat

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main
from briefcase.engine import compose, install, trust

from tests.cli.engine_fakes import (
    OPENSSL, VERSION, FakePlatform, SigningKey, build_index, build_release, calls,
    docker_state, install_fake_tools, make_docker_archive,
)

TOOLS = ("docker", "skopeo", "xattr")


@pytest.fixture
def key(tmp_path_factory):
    return SigningKey(tmp_path_factory.mktemp("signing"))


@pytest.fixture
def pinned(key, tmp_path, monkeypatch):
    """Test-only trust root: the throwaway key is the one release key this 'SDK build' pins."""
    keys_dir = tmp_path / "keys"
    key.pin(keys_dir)
    monkeypatch.setattr(trust, "KEYS_DIR", keys_dir)
    return keys_dir


@pytest.fixture
def env(shell_home, tmp_path, monkeypatch, key, pinned):
    release = build_release(key)
    platform = FakePlatform(release, build_index(key, [VERSION, "0.3.0"]))
    monkeypatch.setattr(install, "host_target", lambda: {"os": "linux", "arch": "x86_64"})
    monkeypatch.setattr(install, "is_macos", lambda: False)
    log = tmp_path / "docker.log"
    # Without `cryptography` the installer falls back to `cosign verify-blob`; the fake cosign
    # verifies with openssl, so the whole suite also runs (and checks for real) on that path.
    install_fake_tools(tmp_path / "bin", log, monkeypatch, TOOLS + (() if trust.crypto_available() else ("cosign",)))
    monkeypatch.setenv("BRIEFCASE_PLATFORM_URL", platform.url)
    for name in ("OCI_JJ_SERVER", "OCI_JJ_AUTH_TOKEN", "OCI_JJ_TLS_CA", "OCI_JJ_REPO", "BRIEFCASE_LICENSE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield {"platform": platform, "release": release, "log": log, "home": shell_home / ".briefcase",
           "key": key, "tmp": tmp_path}
    platform.close()


def _install(*extra):
    return main(["engine", "install", "--yes", *extra])


def _docker(env):
    return [c["argv"] for c in calls(env["log"])]


# --------------------------------------------------------------------------- online


def test_install_with_code_downloads_verifies_loads_the_bundle_and_records(env, capsys):
    assert _install("--code", "bci_good") == 0
    out = capsys.readouterr().out
    assert f"engine {VERSION} installed" in out and "Next: briefcase-ai engine up" in out
    assert f"verified with key {env['key'].key_id}" in out

    post = env["platform"].posts()[0]
    assert post[1] == "/v1/engine/install" and post[2] == ""  # the code goes in the body, not as a bearer
    assert post[3] == {"os": "linux", "arch": "x86_64", "code": "bci_good"}
    downloads = env["platform"].downloads()
    assert downloads and all(r[2] == "" for r in downloads)  # presigned URLs get no auth header
    names = [r[1].split("?")[0].rsplit("/", 1)[-1] for r in downloads]
    bundle_name = f"engine-images-{VERSION}-x86_64.tar"
    assert names.index("engine-manifest.json") < names.index(f"oci-bai-{VERSION}-linux-x86_64.tar.gz")
    assert bundle_name in names and bundle_name + ".sig" in names

    record = _engine.current_install()
    release = env["release"]
    assert record["version"] == VERSION and record["source"] == "code"
    assert record["key_id"] == env["key"].key_id and record["client"] == {"mode": "binary"}
    binary = env["home"] / "engine" / VERSION / "bin" / "oci-bai"
    assert record["binary"] == str(binary) and os.access(binary, os.X_OK)
    assert record["binary_file"]["verified_by"] == "signature"

    argvs = _docker(env)
    downloaded = env["home"] / "engine" / VERSION / "download" / bundle_name
    assert ["load", "-i", str(downloaded)] in argvs
    assert not any(a[0] in ("login", "pull", "logout") for a in argvs)
    assert record["load_method"] == "docker load" and "registry" not in record
    for name, image_info in env["release"].images.items():
        assert record["image_ids"][name] == image_info["config"]
    assert record["images"]["server"] == f"localhost/briefcase-engine/server:{VERSION}"
    assert record["image_digests"]["server"] == release.manifest["images"]["server"]["digest"]

    text = (env["home"] / "engine" / VERSION / "docker-compose.yml").read_text()
    for name, image in release.manifest["images"].items():
        assert f"# signed manifest: {image['digest']}\n    image: localhost/briefcase-engine/{name}:{VERSION}\n" \
               f"    pull_policy: never\n" in text
    assert "oci_jj_secret" not in text and "OCI_JJ_AUTH_TOKENS: ${OCI_JJ_WORKER_TOKEN}:platform:admin" in text
    assert (env["home"] / "engine" / VERSION / "engine-manifest.json").read_bytes() == \
        release.files["engine-manifest.json"]

    secrets = env["home"] / "engine" / "engine.env"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    values = _engine.read_env_file(secrets)
    assert set(compose.SECRET_KEYS) <= set(values)
    settings = _engine.client_settings()
    assert settings["source"] == "local install" and settings["token"] == compose.tenant_token(values)


def test_install_code_is_single_use(env, capsys):
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    assert _install("--code", "bci_good") == 1
    err = capsys.readouterr().err
    assert "the install code was already used" in err
    assert f"Fix: Get a new install code at {env['platform'].url}/engine" in err


def test_failure_after_redeeming_a_code_says_get_a_new_code(env, capsys):
    env["platform"].overrides[f"oci-bai-{VERSION}-linux-x86_64.tar.gz"] = b"tampered binary"
    assert _install("--code", "bci_good") == 1
    err = capsys.readouterr().err
    assert "does not match the signed manifest" in err
    assert f"this install code is now used up; get a new one at {env['platform'].url}/engine" in err
    assert _engine.current_install() is None
    assert not list((env["home"] / "engine" / VERSION / "download").glob("oci-bai*"))  # bad file removed


def test_tampered_bundle_after_redeeming_is_refused_and_says_new_code(env, capsys):
    name = f"engine-images-{VERSION}-x86_64.tar"
    good = env["release"].files[name]
    env["platform"].overrides[name] = good[:-1] + bytes([good[-1] ^ 1])  # same length, one bit flipped
    assert _install("--code", "bci_good") == 1
    err = capsys.readouterr().err
    assert "does not match the signed manifest" in err and "used up; get a new one" in err
    assert not any(a[0] == "load" for a in _docker(env)) and _engine.current_install() is None


def test_docker_load_failure_after_redeeming_says_new_code(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_LOAD_FAIL", "1")
    (env["tmp"] / "bin" / "skopeo").unlink()
    monkeypatch.setenv("PATH", str(env["tmp"] / "bin"))  # a skopeo installed on the machine must not be found
    assert _install("--code", "bci_good") == 1
    assert "used up; get a new one" in capsys.readouterr().err


def test_a_bundle_without_its_detached_signature_is_refused(env, capsys):
    env["platform"].omit_bundle_sig = True
    assert _install("--token", "bcl_good") == 1
    assert "no signature for the image bundle" in capsys.readouterr().err
    assert not any(a[0] == "load" for a in _docker(env))


def test_check_file_rejects_a_same_length_change_without_a_signature(tmp_path, key, pinned):
    release = build_release(key)
    entry = release.manifest["bundles"][0]
    path = tmp_path / entry["file"]
    data = release.files[entry["file"]]
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    manifest = trust.load_manifest(release.files["engine-manifest.json"],
                                   release.files["engine-manifest.json.sig"], trust.load_keys(trust.KEYS_DIR))
    with pytest.raises(trust.TrustError, match="does not match"):
        trust.check_file(path, entry, manifest, None)


def test_answer_without_a_bundle_is_refused_unless_no_images(env, capsys):
    env["platform"].omit_bundle = True
    assert _install("--token", "bcl_good") == 1
    assert "sent no image bundle download for x86_64" in capsys.readouterr().err
    assert _install("--token", "bcl_good", "--no-images") == 0


def test_a_registry_in_the_answer_is_not_used(env, capsys):
    real = install.request_install

    def with_registry(*a, **kw):
        data = real(*a, **kw)
        data["registry"] = {"endpoint": "https://registry.invalid", "username": "u", "password": "p"}
        return data

    install.request_install = with_registry
    try:
        assert _install("--token", "bcl_good") == 0
    finally:
        install.request_install = real
    assert not any(a[0] in ("login", "pull", "logout") for a in _docker(env))


def test_token_install_is_idempotent(env, capsys, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_LICENSE_TOKEN", "bcl_good")
    assert _install() == 0
    first = (env["home"] / "engine" / "engine.env").read_text()
    post = env["platform"].posts()[0]
    assert post[2] == "Bearer bcl_good" and "code" not in post[3]
    binary_gets = len([r for r in env["platform"].downloads() if "oci-bai" in r[1] and ".sig" not in r[1]])
    bundle_gets = len([r for r in env["platform"].downloads() if "engine-images" in r[1] and ".sig" not in r[1]])
    assert bundle_gets == 1
    env["log"].unlink()
    assert _install() == 0
    assert "already installed" in capsys.readouterr().out
    assert len([r for r in env["platform"].downloads()
                if "oci-bai" in r[1] and ".sig" not in r[1]]) == binary_gets  # the verified file was reused
    assert not any(a[0] in ("login", "pull") for a in _docker(env))
    assert (env["home"] / "engine" / "engine.env").read_text() == first
    assert len([r for r in env["platform"].downloads()
                if "engine-images" in r[1] and ".sig" not in r[1]]) == bundle_gets  # the bundle was reused


def test_binary_without_signature_url_is_checked_against_the_signed_manifest(env, capsys):
    env["platform"].omit_binary_sig = True
    assert _install("--code", "bci_good") == 0
    assert "sha256 from the signed manifest" in capsys.readouterr().out
    assert _engine.current_install()["binary_file"]["verified_by"] == "sha256"


def test_wrong_binary_signature_is_refused(env, capsys):
    env["platform"].overrides[f"oci-bai-{VERSION}-linux-x86_64.tar.gz.sig"] = env["key"].sign(b"something else")
    assert _install("--code", "bci_good") == 1
    assert "its signature does not verify" in capsys.readouterr().err


@pytest.mark.parametrize("change, message", [
    ("tamper", "signature does not verify with pinned key"),
    ("other_key", "which this SDK does not trust"),
    ("other_signer", "signature does not verify with pinned key"),
    ("schema", "schema_version 3; this SDK understands only 2"),
    ("min_sdk", "needs briefcase-ai 99.0.0 or newer"),
])
def test_manifest_checks(env, capsys, tmp_path, change, message):
    key = env["key"]
    other = SigningKey(tmp_path / "other")
    if change == "tamper":
        raw = env["release"].files["engine-manifest.json"].replace(b"Test release", b"Evil release")
        env["platform"].overrides["engine-manifest.json"] = raw
    elif change == "other_key":
        env["platform"].release = build_release(other)
    elif change == "other_signer":  # names the pinned key, signed by another
        env["platform"].release = build_release(key, manifest_signer=other)
    elif change == "schema":
        env["platform"].release = build_release(key, schema_version=3)
    elif change == "min_sdk":
        env["platform"].release = build_release(key, min_sdk="99.0.0")
    assert _install("--code", "bci_good") == 1
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err
    assert "Fix:" in err and _engine.current_install() is None
    assert not any(a[0] in ("pull", "load") for a in _docker(env))


def test_no_bundled_key_refuses_before_redeeming(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(trust, "KEYS_DIR", tmp_path / "empty-keys")
    assert _install("--code", "bci_good") == 1
    err = capsys.readouterr().err
    assert ("no trusted release key is bundled in this SDK build; the engine cannot be installed until "
            "a release key is published") in err
    assert env["platform"].posts() == []  # the code was not spent


def test_key_loader_accepts_any_release_pub_and_ignores_others(key, tmp_path):
    keys_dir = tmp_path / "k"
    keys_dir.mkdir()
    (keys_dir / "release-anything.pub").write_bytes(key.public_pem)
    (keys_dir / "release-broken.pub").write_text("not a key")
    (keys_dir / "notes.pub").write_bytes(key.public_pem)
    (keys_dir / "README.md").write_text("x")
    loaded = trust.load_keys(keys_dir)
    assert [k.key_id for k in loaded] == [key.key_id] and loaded[0].label == "release-anything.pub"


def test_shipped_key_directory_has_readme_and_only_the_pinned_release_keys():
    assert (trust.KEYS_DIR / "README.md").is_file()
    keys = trust.load_keys(trust.KEYS_DIR)
    assert sorted(k.key_id for k in keys) == [
        "sha256:2c2303bdef3d97b33ca998f9eb397a8bf3de61465a0cb4fe955767e8b5470111",   # active release key
        "sha256:54a3f59fc571363966ea0e0700388201ce51a5d50e4d2f3de7843e8cd61fee43"]   # offline recovery key


@pytest.mark.parametrize("argv, message, fix", [
    (["--code", "bci_expired"], "the install code has expired", "Get a new install code at {url}/engine"),
    (["--code", "bci_noent"], "your account isn't entitled to the engine", "Request access at {url}/engine"),
    (["--token", "bcl_noent"], "your account isn't entitled to the engine", "Request access at {url}/engine"),
    (["--token", "bcl_expired"], "the licence token has expired", "create a new engine licence token at {url}/engine"),
    (["--token", "bcl_bogus"], "did not accept the licence token", "or create one at {url}/engine"),
    (["--token", "bcl_slow"], "retry in 42 seconds", None),
    (["--token", "bcl_noconfig"], "cannot serve engine downloads right now", "operator"),
])
def test_platform_refusals_name_the_next_step(env, capsys, argv, message, fix):
    assert _install(*argv) == 1
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err
    if fix:
        assert fix.format(url=env["platform"].url) in err


@pytest.mark.parametrize("argv, message", [
    ([], "no install code, licence token or offline bundle given"),
    (["--code", "bci_good", "--version", "0.3.0"], "an install code is bound to the version"),
    (["--code", "bcl_x"], "install codes start with bci_"),
    (["--token", "bci_good"], "that is an install code, not a licence token"),
    (["--code", "bci_good", "--token", "bcl_good"], "either --code or --token"),
    (["--bundle", "x.tar"], "needs both --bundle and --manifest"),
])
def test_usage_errors(env, capsys, argv, message):
    assert _install(*argv) == 2
    err = capsys.readouterr().err
    assert message in err and env["platform"].posts() == []
    if not argv:
        assert f"{env['platform'].url}/engine" in err


def test_default_platform_is_the_portal(shell_home, capsys, pinned, monkeypatch):
    monkeypatch.delenv("BRIEFCASE_PLATFORM_URL", raising=False)
    assert main(["engine", "install"]) == 2
    assert "https://observe.briefcasebrain.io/engine" in capsys.readouterr().err


def test_dry_run_with_a_code_does_not_redeem_it(env, capsys):
    assert main(["engine", "install", "--code", "bci_good", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "the install code was not redeemed" in out and "Next: briefcase-ai engine install --code bci_good --yes" in out  # noqa: E501
    assert env["platform"].posts() == [] and not (env["home"] / "engine").exists()


def test_dry_run_with_a_token_shows_the_signed_plan(env, capsys):
    assert main(["engine", "install", "--token", "bcl_good", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["version"] == VERSION and plan["key_id"] == env["key"].key_id
    assert any(step.startswith("download the image bundle for x86_64") for step in plan["steps"])
    assert any(step.startswith("docker load the bundle") for step in plan["steps"])
    assert not any("ECR" in step or "docker login" in step or "docker pull" in step for step in plan["steps"])
    assert not any(a[0] in ("login", "pull") for a in _docker(env))
    assert not (env["home"] / "engine" / VERSION / "docker-compose.yml").exists()


def test_needs_confirmation_without_a_terminal_and_keeps_the_code(env, capsys):
    assert main(["engine", "install", "--code", "bci_good"]) == 2
    assert "re-run with --yes" in capsys.readouterr().err
    assert env["platform"].posts() == [] and _engine.current_install() is None


def test_no_docker_fails_before_redeeming(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_DOWN", "1")
    assert _install("--code", "bci_good") == 1
    assert "Docker is not installed or its daemon is not running" in capsys.readouterr().err
    assert env["platform"].posts() == []
    assert _install("--code", "bci_good", "--no-images") == 0  # the client binary alone still installs
    assert _engine.current_install()["compose"] is None


def test_expired_download_link(env, capsys):
    env["platform"].expired_paths.add("engine-manifest.json")
    assert _install("--token", "bcl_good") == 1
    assert "expire after 5 minutes" in capsys.readouterr().err


# --------------------------------------------------------------------------- no binary for this OS / macOS


def test_no_binary_for_this_os_runs_oci_bai_from_the_server_image(env, capsys, monkeypatch, tmp_path, key):
    env["platform"].release = build_release(key, targets=(("linux", "x86_64"),), bundle_arch="aarch64")
    monkeypatch.setattr(install, "host_target", lambda: {"os": "darwin", "arch": "aarch64"})
    assert _install("--code", "bci_good") == 0
    out = capsys.readouterr().out
    server = f"localhost/briefcase-engine/server:{VERSION}"
    assert f"no oci-bai build for darwin/aarch64 in engine {VERSION}" in out and f"run it from {server}" in out
    assert out.count("no oci-bai build") == 1
    record = _engine.current_install()
    assert record["binary"] is None and record["client"]["mode"] == "container"
    assert record["client"]["image"] == server and record["client"]["image_id"] == record["image_ids"]["server"]
    assert not [r for r in env["platform"].downloads() if "oci-bai" in r[1]]

    cohort = tmp_path / "project" / "cohort"
    cohort.mkdir()
    (cohort / "cohort.json").write_text("{}")
    assert main(["env", "cohort", str(cohort), "--dry-run"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("docker run --rm -i --pull never --network briefcase-engine_default")
    image_id = record["image_ids"]["server"]
    assert f"-v {cohort}:{cohort}:ro" in line and f"--entrypoint /usr/local/bin/oci-bai {image_id}" in line
    assert " --pull never " in line and server not in line  # the verified ID, never the mutable tag
    assert line.endswith(f"--server http://oci-jj-server:50051 --repo rl-gym-env cohort push {cohort}")
    token = compose.tenant_token(_engine.read_env_file(env["home"] / "engine" / "engine.env"))
    assert token not in line and "-e OCI_JJ_AUTH_TOKEN " in line  # by name only

    env["log"].unlink()
    assert main(["env", "cohort", str(cohort)]) == 0
    run = calls(env["log"])[-1]
    assert run["argv"][:2] == ["run", "--rm"] and run["token"] == token  # the value came via the environment
    assert main(["engine", "status"]) == 0
    assert f"oci-bai:       runs from {server}" in capsys.readouterr().out


def test_no_binary_and_no_images_is_refused(env, capsys, monkeypatch):
    monkeypatch.setattr(install, "host_target", lambda: {"os": "darwin", "arch": "aarch64"})
    assert _install("--token", "bcl_good", "--no-images") == 1
    assert "leaves no server image to run it from" in capsys.readouterr().err


def test_quarantine_is_removed_only_after_verification(env, capsys, monkeypatch, key):
    env["platform"].release = build_release(key, targets=(("linux", "x86_64"), ("darwin", "aarch64")),
                                            bundle_arch="aarch64")
    monkeypatch.setattr(install, "host_target", lambda: {"os": "darwin", "arch": "aarch64"})
    monkeypatch.setattr(install, "is_macos", lambda: True)
    monkeypatch.setenv("FAKE_QUARANTINE", "1")
    env["platform"].overrides[f"oci-bai-{VERSION}-darwin-aarch64.tar.gz"] = b"tampered"
    assert _install("--token", "bcl_good") == 1
    assert calls(env["log"], "xattr") == []  # never touched an unverified file
    env["platform"].overrides.clear()
    capsys.readouterr()
    assert _install("--token", "bcl_good") == 0
    binary = str(env["home"] / "engine" / VERSION / "bin" / "oci-bai")
    assert [c["argv"] for c in calls(env["log"], "xattr")] == [
        ["-p", "com.apple.quarantine", binary], ["-d", "com.apple.quarantine", binary]]
    assert "removed the macOS quarantine attribute" in capsys.readouterr().out
    assert _engine.current_install()["client"] == {"mode": "binary"}


# --------------------------------------------------------------------------- offline


@pytest.fixture
def offline(env, monkeypatch):
    """The portal downloads in a directory, and no reachable platform."""
    directory = env["release"].write(env["tmp"] / "downloads")
    monkeypatch.setenv("BRIEFCASE_PLATFORM_URL", "http://127.0.0.1:9")
    env["platform"].requests.clear()
    return directory


def _offline_args(directory, *extra):
    return ["--bundle", str(directory / f"engine-images-{VERSION}-x86_64.tar"),
            "--manifest", str(directory / "engine-manifest.json"), *extra]


def test_offline_install_loads_the_bundle_and_checks_every_image(env, offline, capsys):
    assert _install(*_offline_args(offline)) == 0
    out = capsys.readouterr().out
    assert "bundle signature verified" in out and "loaded 5 images with docker load" in out
    assert env["platform"].requests == []  # no network
    argvs = _docker(env)
    assert [a for a in argvs if a[0] == "load"][0][-1].endswith(f"/engine-images-{VERSION}-x86_64.tar")
    assert not any(a[0] in ("login", "pull", "logout") for a in argvs)
    record = _engine.current_install()
    info = env["release"].images
    assert record["source"] == "bundle" and record["load_method"] == "docker load"
    assert record["image_ids"] == {n: i["config"] for n, i in info.items()}  # IDs = config digests
    tags = {a[2]: a[1] for a in argvs if a[0] == "tag"}
    assert tags[f"localhost/briefcase-engine/postgres:{VERSION}"] == info["postgres"]["config"]
    assert record["binary"] and record["binary_file"]["verified_by"] == "signature"
    state = docker_state(env["log"])
    assert state["refs"][f"localhost/briefcase-engine/server:{VERSION}"] == info["server"]["config"]
    # The bundle's Docker-Hub-looking names are untagged (never forced) once the local tag exists.
    untagged = [a[-1] for a in argvs if a[:3] == ["image", "rm", "--no-prune"]]
    assert sorted(untagged) == sorted(f"briefcase-engine/{n}:{VERSION}" for n in info)


def test_offline_install_on_macos_runs_oci_bai_from_the_server_image(env, key, capsys, tmp_path, monkeypatch):
    """The real macOS path: releases carry linux binaries only (oci-jj 6d1db6a)."""
    monkeypatch.setattr(install, "host_target", lambda: {"os": "darwin", "arch": "aarch64"})
    directory = build_release(key, bundle_arch="aarch64").write(tmp_path / "mac")
    assert _install("--bundle", str(directory / f"engine-images-{VERSION}-aarch64.tar"),
                    "--manifest", str(directory / "engine-manifest.json")) == 0
    out = capsys.readouterr().out
    assert f"no oci-bai build for darwin/aarch64 in engine {VERSION}" in out
    assert "pass --binary" not in out  # nothing to pass: there is no darwin build
    record = _engine.current_install()
    assert record["client"]["mode"] == "container" and record["binary"] is None


def test_offline_install_with_the_containerd_image_store(env, offline, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_CONTAINERD", "1")  # image ID = the manifest/index digest
    assert _install(*_offline_args(offline)) == 0
    info = env["release"].images
    assert _engine.current_install()["image_ids"]["postgres"] == info["postgres"]["digest"]


def _record_loads(monkeypatch):
    """Wrap the image loader to note, at load time, where the archive is and who can reach its directory."""
    seen = []
    real = install._load_images

    def spy(manifest, bundle_path, *args, **kwargs):
        directory = os.path.dirname(str(bundle_path))
        seen.append({"path": str(bundle_path), "dir_mode": stat.S_IMODE(os.stat(directory).st_mode),
                     "sha": trust.sha256_file(install.Path(bundle_path))})
        return real(manifest, bundle_path, *args, **kwargs)

    monkeypatch.setattr(install, "_load_images", spy)
    return seen


def test_offline_install_loads_a_private_verified_copy_and_removes_it_afterwards(env, offline, monkeypatch):
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    seen = _record_loads(monkeypatch)
    assert _install(*_offline_args(offline)) == 0
    assert len(seen) == 1 and seen[0]["path"] != str(bundle)
    assert seen[0]["path"].startswith(str(env["home"] / "engine" / VERSION / "download"))
    assert seen[0]["dir_mode"] == 0o700
    assert seen[0]["sha"] == env["release"].manifest["bundles"][0]["sha256"]
    assert not os.path.exists(seen[0]["path"]) and not os.path.exists(os.path.dirname(seen[0]["path"]))


def test_each_offline_install_stages_into_its_own_directory(env, offline, monkeypatch):
    seen = _record_loads(monkeypatch)
    assert _install(*_offline_args(offline)) == 0
    assert _install(*_offline_args(offline)) == 0
    assert len({os.path.dirname(item["path"]) for item in seen}) == 2


def test_a_failed_offline_install_leaves_no_staged_archive(env, offline, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_LOAD_FAIL", "1")
    (env["tmp"] / "bin" / "skopeo").unlink()
    monkeypatch.setenv("PATH", str(env["tmp"] / "bin"))
    assert _install(*_offline_args(offline)) == 1
    download = env["home"] / "engine" / VERSION / "download"
    assert not download.exists() or list(download.rglob("*")) == []


def test_offline_install_checks_free_space_before_copying(env, offline, monkeypatch, capsys):
    monkeypatch.setattr(install.shutil, "disk_usage", lambda path: type("U", (), {"free": 1024})())
    assert _install(*_offline_args(offline)) == 1
    assert "not enough free disk space" in capsys.readouterr().err
    download = env["home"] / "engine" / VERSION / "download"
    assert not download.exists() or list(download.rglob("*")) == []


def test_a_copy_that_fails_midway_leaves_no_partial_file(env, offline, monkeypatch, capsys):
    def full_disk(src, dst, length=0):
        dst.write(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(install.shutil, "copyfileobj", full_disk)
    assert _install(*_offline_args(offline)) == 1
    download = env["home"] / "engine" / VERSION / "download"
    assert not download.exists() or list(download.rglob("*")) == []


def test_the_client_archive_next_to_the_bundle_is_found_when_the_manifest_is_elsewhere(env, offline, tmp_path):
    manifest_dir = tmp_path / "manifest-only"
    manifest_dir.mkdir()
    for name in ("engine-manifest.json", "engine-manifest.json.sig"):
        (manifest_dir / name).write_bytes((offline / name).read_bytes())
        (offline / name).unlink()
    binary = f"oci-bai-{VERSION}-linux-x86_64.tar.gz"
    assert (offline / binary).exists() and not (manifest_dir / binary).exists()
    args = ["--bundle", str(offline / f"engine-images-{VERSION}-x86_64.tar"),
            "--manifest", str(manifest_dir / "engine-manifest.json")]
    assert _install(*args) == 0
    assert _engine.current_install()["binary"]


def test_two_concurrent_offline_installs_of_one_release_both_end_verified(env, offline, monkeypatch):
    import multiprocessing

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("needs fork")
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()

    def worker():
        code = _install(*_offline_args(offline))
        results.put((code, _engine.current_install() is not None))

    procs = [ctx.Process(target=worker) for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(120)
        assert proc.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in procs]
    assert outcomes == [(0, True), (0, True)]
    loads = [a[-1] for a in _docker(env) if a[0] == "load"]
    assert len(loads) == 2 and len({os.path.dirname(p) for p in loads}) == 2
    download = env["home"] / "engine" / VERSION / "download"
    assert not download.exists() or list(download.rglob("*")) == []


def test_offline_bundle_changed_after_the_first_check_is_refused_before_docker_sees_it(
        env, offline, capsys, monkeypatch):
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    real = install.offline_install

    def tamper_then_install(*args, **kwargs):
        data = bundle.read_bytes()
        bundle.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))  # same length, one bit flipped
        return real(*args, **kwargs)

    monkeypatch.setattr(install, "offline_install", tamper_then_install)
    assert _install(*_offline_args(offline)) == 1
    assert "does not match the signed manifest" in capsys.readouterr().err
    assert not any(a[0] == "load" for a in _docker(env)) and _engine.current_install() is None


def test_offline_install_falls_back_to_skopeo(env, offline, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_LOAD_FAIL", "1")
    assert _install(*_offline_args(offline)) == 0
    assert "trying skopeo" in capsys.readouterr().out
    copies = [c["argv"] for c in calls(env["log"], "skopeo")]
    staged = [c["argv"][2] for c in calls(env["log"], "skopeo") if c["argv"][0] == "copy"][0]
    assert staged.startswith("oci-archive:" + str(env["home"] / "engine" / VERSION / "download"))
    bundle = staged[len("oci-archive:"):].rsplit(":briefcase-engine/", 1)[0]
    assert ["copy", "--quiet", f"oci-archive:{bundle}:briefcase-engine/server:{VERSION}",
            f"docker-daemon:localhost/briefcase-engine/server:{VERSION}"] in copies
    assert _engine.current_install()["load_method"] == "skopeo"


def test_offline_docker_load_failure_without_skopeo(env, offline, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_LOAD_FAIL", "1")
    (env["tmp"] / "bin" / "skopeo").unlink()
    monkeypatch.setenv("PATH", str(env["tmp"] / "bin"))  # a skopeo installed on the machine must not be found
    assert _install(*_offline_args(offline)) == 1
    err = capsys.readouterr().err
    assert "docker load" in err and "Docker to 25 or newer" in err


def test_offline_docker_save_archive(env, key, capsys, tmp_path, monkeypatch):
    data, ids = make_docker_archive()
    release = build_release(key)
    release.files[f"engine-images-{VERSION}-x86_64.tar"] = data
    release.files[f"engine-images-{VERSION}-x86_64.tar.sig"] = key.sign(data)
    release.manifest["bundles"][0].update(sha256=__import__("hashlib").sha256(data).hexdigest(), size=len(data))
    raw = json.dumps(release.manifest).encode()
    release.files["engine-manifest.json"], release.files["engine-manifest.json.sig"] = raw, key.sign(raw)
    directory = release.write(tmp_path / "legacy")
    assert _install(*_offline_args(directory)) == 0
    assert "without registry digests" in capsys.readouterr().out
    assert _engine.current_install()["image_ids"] == ids


@pytest.mark.parametrize("what, message", [
    ("bundle", "does not match the signed manifest"),
    ("bundle_sig", "its signature does not verify"),
    ("manifest", "signature does not verify with pinned key"),
])
def test_offline_tampering_is_refused_before_loading(env, offline, capsys, what, message):
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    if what == "bundle":
        bundle.write_bytes(bundle.read_bytes() + b"\0" * 512)
    elif what == "bundle_sig":
        (offline / (bundle.name + ".sig")).write_bytes(env["key"].sign(b"other"))
    else:
        (offline / "engine-manifest.json").write_bytes(
            (offline / "engine-manifest.json").read_bytes().replace(b"0.4.0", b"0.4.1"))
    assert _install(*_offline_args(offline)) == 1
    assert message in capsys.readouterr().err
    assert not any(a[0] == "load" for a in _docker(env)) and _engine.current_install() is None


def test_offline_wrong_architecture(env, key, capsys, tmp_path):
    directory = build_release(key, bundle_arch="aarch64").write(tmp_path / "arm")
    assert _install("--bundle", str(directory / f"engine-images-{VERSION}-aarch64.tar"),
                    "--manifest", str(directory / "engine-manifest.json")) == 1
    assert "is for aarch64; this machine is x86_64" in capsys.readouterr().err


def test_offline_without_the_binary_uses_the_server_image(env, offline, capsys):
    (offline / f"oci-bai-{VERSION}-linux-x86_64.tar.gz").unlink()
    assert _install(*_offline_args(offline)) == 0
    out = capsys.readouterr().out
    assert "pass --binary FILE" in out and "no oci-bai build" in out
    assert _engine.current_install()["client"]["mode"] == "container"


# --------------------------------------------------------------------------- verify


def test_engine_verify(env, offline, capsys):
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    manifest = offline / "engine-manifest.json"
    assert main(["engine", "verify", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert f"verified: engine-manifest.json (engine {VERSION})" in out and env["key"].key_id in out
    assert main(["engine", "verify", str(bundle), "--manifest", str(manifest), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["checked"] == ["size", "sha256", "signature"]
    assert result["key_id"] == env["key"].key_id and result["key_file"].startswith("release-")
    index = build_index(env["key"], [VERSION])
    (offline / "index.json").write_bytes(index["index.json"])
    (offline / "index.json.sig").write_bytes(index["index.json.sig"])
    assert main(["engine", "verify", str(offline / "index.json")]) == 0
    assert "checked: signature" in capsys.readouterr().out
    binary = offline / f"oci-bai-{VERSION}-linux-x86_64.tar.gz"
    assert main(["engine", "verify", str(binary)]) == 0  # its .sig alone
    assert "checked: signature" in capsys.readouterr().out

    binary.write_bytes(binary.read_bytes() + b"x")
    assert main(["engine", "verify", str(binary)]) == 1
    assert "does not verify with any key pinned in this SDK" in capsys.readouterr().err
    assert main(["engine", "verify", str(binary), "--manifest", str(manifest)]) == 1
    assert "does not match the signed manifest" in capsys.readouterr().err
    (offline / "lonely.bin").write_bytes(b"x")
    assert main(["engine", "verify", str(offline / "lonely.bin")]) == 2
    assert "--signature" in capsys.readouterr().err
    assert env["platform"].requests == []


def test_engine_verify_with_cosign_when_cryptography_is_missing(env, offline, capsys, monkeypatch):
    if OPENSSL is None:
        pytest.skip("the fake cosign verifies with openssl")
    install_fake_tools(env["tmp"] / "bin", env["log"], monkeypatch, ("cosign",))
    monkeypatch.setattr(trust, "crypto_available", lambda: False)
    bundle = offline / f"engine-images-{VERSION}-x86_64.tar"
    assert main(["engine", "verify", str(bundle)]) == 0
    argv = calls(env["log"], "cosign")[0]["argv"]
    assert argv[0] == "verify-blob" and "--key" in argv and "--insecure-ignore-tlog=true" in argv
    (offline / (bundle.name + ".sig")).write_bytes(env["key"].sign(b"other"))
    assert main(["engine", "verify", str(bundle)]) == 1


def test_no_cryptography_and_no_cosign_refuses_before_redeeming(env, capsys, monkeypatch):
    monkeypatch.setattr(trust, "crypto_available", lambda: False)
    (env["tmp"] / "bin" / "cosign").unlink(missing_ok=True)
    assert _install("--code", "bci_good") == 1
    err = capsys.readouterr().err
    assert "neither the `cryptography` package nor `cosign`" in err and "briefcase-ai[engine]" in err
    assert env["platform"].posts() == []


def test_signature_format_is_cosign_blob(key):
    data = b"payload"
    sig = key.sign(data)
    k = trust.ReleaseKey(key.key_id, trust.Path("release-x.pub"), key.public_pem, trust.pem_to_der(key.public_pem))
    if not trust.crypto_available():
        pytest.skip("pure-python verification needs cryptography")
    assert trust.verify_blob(data, sig, k) and trust.verify_blob(data, sig + b"\n", k)
    assert not trust.verify_blob(b"other", sig, k)
    assert trust.key_id_of(key.public_pem) == key.key_id


# --------------------------------------------------------------------------- status / doctor / lifecycle


def test_status_shows_key_and_newer_version(env, capsys, monkeypatch, key):
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    assert main(["engine", "status"]) == 0
    out = capsys.readouterr().out
    assert f"verified with: {key.key_id}" in out and "newest:        not checked (no licence token" in out
    env["platform"].index = build_index(key, ["0.5.0", VERSION])
    monkeypatch.setenv("BRIEFCASE_LICENSE_TOKEN", "bcl_good")
    assert main(["engine", "status", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["key_id"] == key.key_id and state["update"] == {"checked": True, "latest": "0.5.0",
                                                                 "update_available": True}
    assert state["services"] == [{"service": "oci-jj-server", "state": "running", "health": "healthy"}]
    assert main(["engine", "status"]) == 0
    assert "Next: briefcase-ai engine upgrade" in capsys.readouterr().out
    env["platform"].index["index.json"] = env["platform"].index["index.json"].replace(b"0.5.0", b"9.9.9")
    assert main(["engine", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["update"]["checked"] is False  # bad signature: not trusted


def test_doctor_and_up_detect_a_replaced_image(env, capsys, monkeypatch):
    from briefcase.cli import engine_commands

    monkeypatch.setattr(engine_commands, "port_open", lambda addr, timeout=0.3: False)
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    assert main(["engine", "doctor", "--json"]) == 0
    checks = {row["check"]: row for row in json.loads(capsys.readouterr().out)}
    for name in ("docker", "release keys", "signature", "client binary", "image server", "secrets"):
        assert checks[name]["severity"] == "ok", checks[name]
    assert checks["gRPC"]["severity"] == "warn"
    # Someone retags the server image.
    state_file = env["log"].with_suffix(".state.json")
    state = json.loads(state_file.read_text())
    state["refs"][f"localhost/briefcase-engine/server:{VERSION}"] = "sha256:" + "e" * 64
    state_file.write_text(json.dumps(state))
    assert main(["engine", "doctor", "--json"]) == 1
    checks = {row["check"]: row for row in json.loads(capsys.readouterr().out)}
    assert checks["image server"]["severity"] == "fail"
    env["log"].unlink()
    state_file.write_text(json.dumps(state))
    assert main(["engine", "up"]) == 1
    err = capsys.readouterr().err
    assert "changed since it was verified" in err and "/engine" in err
    assert not any(a[0] == "compose" for a in _docker(env))


def test_up_status_logs_down_drive_the_installed_compose(env, capsys):
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    env["log"].unlink()
    assert main(["engine", "up"]) == 0
    assert main(["engine", "logs", "oci-jj-server", "--tail", "5"]) == 0
    assert main(["engine", "down"]) == 0
    compose_calls = [a for a in _docker(env) if a[0] == "compose"]
    for argv in compose_calls:
        assert argv[1:3] == ["--project-name", "briefcase-engine"] and "--env-file" in argv
    assert [a[a.index("--env-file") + 2] for a in compose_calls] == ["up", "logs", "down"]


def test_engine_up_refuses_while_a_4_4_stack_is_still_running(env, capsys, monkeypatch):
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    monkeypatch.setenv("FAKE_DOCKER_LEGACY", "1")
    env["log"].unlink()
    assert main(["engine", "up"]) == 1
    err = capsys.readouterr().err
    assert "4.4 stack" in err and "docker compose -p oci-jj down" in err
    assert "oci-jj_pgdata" in err and "not migrated" in err
    assert not any(a[0] == "compose" and "up" in a for a in _docker(env))


def test_engine_doctor_warns_about_a_running_4_4_stack_and_its_leftover_volumes(env, capsys, monkeypatch):
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    monkeypatch.setenv("FAKE_DOCKER_LEGACY", "1")
    main(["engine", "doctor", "--json"])
    rows = {row["check"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["4.4 stack"]["severity"] == "warn" and "docker compose -p oci-jj down" in rows["4.4 stack"]["detail"]
    monkeypatch.delenv("FAKE_DOCKER_LEGACY")
    monkeypatch.setenv("FAKE_DOCKER_LEGACY_VOLUMES", "1")
    main(["engine", "doctor", "--json"])
    rows = {row["check"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["4.4 stack"]["severity"] == "warn" and "oci-jj_pgdata" in rows["4.4 stack"]["detail"]


def test_run_logs_says_the_compose_override_is_no_longer_read(env, capsys, monkeypatch):
    from types import SimpleNamespace

    from briefcase.cli import commands

    monkeypatch.setenv("BRIEFCASE_OCIJJ_COMPOSE", "/somewhere/docker-compose.yml")
    store = SimpleNamespace(get_run=lambda run_id: {"id": run_id})
    commands.cmd_run_logs(SimpleNamespace(id="demo", follow=False), store, None)
    assert "BRIEFCASE_OCIJJ_COMPOSE is no longer read" in capsys.readouterr().err


def test_uninstall_removes_install_and_purge_removes_secrets(env, capsys):
    assert _install("--code", "bci_good") == 0
    assert main(["engine", "uninstall", "--dry-run"]) == 0
    assert _engine.current_install() is not None
    assert main(["engine", "uninstall", "--purge", "--yes"]) == 0
    assert _engine.current_install() is None
    assert not (env["home"] / "engine" / VERSION).exists()
    assert not (env["home"] / "engine" / "engine.env").exists()
    removed = {a[2] for a in _docker(env) if a[:2] == ["image", "rm"]}
    for name in env["release"].manifest["images"]:
        assert f"localhost/briefcase-engine/{name}:{VERSION}" in removed
    assert not any("@sha256:" in ref for ref in removed)


def test_upgrade_needs_a_code_or_token_and_restarts_a_running_engine(env, capsys, monkeypatch, key):
    assert _install("--code", "bci_good") == 0
    capsys.readouterr()
    assert main(["engine", "upgrade", "--yes"]) == 2
    assert "no install code, licence token or offline bundle" in capsys.readouterr().err
    env["platform"].release = build_release(key, version="0.5.0")
    monkeypatch.setenv("FAKE_DOCKER_RUNNING", "abc123")
    env["log"].unlink()
    assert main(["engine", "upgrade", "--yes", "--token", "bcl_good"]) == 0
    assert _engine.current_install()["version"] == "0.5.0"
    ups = [a for a in _docker(env) if a[0] == "compose" and "up" in a]
    assert ups and "0.5.0" in ups[-1][ups[-1].index("-f") + 1]


def test_run_logs_reads_the_verdict_worker_logs_when_the_release_has_one(env, capsys, key):
    from types import SimpleNamespace

    from briefcase.cli import commands
    from tests.cli.engine_fakes import IMAGE_NAMES

    env["platform"].release = build_release(key, image_names=IMAGE_NAMES + ("verdict_worker",))
    assert _install("--code", "bci_good") == 0
    env["log"].unlink()
    store = SimpleNamespace(get_run=lambda run_id: {"id": run_id})
    assert commands.cmd_run_logs(SimpleNamespace(id="demo", follow=True), store, None) == 0
    argv = _docker(env)[-1]
    assert argv[:3] == ["compose", "--project-name", "briefcase-engine"]
    assert argv[argv.index("logs"):] == ["logs", "--tail", "200", "-f", "verdict-worker"]


def test_run_logs_says_so_when_the_release_has_no_verdict_worker(env, capsys):
    from types import SimpleNamespace

    from briefcase.cli import commands

    assert _install("--code", "bci_good") == 0
    env["log"].unlink()
    capsys.readouterr()
    store = SimpleNamespace(get_run=lambda run_id: {"id": run_id})
    assert commands.cmd_run_logs(SimpleNamespace(id="demo", follow=False), store, None) == 1
    err = capsys.readouterr().err
    assert "no verdict worker" in err and "briefcase-ai engine logs" in err
    assert not any(a[0] == "compose" and "logs" in a for a in _docker(env))


def test_the_fake_compose_fails_for_an_unknown_service(env):
    assert _install("--code", "bci_good") == 0
    assert main(["engine", "logs", "verdict-worker"]) == 1  # not in this release's compose file


def test_run_logs_without_an_engine_says_to_install_it(shell_home, capsys, monkeypatch):
    from types import SimpleNamespace

    from briefcase.cli import commands

    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    store = SimpleNamespace(get_run=lambda run_id: {"id": run_id})
    assert commands.cmd_run_logs(SimpleNamespace(id="demo", follow=False), store, None) == 3
    assert "briefcase-ai engine install" in capsys.readouterr().err


# --------------------------------------------------------------------------- compose


def _service(text, name):
    return text.split(f"\n  {name}:\n", 1)[1].split("\n\n", 1)[0]


DIGESTS = {n: f"ghcr.example/oci-jj-{n}@sha256:" + str(i) * 64
           for i, n in enumerate(("server", "gateway", "workers", "postgres", "reconstructor"), 1)}


def test_unpinned_image_is_refused():
    with pytest.raises(ValueError):
        compose.render({"server": "x:1", "gateway": "x", "workers": "x", "postgres": "x"}, "0.1.0")
    with pytest.raises(ValueError):  # a bare tag that could resolve to Docker Hub
        compose.render(dict(DIGESTS, server="briefcase-engine/server:0.4.0"), "0.4.0")


def test_compose_matches_the_engine_security_model():
    """oci-jj e89ddbf/8a52297/be043d9: only the workers hold a (worker) token; the gateway and
    reconstructor forward callers' tokens; the verdict worker has no object-store credentials;
    datasets live in the tenant-prefixed bucket; the app role password comes from the env."""
    images = dict(DIGESTS, verdict_worker="ghcr.example/oci-jj-verdict-worker@sha256:" + "6" * 64)
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


def test_compose_takes_minio_and_registry_from_the_manifest():
    names = ("server", "gateway", "workers", "postgres", "minio", "registry")
    local = {n: f"localhost/briefcase-engine/{n}:0.4.0" for n in names}
    text = compose.render(local, "0.4.0")
    assert "image: localhost/briefcase-engine/minio:0.4.0\n    pull_policy: never" in _service(text, "minio")
    assert "image: localhost/briefcase-engine/registry:0.4.0" in _service(text, "registry")


def test_compose_fallback_third_party_images_are_pinned_by_digest():
    text = compose.render(dict(DIGESTS), "0.2.0")
    for service in ("minio", "registry"):
        ref = [line for line in _service(text, service).splitlines() if line.strip().startswith("image:")][0]
        assert "@sha256:" in ref, ref


def test_tls_compose_variant():
    text = compose.render(dict(DIGESTS), "0.2.0", tls=True)
    assert "OCI_JJ_TLS_CERT: /tls/server.crt" in text and "OCI_JJ_ALLOW_PLAINTEXT" not in text
    assert "OCI_JJ_SERVER_GRPC: https://oci-jj-server:50051" in text and "OCI_JJ_TLS_CA: /tls/server.crt" in text


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
    assert "engine not installed — `briefcase-ai engine install`" in text and "Traceback" not in text


def test_engine_status_json_without_an_install_exits_3(shell_home, capsys, monkeypatch):
    monkeypatch.delenv("OCI_JJ_SERVER", raising=False)
    assert main(["engine", "status", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["installed"] is False


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
        assert f"{help_text} (engine not installed — `briefcase-ai engine install`)" in flat
    assert main(["status", "--json"]) == 0
    items = {i["id"]: i for i in json.loads(capsys.readouterr().out)["items"]}
    for ident in ("engine", "graph", "env-artifacts"):
        assert items[ident]["state"] == "off"
        assert items[ident]["detail"] == _engine.NOT_INSTALLED and items[ident]["next"] is None
    assert main(["features", "--json"]) == 0
    ids = {f["id"] for g in json.loads(capsys.readouterr().out)["groups"] for f in g["features"]}
    assert {"engine", "graph", "env-artifacts"} <= ids


def test_install_help_has_one_example(shell_home, capsys):
    with pytest.raises(SystemExit):
        main(["engine", "install", "--help"])
    out = capsys.readouterr().out
    assert "briefcase-ai engine install --code bci_..." in out and "https://observe.briefcasebrain.io/engine" in out
    assert "--insecure" not in out  # there is no way to skip verification


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


def test_offline_bundle_of_another_release_is_refused(env, offline, capsys):
    other = offline / "renamed.tar"
    other.write_bytes(b"\0" * 1024)
    assert _install("--bundle", str(other), "--manifest", str(offline / "engine-manifest.json")) == 1
    assert "is not a bundle of engine" in capsys.readouterr().err
