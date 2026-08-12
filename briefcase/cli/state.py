"""Local registries for the ``briefcase`` evaluation-run CLI.

``Store`` is the durable, oci-jj-agnostic state behind the run lifecycle: the datasets you
``register``, the secrets you ``set``, and the runs you ``submit``. Everything is plain JSON written
atomically under ``~/.briefcase`` (override with ``$BRIEFCASE_HOME`` — what the tests point at a tmp
dir). The run *name* is its handle.

This module has no knowledge of oci-jj or subprocesses; it is pure, so it unit-tests without a stack.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class Store:
    def __init__(self, home: Path | None = None) -> None:
        if home is None:
            env = os.environ.get("BRIEFCASE_HOME")
            home = Path(env) if env else Path.home() / ".briefcase"
        self.home = Path(home)
        # Owner-only: the store holds secret values, so nothing here is group/world readable.
        # Opening the store tightens the directory and any registry it finds.
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            self._restrict(self.home, 0o700)
            for existing in self.home.glob("*.json"):
                self._restrict(existing, 0o600)

    @staticmethod
    def _restrict(path: Path, mode: int) -> None:
        # Symlinks are skipped: chmod follows them, and a planted link must
        # not change the mode of a file outside the store.
        try:
            if path.is_symlink():
                return
            if (path.stat().st_mode & 0o777) != mode:
                path.chmod(mode)
        except OSError:
            pass

    # ---- low-level JSON store (atomic) ----
    def _path(self, name: str) -> Path:
        return self.home / f"{name}.json"

    def _load(self, name: str) -> dict:
        p = self._path(name)
        if not p.exists():
            return {}
        return json.loads(p.read_text())

    def _save(self, name: str, obj: dict) -> None:
        # Serialize first so a non-serializable payload raises *before* we touch any file — the
        # existing registry is left intact and no temp file lingers (see test_failed_save_*).
        data = json.dumps(obj, indent=2, sort_keys=True)
        # mkstemp gives a per-call unique 0600 temp file, so concurrent writers
        # never collide; os.replace carries the mode onto the final file.
        fd, tmp = tempfile.mkstemp(dir=self.home, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, self._path(name))  # atomic on POSIX
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    # ---- datasets ----
    def register_dataset(self, name: str, uri: str) -> dict:
        rec = {"name": name, "uri": uri}
        datasets = self._load("datasets")
        datasets[name] = rec
        self._save("datasets", datasets)
        return rec

    def get_dataset(self, name: str) -> dict | None:
        return self._load("datasets").get(name)

    def list_datasets(self) -> list[dict]:
        return sorted(self._load("datasets").values(), key=lambda r: r["name"])

    # ---- secrets ----
    def set_secret(self, key: str, value: str) -> None:
        secrets = self._load("secrets")
        secrets[key] = value
        self._save("secrets", secrets)

    def get_secrets(self) -> dict:
        return dict(self._load("secrets"))

    def list_secret_keys(self) -> list[str]:
        return sorted(self._load("secrets").keys())

    # ---- runs (keyed by the job name) ----
    def record_run(self, run: dict) -> str:
        run_id = run.get("id") or run["name"]
        record = {**run, "id": run_id}
        record.setdefault("created_at", time.time())
        runs = self._load("runs")
        runs[run_id] = record
        self._save("runs", runs)
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        return self._load("runs").get(run_id)

    def list_runs(self) -> list[dict]:
        return sorted(self._load("runs").values(), key=lambda r: r.get("created_at", 0))

    def update_run(self, run_id: str, **fields) -> dict:
        runs = self._load("runs")
        if run_id not in runs:
            raise KeyError(run_id)
        runs[run_id] = {**runs[run_id], **fields}
        self._save("runs", runs)
        return runs[run_id]

    def delete_run(self, run_id: str) -> bool:
        runs = self._load("runs")
        if run_id not in runs:
            return False
        del runs[run_id]
        self._save("runs", runs)
        return True
