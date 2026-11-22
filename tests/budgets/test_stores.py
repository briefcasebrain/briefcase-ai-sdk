"""The store contract, for every store, and its atomicity under concurrent
reservations: threads, asyncio tasks, and two OS processes against the
shared stores."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
import time
import uuid

import pytest

from briefcase.budgets.stores import MemoryBudgetStore, SQLBudgetStore, store_from_url


def test_reserve_within_and_over_limit(any_store):
    store, _, _ = any_store
    key = f"k-{uuid.uuid4().hex[:6]}"
    assert store.reserve(key, 600, 10, 1000, None, None) == (True, 600, 10)
    assert store.reserve(key, 400, 10, 1000, None, None) == (True, 1000, 20)
    # One nano-dollar over: refused, counter unchanged.
    assert store.reserve(key, 1, 1, 1000, None, None) == (False, 1000, 20)
    assert store.get(key) == (1000, 20, 1000, 20)


def test_token_limit_and_force(any_store):
    store, _, _ = any_store
    key = f"t-{uuid.uuid4().hex[:6]}"
    assert store.reserve(key, 0, 90, None, 100, None)[0]
    assert store.reserve(key, 0, 11, None, 100, None) == (False, 0, 90)
    assert store.reserve(key, 0, 11, None, 100, None, force=True) == (True, 0, 101)


def test_adjust_settles_and_releases(any_store):
    store, _, _ = any_store
    key = f"a-{uuid.uuid4().hex[:6]}"
    store.reserve(key, 1000, 50, 5000, None, None)
    # Actual cost 300: used -= 700, in-flight -= 1000.
    store.adjust(key, -700, -20, -1000, -50, None)
    assert store.get(key) == (300, 30, 0, 0)


def test_scan_lists_keys_by_prefix(any_store):
    store, _, _ = any_store
    tag = uuid.uuid4().hex[:6]
    store.reserve(f"daily_{tag}|*|2026-01-01", 5, 1, None, None, None)
    store.reserve(f"daily_{tag}|tenant=a%b|2026-01-01", 7, 1, None, None, None)
    store.reserve(f"dailyX{tag}|*|2026-01-01", 9, 1, None, None, None)
    rows = dict(store.scan(f"daily_{tag}|"))
    assert set(rows) == {f"daily_{tag}|*|2026-01-01", f"daily_{tag}|tenant=a%b|2026-01-01"}
    assert rows[f"daily_{tag}|*|2026-01-01"][0] == 5


def test_expired_counters_start_again():
    now = [1000.0]
    store = MemoryBudgetStore(clock=lambda: now[0])
    store.reserve("k", 10, 1, None, None, 1500)
    now[0] = 1600.0
    assert store.reserve("k", 10, 1, 15, None, 2000) == (True, 10, 1)


def test_threads_never_overshoot(any_store):
    store, _, _ = any_store
    key = f"thr-{uuid.uuid4().hex[:6]}"
    ok = []
    lock = threading.Lock()

    def worker():
        for _ in range(15):
            if store.reserve(key, 7, 1, 700, None, None)[0]:
                with lock:
                    ok.append(1)

    threads = [threading.Thread(target=worker) for _ in range(12)]  # 180 attempts, 100 fit
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 100
    assert store.get(key)[0] == 700


def test_asyncio_tasks_never_overshoot(any_store):
    store, _, _ = any_store
    key = f"aio-{uuid.uuid4().hex[:6]}"

    async def one():
        return (await asyncio.to_thread(store.reserve, key, 3, 1, 90, None, None))[0]

    async def main():
        return await asyncio.gather(*(one() for _ in range(60)))

    results = asyncio.run(main())
    assert sum(results) == 30
    assert store.get(key)[0] == 90


def _process_worker(url, prefix, key, attempts, queue):
    from briefcase.budgets.stores import RedisBudgetStore, store_from_url

    store = RedisBudgetStore(url, prefix=prefix) if url.startswith("redis") else store_from_url(url)
    won = 0
    for _ in range(attempts):
        if store.reserve(key, 5, 1, 500, None, None)[0]:
            won += 1
    queue.put(won)


def test_two_processes_never_overshoot(any_store):
    store, url, kind = any_store
    if url is None:
        pytest.skip(f"{kind} store is in-process only")
    key = f"proc-{uuid.uuid4().hex[:6]}"
    prefix = getattr(store, "prefix", "")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_process_worker, args=(url, prefix, key, 80, queue))
             for _ in range(2)]
    for p in procs:
        p.start()
    wins = [queue.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert sum(wins) == 100  # 160 attempts, exactly 500/5 fit
    assert store.get(key)[0] == 500


def test_sqlite_schema_is_documented_and_created(tmp_path):
    import sqlite3

    store = store_from_url(f"sqlite:///{tmp_path}/b.db")
    assert isinstance(store, SQLBudgetStore)
    columns = [r[1] for r in sqlite3.connect(tmp_path / "b.db").execute(
        "PRAGMA table_info(briefcase_budget_usage)")]
    assert columns == ["budget_key", "used_nano", "used_tokens", "inflight_nano",
                       "inflight_tokens", "expires_at"]


def test_sqlalchemy_engine_is_accepted(tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/sa.db")
    store = SQLBudgetStore(engine)
    assert store.dialect == "sqlite"
    assert store.reserve("k", 1, 1, 1, None, None) == (True, 1, 1)


def test_unknown_url_and_dialect_are_refused():
    with pytest.raises(ValueError, match="unknown budget store URL"):
        store_from_url("mongodb://x")
    with pytest.raises(ValueError, match="dialect"):
        SQLBudgetStore(lambda: None, "oracle", create=False)
    with pytest.raises(ValueError, match="table name"):
        SQLBudgetStore(lambda: None, "sqlite", table="x; drop", create=False)


def test_memory_store_contract_is_fast():
    store = MemoryBudgetStore()
    start = time.perf_counter()
    for i in range(2000):
        store.reserve("k", 1, 1, None, None, None)
    assert (time.perf_counter() - start) / 2000 < 0.0005
