#!/usr/bin/env python3
"""Re-vendor oci-jj's public gRPC contract into ``briefcase/engine/_proto``.

oci-jj's ``proto/`` directory is Apache-2.0 (the engine itself is proprietary). The SDK ships:

* the ``.proto`` files the client needs (``vcs.proto`` and its imports), each with an
  SPDX header, under ``briefcase/engine/_proto/oci_jj/v1/``;
* ``oci_jj_v1.binpb``: a serialized ``FileDescriptorSet`` compiled from them with
  ``grpc_tools.protoc --include_imports``. ``briefcase.engine.client`` builds its message
  classes from this file at runtime.

Why a descriptor set and not ``*_pb2.py`` modules: code emitted by a current protoc refuses
to import under an older protobuf runtime (``ValidateProtobufRuntimeVersion``), which would
pin every user of the ``engine`` extra to one protobuf major. Message classes built from a
descriptor set work on protobuf 4.25 through 7.x, the same way the reflection client does.

    pip install grpcio-tools
    python scripts/engine/regen_proto.py /path/to/oci-jj        # rewrite the vendored copy
    python scripts/engine/regen_proto.py /path/to/oci-jj --check  # CI: fail if it is stale
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "briefcase" / "engine" / "_proto"
FILES = ("oci_jj/v1/common.proto", "oci_jj/v1/telemetry.proto", "oci_jj/v1/vcs.proto")
HEADER = (
    "// SPDX-License-Identifier: Apache-2.0\n"
    "// Vendored from oci-jj proto/{name} (commit {commit}). The oci-jj gRPC contract is\n"
    "// Apache-2.0; the engine that serves it is not part of this package.\n"
    "// Regenerate with scripts/engine/regen_proto.py; do not edit by hand.\n"
)
NOTE = ("// Vendored from oci-jj proto/{name} (commit {commit}); the engine is not part of this package.\n"
        "// Regenerate with scripts/engine/regen_proto.py; do not edit by hand.\n")


def _commit(source: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def render(source: Path) -> dict:
    """``{relative path: bytes}`` for every vendored file."""
    commit = _commit(source)
    out = {}
    for name in FILES:
        text = (source / "proto" / name).read_text(encoding="utf-8")
        if text.startswith("// SPDX-License-Identifier"):
            # Upstream carries its own licence header (oci-jj 70cb250): keep it, add the note.
            lines = text.splitlines(keepends=True)
            head = 0
            while head < len(lines) and lines[head].startswith("// ") and head < 3:
                head += 1
            text = "".join(lines[:head]) + NOTE.format(name=name, commit=commit) + "".join(lines[head:])
        else:
            text = HEADER.format(name=name, commit=commit) + text
        out[name] = text.encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, data in out.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        target = root / "set.binpb"
        from grpc_tools import protoc

        code = protoc.main(["protoc", f"-I{root}", "--include_imports",
                            f"--descriptor_set_out={target}", *[str(root / n) for n in FILES]])
        if code != 0:
            raise SystemExit(f"protoc failed ({code})")
        out["oci_jj_v1.binpb"] = target.read_bytes()
    licence = source / "proto" / "LICENSE"
    if licence.exists():
        out["LICENSE"] = licence.read_bytes()
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="An oci-jj checkout (reads its proto/ directory).")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the vendored copy differs.")
    args = parser.parse_args(argv)
    rendered = render(args.source)
    stale = [name for name, data in rendered.items()
             if not (OUT / name).exists() or (OUT / name).read_bytes() != data]
    if args.check:
        for name in stale:
            print(f"stale: briefcase/engine/_proto/{name}", file=sys.stderr)
        return 1 if stale else 0
    for name, data in rendered.items():
        (OUT / name).parent.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_bytes(data)
    print(f"wrote {len(rendered)} files to {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
