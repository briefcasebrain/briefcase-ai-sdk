"""AsOfView — the API-wrapping pattern. No-lookahead guarantee."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.bitemporal import (
    BitemporalRecord,
    InMemoryBitemporalStore,
    AsOfView,
)
from briefcase.bitemporal.asof import AsOfViewWriteError


UTC = timezone.utc


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_asof_requires_a_clamp():
    store = InMemoryBitemporalStore()
    with pytest.raises(ValueError):
        AsOfView(store)


def test_asof_hides_records_learned_after_clamp():
    store = InMemoryBitemporalStore()
    early = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(0)
    )
    late = BitemporalRecord.new(
        key="k", valid_time=_ts(10), value=2, source="s", transaction_time=_ts(20)
    )
    store.append_many([early, late])
    with AsOfView(store, transaction_time=_ts(5)) as v:
        # Late record not visible.
        assert v.latest("k").value == 1
        assert len(v.history("k")) == 1


def test_asof_history_is_filtered():
    store = InMemoryBitemporalStore()
    for i in range(5):
        store.append(BitemporalRecord.new(
            key="k", valid_time=_ts(i), value=i, source="s",
            transaction_time=_ts(i * 10),
        ))
    with AsOfView(store, transaction_time=_ts(25)) as v:
        # transaction_times 0, 10, 20 visible; 30, 40 hidden.
        assert [r.value for r in v.history("k")] == [0, 1, 2]


def test_asof_keys_only_show_visible():
    store = InMemoryBitemporalStore()
    store.append(BitemporalRecord.new(
        key="early", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(0),
    ))
    store.append(BitemporalRecord.new(
        key="late", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(100),
    ))
    with AsOfView(store, transaction_time=_ts(50)) as v:
        assert v.keys() == ["early"]


def test_asof_refuses_writes():
    store = InMemoryBitemporalStore()
    view = AsOfView(store, transaction_time=_ts(10))
    r = BitemporalRecord.new(key="k", valid_time=_ts(), value=1, source="s")
    with pytest.raises(AsOfViewWriteError):
        view.append(r)
    with pytest.raises(AsOfViewWriteError):
        view.append_many([r])


def test_asof_narrower_caller_clamp_wins():
    store = InMemoryBitemporalStore()
    for i in range(3):
        store.append(BitemporalRecord.new(
            key="k", valid_time=_ts(i), value=i, source="s",
            transaction_time=_ts(i * 10),
        ))
    view = AsOfView(store, transaction_time=_ts(25))
    # Caller clamps narrower — at t=5 only the first row is visible.
    seen = view.as_of("k", transaction_time=_ts(5))
    assert seen is not None and seen.value == 0


def test_asof_caller_cannot_widen_beyond_view():
    store = InMemoryBitemporalStore()
    for i in range(3):
        store.append(BitemporalRecord.new(
            key="k", valid_time=_ts(i), value=i, source="s",
            transaction_time=_ts(i * 10),
        ))
    view = AsOfView(store, transaction_time=_ts(5))
    # Caller tries to widen to t=100 — view's clamp still binds.
    seen = view.as_of("k", transaction_time=_ts(100))
    assert seen is not None and seen.value == 0


def test_no_lookahead_in_backtest_scenario():
    """End-to-end backtest scenario using as-of.

    A training fact is later corrected. A naive backtest (latest()) sees
    the corrected value and overfits; an as-of-wrapped backtest sees the
    original value and evaluates cleanly.
    """
    store = InMemoryBitemporalStore()
    decision_ts = _ts(100)
    # At decision time, system knew price was 1.0001.
    store.append(BitemporalRecord.new(
        key="USDC/USD", valid_time=_ts(100), value=1.0001, source="bloomberg",
        transaction_time=_ts(100),
    ))
    # A week later, a correction arrived.
    from briefcase.bitemporal import append_correction
    original = store.history("USDC/USD")[0]
    append_correction(store, original, corrected_value=1.0002, transaction_time=_ts(700))

    # Naive backtest: sees corrected value — contamination.
    naive = store.latest("USDC/USD")
    assert naive.value == 1.0002

    # As-of-wrapped backtest: sees original value — clean.
    with AsOfView(store, transaction_time=decision_ts) as view:
        clean = view.latest("USDC/USD")
        assert clean.value == 1.0001

    # The divergence is the whole point.
    assert naive.value != clean.value
