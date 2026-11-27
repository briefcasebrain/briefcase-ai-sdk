"""Verify the migration CLI refuses to provision an engine, without a daemon or network."""
from pathlib import Path
import os
import tempfile

from briefcase.cli.__main__ import main


def check(directory: Path) -> None:
    os.environ["BRIEFCASE_HOME"] = str(directory)
    for verb in ("install", "up", "upgrade"):
        try:
            main(["engine", verb])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"retired verb {verb} was accepted")
    assert not (directory / "engine").exists()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        check(Path(temporary))
