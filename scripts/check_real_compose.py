"""Exercise SDK startup against installed Docker Compose. Run from the SDK root with
`python -m scripts.check_real_compose` and a local alpine:latest image. No ports or volumes are mounted.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from briefcase.cli.__main__ import main
from briefcase.cli.engine_commands import compose_services
from briefcase.engine import compose
from briefcase.engine.install import save_state


def check(directory: Path) -> None:
    os.environ["BRIEFCASE_HOME"] = str(directory)
    compose.PROJECT = "briefcase-release-check-" + uuid4().hex
    image = "alpine:latest"
    image_id = subprocess.check_output(["docker", "image", "inspect", "-f", "{{.Id}}", image], text=True).strip()
    env_file = directory / "engine.env"
    env_file.touch()
    services = ("server", "gateway", "workers", "postgres")
    generated = compose.write(directory, {s: f"localhost/briefcase-engine/{s}:fixture" for s in services}, "fixture")
    record = {"version": "fixture", "compose": str(generated), "env_file": str(env_file)}
    configured = compose_services(record)
    assert configured and all(configured.values()) and "oci-jj-server" in configured, configured
    print("Generated engine configuration:", ", ".join(sorted(configured)), flush=True)

    # Same service identity and transport checks as the shipped engine, using a harmless local image.
    generated.write_text(json.dumps({"services": {"oci-jj-server": {
        "image": image, "pull_policy": "never", "command": ["sleep", "600"],
        "environment": {"OCI_JJ_ALLOW_PLAINTEXT": "1"},
    }}}))
    record.update(images={"server": image}, image_ids={"server": image_id}, tls=False,
                  grpc_addr="fixture", gateway="fixture")
    save_state({"schema": 2, "installs": {"fixture": record}, "current": "fixture"})
    command = compose.compose_argv(generated, env_file)
    try:
        assert main(["engine", "up"]) == 0
        first = subprocess.check_output(command + ["ps", "-q", "oci-jj-server"], text=True).strip()
        assert first
        subprocess.run(command + ["stop", "-t", "1"], check=True)
        assert main(["engine", "up"]) == 0
        assert subprocess.check_output(command + ["ps", "-q", "oci-jj-server"], text=True).strip() == first
        print("Fresh startup and stopped-container reuse passed.")
    finally:
        subprocess.run(command + ["down", "-t", "1"], check=True)


if __name__ == "__main__":
    subprocess.run(["docker", "compose", "version"], check=True)
    with tempfile.TemporaryDirectory() as temporary:
        check(Path(temporary))
