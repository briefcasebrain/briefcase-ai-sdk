"""Runs the packaged conformance suite against the OSS stores.

Third-party backends reuse the same suite by subclassing
:class:`briefcase.tests.bitemporal.BitemporalConformanceSuite` and
overriding the ``store`` fixture.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from briefcase.bitemporal import BitemporalStore, InMemoryBitemporalStore
from briefcase.bitemporal.backends import SqliteBitemporalBackend
from briefcase.tests.bitemporal import BitemporalConformanceSuite


class TestInMemoryConformance(BitemporalConformanceSuite):
    @pytest.fixture
    def store(self) -> InMemoryBitemporalStore:
        return InMemoryBitemporalStore()


class TestSqliteConformance(BitemporalConformanceSuite):
    @pytest.fixture
    def store(self) -> Iterator[BitemporalStore]:
        backend = SqliteBitemporalBackend(":memory:", wal=False)
        try:
            yield backend
        finally:
            backend.close()
