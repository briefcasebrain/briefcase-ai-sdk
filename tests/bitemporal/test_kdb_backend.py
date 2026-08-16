"""Unit tests for :class:`briefcase.bitemporal.backends.kdb.KdbBitemporalBackend`.

``pykx`` is a stub installed in ``tests/bitemporal/conftest.py``. Each
test patches the stub's ``QConnection`` with a :class:`FakeConn` that
records every q expression it receives so we can assert on wire format
without needing a real kdb+ instance.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

import pykx  # the conftest stub

from briefcase.bitemporal import BitemporalRecord, BitemporalStore
from briefcase.bitemporal.backends.kdb import KdbBitemporalBackend

UTC = timezone.utc


class _MockPy:
    """Stand-in for a pykx scalar / list result with a ``.py()`` accessor."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def py(self) -> Any:
        return self._value


class FakeConn:
    """Records every q call and returns queued / heuristic responses."""

    def __init__(self, **_kwargs: Any) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._returns: list[Any] = []
        self.count_result: int = 0

    def queue(self, value: Any) -> None:
        self._returns.append(value)

    def __call__(self, q_expr: str, *args: Any) -> Any:
        self.calls.append((q_expr, args))
        if self._returns:
            return self._returns.pop(0)
        stripped = q_expr.lstrip()
        if stripped.startswith("count "):
            return _MockPy(self.count_result)
        if stripped.startswith("distinct "):
            return _MockPy([])
        if stripped.startswith("1#") or "xdesc" in q_expr:
            return _MockPy([])
        return _MockPy(None)

    def close(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeConn]:
    conn = FakeConn()

    def _factory(**_kwargs: Any) -> FakeConn:
        return conn

    monkeypatch.setattr(pykx, "QConnection", _factory)
    # pykx.q is used for datetime conversion; return a sentinel MockPy.
    monkeypatch.setattr(pykx, "q", lambda *_args, **_kwargs: _MockPy(None))
    yield conn


def _record(key: str = "k", px: float = 1.0) -> BitemporalRecord:
    # transaction_time is pinned so correction tests do not depend on the
    # wall clock.
    return BitemporalRecord.new(
        key=key,
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        transaction_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": px},
        source="test",
        source_trust_level="primary",
    )


def test_satisfies_bitemporal_store_protocol() -> None:
    assert isinstance(KdbBitemporalBackend(), BitemporalStore)


