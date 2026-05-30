"""Bloomberg-correction scenario: appending supersedes without mutating."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.bitemporal import (
    BitemporalRecord,
    InMemoryBitemporalStore,
    AsOfView,
    append_correction,
)


UTC = timezone.utc


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_correction_preserves_original_and_supersedes():
    store = InMemoryBitemporalStore()
    original = BitemporalRecord.new(
        key="USDC/USD",
        valid_time=_ts(0),
        value=1.0001,
        source="bloomberg",
        transaction_time=_ts(0),
    )
    store.append(original)

    correction = append_correction(
        store, original, corrected_value=1.0002, transaction_time=_ts(10)
    )

    # History preserves both.
    hist = store.history("USDC/USD")
    assert len(hist) == 2
    assert hist[0].value == 1.0001
    assert hist[1].value == 1.0002
    assert hist[1].parent_record_id == original.record_id
    # And the original is still present by record_id.
    assert any(r.record_id == original.record_id and r.value == 1.0001 for r in hist)

    # Latest now reflects the correction.
    latest = store.latest("USDC/USD")
    assert latest is not None and latest.value == 1.0002
    assert latest.record_id == correction.record_id


def test_asof_before_correction_returns_original_value():
    """This is the examiner-replay test — the decision hinges on it."""
    store = InMemoryBitemporalStore()
    original = BitemporalRecord.new(
        key="USDC/USD",
        valid_time=_ts(0),
        value=1.0001,
        source="bloomberg",
        transaction_time=_ts(0),
    )
    store.append(original)
    append_correction(store, original, corrected_value=1.0002, transaction_time=_ts(100))

    # Replay as of t=50: correction at t=100 not yet learned.
    with AsOfView(store, transaction_time=_ts(50)) as view:
        seen = view.latest("USDC/USD")
        assert seen is not None and seen.value == 1.0001

    # Replay as of t=150: correction visible.
    with AsOfView(store, transaction_time=_ts(150)) as view:
        seen = view.latest("USDC/USD")
        assert seen is not None and seen.value == 1.0002


def test_correction_must_have_newer_transaction_time():
    store = InMemoryBitemporalStore()
    original = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(10)
    )
    store.append(original)
    with pytest.raises(ValueError):
        append_correction(
            store, original, corrected_value=2, transaction_time=_ts(5)
        )


def test_correction_records_parent_in_metadata():
    store = InMemoryBitemporalStore()
    original = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(0)
    )
    store.append(original)
    correction = append_correction(
        store, original, corrected_value=2, transaction_time=_ts(10)
    )
    assert correction.metadata.get("correction_of") == original.record_id
    assert correction.valid_time == original.valid_time
