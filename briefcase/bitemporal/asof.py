"""
AsOfView — the API-wrapping pattern.

Wrap any :class:`~briefcase.bitemporal.BitemporalStore` in an ``AsOfView``
to clamp reads to a historical ``transaction_time`` (and optionally
``valid_time``). Application code keeps calling ``latest(key)`` /
``as_of(key)`` unchanged; the view ensures no post-decision information
leaks into the caller.

This is the primitive that makes backtests free of look-ahead bias and
examiner replays reproducible by construction.

Writes are refused: a view of the past does not accept new knowledge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from briefcase.bitemporal.record import BitemporalRecord
from briefcase.bitemporal.store import BitemporalStore


class AsOfViewWriteError(RuntimeError):
    """Raised when write operations are attempted on an AsOfView."""


class AsOfView:
    """Read-only view of a :class:`BitemporalStore` clamped to an as-of point.

    Parameters
    ----------
    store
        The underlying store. Typically the production store.
    transaction_time
        Only records with ``transaction_time <= transaction_time`` are
        visible. This is the core clamp — it is what prevents look-ahead
        bias in backtests and what makes examiner replay reproducible.
    valid_time
        Optional. When supplied, further restricts visibility to records
        whose ``valid_time <= valid_time``. Used when reconstructing the
        state of the world at a past real-world moment (as distinct from
        what the system had learned by then).

    Usage
    -----
        with AsOfView(store, transaction_time=decision_ts) as view:
            price = view.latest("USDC/USD")
            ...

    The context manager form is preferred for readability; the instance can
    also be used directly without ``with``.
    """

    def __init__(
        self,
        store: BitemporalStore,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> None:
        if transaction_time is None and valid_time is None:
            raise ValueError(
                "AsOfView requires transaction_time and/or valid_time"
            )
        self._store = store
        self._tx = transaction_time
        self._vt = valid_time

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "AsOfView":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def transaction_time(self) -> Optional[datetime]:
        return self._tx

    @property
    def valid_time(self) -> Optional[datetime]:
        return self._vt

    # ------------------------------------------------------------------
    # Reads — delegate to the store with the clamp applied.
    # ------------------------------------------------------------------

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        return self._store.as_of(
            key, transaction_time=self._tx, valid_time=self._vt
        )

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        # Caller-supplied clamps narrow further; they must not widen.
        tx = transaction_time if transaction_time is not None else self._tx
        vt = valid_time if valid_time is not None else self._vt
        if self._tx is not None and tx is not None and tx > self._tx:
            tx = self._tx
        if self._vt is not None and vt is not None and vt > self._vt:
            vt = self._vt
        return self._store.as_of(key, transaction_time=tx, valid_time=vt)

    def history(self, key: str) -> List[BitemporalRecord]:
        rows = self._store.history(key)
        return [
            r for r in rows
            if (self._tx is None or r.transaction_time <= self._tx)
            and (self._vt is None or r.valid_time <= self._vt)
        ]

    def keys(self) -> List[str]:
        # A key is visible only if at least one of its rows is visible.
        visible: List[str] = []
        for k in self._store.keys():
            if self.history(k):
                visible.append(k)
        return visible

    # ------------------------------------------------------------------
    # Writes — refused.
    # ------------------------------------------------------------------

    def append(self, record: BitemporalRecord) -> None:
        raise AsOfViewWriteError(
            "AsOfView is read-only. Writes would leak post-as-of "
            "knowledge into a view of the past."
        )

    def append_many(self, records: Iterable[BitemporalRecord]) -> None:
        raise AsOfViewWriteError("AsOfView is read-only.")