def test_schema_created_on_first_op(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    backend.append(_record())
    create_calls = [c for c in fake_conn.calls if "bitemporal_records:" in c[0]]
    assert create_calls, "CREATE TABLE q expression never ran"


def test_append_roundtrip(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    backend.append(_record())
    upsert_calls = [c for c in fake_conn.calls if "upsert" in c[0]]
    assert upsert_calls, "upsert never issued"


def test_duplicate_record_id_rejected(fake_conn: FakeConn) -> None:
    fake_conn.count_result = 1  # every count(*) returns 1 -> append refuses
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    with pytest.raises(ValueError, match="already present"):
        backend.append(_record())


def test_as_of_orders_by_transaction_time_desc(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    backend.as_of("k")
    select_calls = [c for c in fake_conn.calls if "xdesc" in c[0]]
    assert select_calls, "as_of did not emit an ordered select"
    q_expr = select_calls[0][0]
    # ORDER BY transaction_time DESC, valid_time DESC, is_correction DESC, LIMIT 1.
    assert "transaction_time" in q_expr
    assert "valid_time" in q_expr
    assert "is_correction" in q_expr
    assert q_expr.lstrip().startswith("1#"), "LIMIT 1 marker missing"


def test_schema_applies_group_attributes(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    backend.as_of("k")
    attr_calls = [c for c in fake_conn.calls if "`g#" in c[0] and "update" in c[0]]
    assert attr_calls, "grouped-attribute update not applied on first connect"
    q_expr = attr_calls[0][0]
    for column in ("key", "valid_time", "transaction_time", "record_id"):
        assert f"`g#{column}" in q_expr


def test_append_correction_honors_trust_level_override(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    parent = _record()
    correction = backend.append_correction(
        parent,
        corrected_value={"px": 1.0002},
        corrected_at=datetime(2026, 5, 17, tzinfo=UTC),
        source="verified-feed",
        source_trust_level="verified",
        metadata={"reason": "upstream correction"},
    )
    assert correction.source_trust_level == "verified"
    assert parent.source_trust_level == "primary"  # parent untouched
    assert correction.parent_record_id == parent.record_id
    assert correction.key == parent.key
    assert correction.valid_time == parent.valid_time
    assert correction.metadata["reason"] == "upstream correction"
    assert correction.metadata["correction_of"] == parent.record_id


def test_append_correction_rejects_non_monotonic_transaction_time(
    fake_conn: FakeConn,
) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    parent = _record()
    with pytest.raises(ValueError, match="strictly after"):
        backend.append_correction(
            parent,
            corrected_value={"px": 1.0002},
            corrected_at=parent.transaction_time,  # equal, not later
            source="verified-feed",
        )


def test_naive_datetime_rejected(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    with pytest.raises(ValueError, match="naive datetime rejected"):
        backend.as_of("k", transaction_time=datetime(2026, 4, 17))


def test_context_manager_closes(fake_conn: FakeConn, monkeypatch: pytest.MonkeyPatch) -> None:
    closed = {"flag": False}

    def _close() -> None:
        closed["flag"] = True

    monkeypatch.setattr(fake_conn, "close", _close)
    with KdbBitemporalBackend(host="localhost", port=5000) as backend:
        backend.append(_record())
    assert closed["flag"]


def test_query_alias_is_as_of() -> None:
    assert KdbBitemporalBackend.query is KdbBitemporalBackend.as_of


# ----------------------------------------------------------------------
# Injection rejection. Caller data embedded in q expressions must match a
# tight allowlist; a double quote or backslash would otherwise terminate
# the q string literal and execute attacker-controlled q.
# ----------------------------------------------------------------------

INJECTION_VALUES = [
    'x" ; system "ls',
    'x\\" ; 0N!x ; "',
    "x\\y",
    'plain"quote',
    "spaced key",
    "semi;colon",
    "back`tick",
]


@pytest.mark.parametrize("bad", INJECTION_VALUES)
def test_as_of_rejects_unsafe_key(fake_conn: FakeConn, bad: str) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    with pytest.raises(ValueError, match="key"):
        backend.as_of(bad)
    assert not any(bad in q for q, _ in fake_conn.calls), "unsafe key reached q"


@pytest.mark.parametrize("bad", INJECTION_VALUES)
def test_history_rejects_unsafe_key(fake_conn: FakeConn, bad: str) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    with pytest.raises(ValueError, match="key"):
        backend.history(bad)
    assert not any(bad in q for q, _ in fake_conn.calls), "unsafe key reached q"


@pytest.mark.parametrize("bad", INJECTION_VALUES)
def test_append_rejects_unsafe_record_id(fake_conn: FakeConn, bad: str) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    record = BitemporalRecord.new(
        key="k",
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": 1.0},
        source="test",
        record_id=bad,
    )
    with pytest.raises(ValueError, match="record_id"):
        backend.append(record)
    assert not any("upsert" in q for q, _ in fake_conn.calls), "row was written"


def test_append_rejects_unsafe_key(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    record = BitemporalRecord.new(
        key='k" ; system "ls',
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": 1.0},
        source="test",
    )
    with pytest.raises(ValueError, match="key"):
        backend.append(record)


def test_safe_symbols_accepted(fake_conn: FakeConn) -> None:
    backend = KdbBitemporalBackend(host="localhost", port=5000)
    backend.append(_record(key="USDC/USD"))
    backend.as_of("ofac_sdn:12345")
    backend.history("a.b-c_d")


def test_unsafe_table_name_rejected() -> None:
    with pytest.raises(ValueError, match="table"):
        KdbBitemporalBackend(host="localhost", port=5000, table='t" ; system "ls')


def test_trailing_newline_in_key_is_rejected(fake_conn):
    """$-anchored regexes accept one trailing newline; fullmatch must not."""
    backend = KdbBitemporalBackend()
    with pytest.raises(ValueError):
        backend.latest("USDC/USD\n")


def test_trailing_newline_in_table_name_is_rejected():
    with pytest.raises(ValueError):
        KdbBitemporalBackend(table="bitemporal_records\n")
