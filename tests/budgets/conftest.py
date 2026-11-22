"""Budget store fixtures.

Shared stores are exercised against real servers when they are available:

- Redis: ``BRIEFCASE_TEST_REDIS_URL``; else a ``redis-server`` found on PATH is
  started on a free port for the session; else ``fakeredis`` (in-process, so
  the two-process test is skipped); else skipped.
- PostgreSQL: ``BRIEFCASE_TEST_POSTGRES_URL`` (e.g.
  ``postgresql://postgres@127.0.0.1:5432/budgets``); else skipped.
- MySQL: ``BRIEFCASE_TEST_MYSQL_URL`` (e.g. ``mysql://root@127.0.0.1:3306/budgets``);
  else skipped.
- SQLite: always (a file in the test's temporary directory).

Nothing here touches the network beyond 127.0.0.1.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

import briefcase.integrations.providers._base as base


@pytest.fixture(autouse=True)
def _no_hooks_left():
    """Every test starts and ends with no call hook and no active budgets."""
    from briefcase import budgets
    from briefcase.guardrails import content

    budgets.disable()
    content.disable()
    yield
    budgets.disable()
    content.disable()
    assert base.call_hooks() == ()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory):
    """``(url or None, kind)`` where kind is ``real`` or ``fake``."""
    url = os.environ.get("BRIEFCASE_TEST_REDIS_URL")
    try:
        import redis
    except ImportError:
        redis = None
    if url and redis is not None:
        yield url, "real"
        return
    binary = shutil.which("redis-server")
    if binary and redis is not None:
        port = _free_port()
        directory = tmp_path_factory.mktemp("redis")
        proc = subprocess.Popen(
            [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly",
             "no", "--dir", str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = redis.Redis(host="127.0.0.1", port=port)
        for _ in range(100):
            try:
                client.ping()
                break
            except Exception:
                time.sleep(0.05)
        else:  # pragma: no cover - environment specific
            proc.kill()
            yield None, "none"
            return
        try:
            yield f"redis://127.0.0.1:{port}/0", "real"
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        return
    yield None, "fake"


def _redis_store(redis_url):
    from briefcase.budgets.stores import RedisBudgetStore

    url, kind = redis_url
    prefix = f"bctest:{uuid.uuid4().hex[:8]}:"
    if url is not None:
        return RedisBudgetStore(url, prefix=prefix)
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa", reason="fakeredis needs lupa to run the Lua scripts")
    return RedisBudgetStore(fakeredis.FakeRedis(), prefix=prefix)


def _sql_store(kind, tmp_path):
    from briefcase.budgets.stores import store_from_url

    if kind == "sqlite":
        url = f"sqlite:///{tmp_path}/budgets.db"
    else:
        url = os.environ.get(f"BRIEFCASE_TEST_{kind.upper()}_URL")
        if not url:
            pytest.skip(f"BRIEFCASE_TEST_{kind.upper()}_URL is not set (no real {kind} server)")
    store = store_from_url(url)
    store.reset("")
    return store, url


STORES = ["memory", "sqlite", "redis", "postgres", "mysql"]


@pytest.fixture(params=STORES)
def any_store(request, tmp_path, redis_url):
    """``(store, url or None, kind)`` for each store kind."""
    from briefcase.budgets.stores import MemoryBudgetStore

    kind = request.param
    if kind == "memory":
        return MemoryBudgetStore(), None, kind
    if kind == "redis":
        store = _redis_store(redis_url)
        url = redis_url[0]
        return store, (url if url else None), ("redis" if url else "fakeredis")
    store, url = _sql_store(kind, tmp_path)
    return store, url, kind
