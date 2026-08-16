"""Conformance suite for any :class:`briefcase.bitemporal.BitemporalStore`.

Usage
-----
In a test module, subclass the suite with a pytest-collectable name and
override the ``store`` fixture to yield the backend under test::

    import pytest
    from briefcase.tests.bitemporal import BitemporalConformanceSuite
    from my_package.backends import MyBackend

    class TestMyBackendConformance(BitemporalConformanceSuite):
        @pytest.fixture
        def store(self):
            backend = MyBackend(...)
            try:
                yield backend
            finally:
                backend.close()

Every test in the suite runs against the fixture. The subclass owns
fixture teardown. Requires pytest at import time, so import this module
from test code only.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from briefcase.bitemporal import (
    BitemporalRecord,
    BitemporalStore,
    append_correction,
)


UTC = timezone.utc


def _record(key: str = "k", px: float = 1.0) -> BitemporalRecord:
    return BitemporalRecord.new(
        key=key,
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": px},
        source="conformance",
        source_trust_level="primary",
    )


class BitemporalConformanceSuite:
    """Mixin exercising the ``BitemporalStore`` Protocol contract.

    Subclasses provide the backend via the ``store`` fixture. The base
    class is not named ``Test*`` so pytest never collects it directly;
    only named subclasses run.
    """

    @pytest.fixture
    def store(self) -> BitemporalStore:
        raise NotImplementedError(
            "subclasses must override the `store` fixture to yield the "
            "backend under test"
        )

    def test_satisfies_protocol(self, store: BitemporalStore) -> None:
        assert isinstance(store, BitemporalStore)

    def test_append_latest_roundtrip(self, store: BitemporalStore) -> None:
        r = _record()
        store.append(r)
        latest = store.latest("k")
        assert latest is not None
        assert latest.record_id == r.record_id

    def test_append_many(self, store: BitemporalStore) -> None:
        store.append_many([_record(px=1.0), _record(key="m", px=2.0)])
        assert set(store.keys()) == {"k", "m"}

    def test_history_order(self, store: BitemporalStore) -> None:
        r1 = _record(px=1.0)
        store.append(r1)
        store.append(
            BitemporalRecord.new(
                key="k",
                valid_time=datetime(2026, 4, 18, tzinfo=UTC),
                value={"px": 1.5},
                source="conformance",
            )
        )
        hist = store.history("k")
        assert len(hist) == 2
        assert hist[0].transaction_time <= hist[1].transaction_time

    def test_as_of_clamps_transaction_time(self, store: BitemporalStore) -> None:
        r1 = BitemporalRecord.new(
            key="k",
            valid_time=datetime(2026, 4, 17, tzinfo=UTC),
            value={"px": 1.0001},
            source="conformance",
            transaction_time=datetime(2026, 4, 17, tzinfo=UTC),
        )
        store.append(r1)
        correction = append_correction(
            store,
            r1,
            corrected_value={"px": 1.0002},
            transaction_time=datetime(2026, 5, 17, tzinfo=UTC),
        )

        latest = store.latest("k")
        assert latest is not None
        assert latest.value == {"px": 1.0002}
        early = store.as_of(
            "k", transaction_time=datetime(2026, 5, 1, tzinfo=UTC)
        )
        assert early is not None and early.value == {"px": 1.0001}
        late = store.as_of(
            "k", transaction_time=datetime(2026, 6, 1, tzinfo=UTC)
        )
        assert late is not None and late.record_id == correction.record_id


__all__ = ["BitemporalConformanceSuite"]
