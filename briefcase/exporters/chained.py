"""Tamper-evident exporter: captured records into a hash-chained SQLite store.

    import briefcase
    briefcase.observe("chain:./evidence.db")      # or ChainedSqliteExporter("evidence.db")

Each record becomes one entry of the store's hash chain (``seq``,
``prev_hash``, ``entry_hash`` over the RFC 8785 encoding of the record),
written in one transaction, so it cannot be overwritten or silently deleted
without ``briefcase-ai verify evidence.db`` noticing. A signed checkpoint is
written every ``checkpoint_every`` records and when the exporter closes
(``briefcase.shutdown()`` or interpreter exit).

Content is stored exactly as captured: whatever redaction or hashing
``@capture`` applied is what the chain holds; this exporter adds nothing.

Needs the compiled core (``briefcase._native``), which ships in the
``briefcase-ai`` wheel.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from briefcase._logging import get_logger
from briefcase.exporters.base import BaseExporter, get_field

logger = get_logger(__name__)

ENV_SIGNING_KEY = "BRIEFCASE_SIGNING_KEY"
ENV_CHECKPOINT_EVERY = "BRIEFCASE_CHECKPOINT_EVERY"


def _record_dict(decision: Any) -> Any:
    if isinstance(decision, dict):
        return decision
    to_dict = getattr(decision, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    to_json = getattr(decision, "to_json", None)
    if callable(to_json):
        return json.loads(to_json())
    return {"value": str(decision)}


class ChainedSqliteExporter(BaseExporter):
    """Append every exported record to a chained store at ``path``.

    Args:
        path: SQLite file; created owner-only (0600) if missing.
        signing_key: Ed25519 key file (private JWK, hex or raw seed; make one
            with ``briefcase-ai bundle keygen``) that signs checkpoints. Default:
            ``$BRIEFCASE_SIGNING_KEY``; without a key, checkpoints carry
            hashes only.
        checkpoint_every: records per automatic checkpoint (default
            ``$BRIEFCASE_CHECKPOINT_EVERY`` or 100; 0 = only on close).

    A record whose values RFC 8785 cannot represent exactly (NaN, Infinity,
    an integer beyond 2**53 that a double cannot hold) is refused, logged,
    and counted as failed in ``briefcase.export_stats()``; nothing is
    rounded silently. Encode such values as strings.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        signing_key: Optional[Union[str, Path, bytes]] = None,
        checkpoint_every: Optional[int] = None,
    ) -> None:
        try:
            from briefcase._native import SqliteBackend
        except ImportError as exc:
            raise ImportError(
                "ChainedSqliteExporter needs the compiled core (briefcase._native), which ships "
                "in the briefcase-ai wheel. Reinstall with: pip install --force-reinstall briefcase-ai"
            ) from exc
        self.path = Path(path)
        if signing_key is None:
            signing_key = os.environ.get(ENV_SIGNING_KEY) or None
        if checkpoint_every is None:
            checkpoint_every = int(os.environ.get(ENV_CHECKPOINT_EVERY) or 100)
        if self.path.parent and not self.path.parent.exists():
            os.makedirs(self.path.parent, mode=0o700, exist_ok=True)
        key_arg = str(signing_key) if isinstance(signing_key, Path) else signing_key
        self.backend = SqliteBackend(
            str(self.path),
            chained=True,
            signing_key=key_arg,
            checkpoint_every=int(checkpoint_every),
        )
        self.signed = signing_key is not None
        self._lock = threading.Lock()
        self._appended = 0
        self.refused = 0

    async def export(self, decision: Any) -> bool:
        record = _record_dict(decision)
        record_id = str(
            get_field(decision, "decision_id")
            or get_field(decision, "record_id")
            or get_field(decision, "id")
            or uuid.uuid4()
        )
        try:
            text = json.dumps(record, default=str, allow_nan=False, ensure_ascii=False)
        except ValueError as exc:
            self.refused += 1
            logger.warning(
                "Record %s not added to the evidence chain: %s. Encode the value as a string.",
                record_id, exc,
            )
            return False
        try:
            with self._lock:
                self.backend.append_record(record_id, text)
                self._appended += 1
        except FileExistsError:
            # Same decision exported twice: the chain already holds it, and
            # chained stores never overwrite.
            logger.info("Record %s is already in the evidence chain; not appended again.", record_id)
            return True
        except ValueError as exc:
            self.refused += 1
            logger.warning(
                "Record %s not added to the evidence chain: %s", record_id, exc,
            )
            return False
        return True

    def checkpoint(self) -> Optional[dict]:
        """Checkpoint everything appended so far; returns the checkpoint dict."""
        with self._lock:
            return self.backend.checkpoint()

    async def flush(self) -> None:
        # Every append is already committed; nothing is buffered.
        pass

    async def close(self) -> None:
        with self._lock:
            if self._appended:
                try:
                    self.backend.checkpoint()
                except Exception:
                    logger.warning("Could not write the closing checkpoint", exc_info=True)
                self._appended = 0


__all__ = ["ChainedSqliteExporter", "ENV_CHECKPOINT_EVERY", "ENV_SIGNING_KEY"]
