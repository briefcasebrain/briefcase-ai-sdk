"""Bundled, pinned engine stack — package data + resolvers.

Ships a pinned ``docker-compose.yml`` (published GHCR image tags) and a ``compat.json`` manifest inside
the wheel, so ``briefcase stack up`` brings up the oci-jj engine with no checkout and no Rust toolchain.
Both files are declared in ``[tool.maturin] include`` so they actually land in the wheel and sdist.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from importlib.resources import as_file, files


@contextmanager
def compose_path():
    """Yield a real filesystem path to the bundled compose.

    ``docker compose -f`` needs an on-disk path; the wheel may be a zip, so ``as_file`` materializes
    the resource for the duration of the call.
    """
    resource = files("briefcase.cli.stack") / "docker-compose.yml"
    with as_file(resource) as path:
        yield path


def load_compat() -> dict:
    """The compatibility manifest shipped in the wheel (versions + image tags + the API floor)."""
    return json.loads((files("briefcase.cli.stack") / "compat.json").read_text("utf-8"))
