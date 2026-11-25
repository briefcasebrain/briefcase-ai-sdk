"""Where budget counters live.

Every store keeps, per key, four integers: ``used_nano`` (nano-USD spent or
reserved), ``used_tokens``, and the part of each that is still a reservation
in flight (``inflight_nano``, ``inflight_tokens``). Two operations change them:

- ``reserve(key, nano, tokens, limit_nano, limit_tokens, expires_at, force)``
  adds ``nano`` / ``tokens`` to both used and in-flight **only if** the result
  stays within the limits (``None``: no limit; ``force``: ignore limits), as
  one atomic step. Returns ``(ok, used_nano, used_tokens)``: the counter after
  the reservation, or as it stood when refused.
- ``adjust(key, d_nano, d_tokens, d_inflight_nano, d_inflight_tokens,
  expires_at)`` adds the deltas unconditionally (settle and release).

Shared stores (:class:`RedisBudgetStore`, :class:`SQLBudgetStore`) make a
limit hold across processes and hosts. A process that dies between reserve
and settle leaves its reservation counted (the safe direction).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from briefcase._logging import get_logger

logger = get_logger("briefcase.budgets")

Counter = Tuple[int, int, int, int]


class MemoryBudgetStore:
    """In-process counters (the default). Limits hold across threads and
    asyncio tasks of one process only."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._data: Dict[str, List[int]] = {}
        self._expiry: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def _row(self, key: str) -> List[int]:
        expires = self._expiry.get(key)
        if expires is not None and expires <= self._clock():
            self._data.pop(key, None)
            self._expiry.pop(key, None)
        return self._data.setdefault(key, [0, 0, 0, 0])

    def reserve(self, key: str, nano: int, tokens: int, limit_nano: Optional[int],
                limit_tokens: Optional[int], expires_at: Optional[int],
                force: bool = False) -> Tuple[bool, int, int]:
        with self._lock:
            row = self._row(key)
            if not force and ((limit_nano is not None and row[0] + nano > limit_nano)
                              or (limit_tokens is not None and row[1] + tokens > limit_tokens)):
                return False, row[0], row[1]
            row[0] += nano
            row[1] += tokens
            row[2] += nano
            row[3] += tokens
            if expires_at is not None:
                self._expiry[key] = expires_at
            return True, row[0], row[1]

    def adjust(self, key: str, d_nano: int, d_tokens: int, d_inflight_nano: int,
               d_inflight_tokens: int, expires_at: Optional[int]) -> None:
        with self._lock:
            row = self._row(key)
            row[0] += d_nano
            row[1] += d_tokens
            row[2] += d_inflight_nano
            row[3] += d_inflight_tokens
            if expires_at is not None:
                self._expiry[key] = expires_at

    def get(self, key: str) -> Counter:
        with self._lock:
            row = self._data.get(key)
            return (row[0], row[1], row[2], row[3]) if row else (0, 0, 0, 0)

    def scan(self, prefix: str) -> List[Tuple[str, Counter]]:
        with self._lock:
            now = self._clock()
            return [(k, (v[0], v[1], v[2], v[3])) for k, v in self._data.items()
                    if k.startswith(prefix) and (self._expiry.get(k) or now + 1) > now]

    def reset(self, prefix: str = "") -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
                self._expiry.pop(k, None)
            return len(keys)

    def describe(self) -> str:
        return "memory (this process only)"


# -- Redis ----------------------------------------------------------------------

_RESERVE_LUA = """
local u = tonumber(redis.call('HGET', KEYS[1], 'u') or '0')
local t = tonumber(redis.call('HGET', KEYS[1], 't') or '0')
local nu = tonumber(ARGV[1])
local nt = tonumber(ARGV[2])
local lu = tonumber(ARGV[3])
local lt = tonumber(ARGV[4])
if ARGV[5] ~= '1' then
  if (lu >= 0 and u + nu > lu) or (lt >= 0 and t + nt > lt) then
    return {0, u, t}
  end
end
u = redis.call('HINCRBY', KEYS[1], 'u', nu)
t = redis.call('HINCRBY', KEYS[1], 't', nt)
redis.call('HINCRBY', KEYS[1], 'iu', nu)
redis.call('HINCRBY', KEYS[1], 'it', nt)
if tonumber(ARGV[6]) > 0 then redis.call('EXPIREAT', KEYS[1], ARGV[6]) end
return {1, u, t}
"""

_ADJUST_LUA = """
redis.call('HINCRBY', KEYS[1], 'u', ARGV[1])
redis.call('HINCRBY', KEYS[1], 't', ARGV[2])
redis.call('HINCRBY', KEYS[1], 'iu', ARGV[3])
redis.call('HINCRBY', KEYS[1], 'it', ARGV[4])
if tonumber(ARGV[5]) > 0 then redis.call('EXPIREAT', KEYS[1], ARGV[5]) end
return 1
"""


class RedisBudgetStore:
    """Counters in Redis hashes (``<prefix><key>`` with fields ``u``, ``t``,
    ``iu``, ``it``). Reserve and adjust are Lua scripts, so each is one
    atomic step on the server. Needs ``pip install redis``.

    Args:
        client: A ``redis.Redis`` client, or a URL (``redis://host:6379/0``).
        prefix: Key prefix (default ``briefcase:budget:``).

    Lua numbers are doubles: counters are exact up to 2**53 nano-USD
    (about 9 million USD) per key and window.
    """

    def __init__(self, client: Any, prefix: str = "briefcase:budget:") -> None:
        if isinstance(client, str):
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - depends on the env
                raise ImportError("RedisBudgetStore needs the redis package: "
                                  "pip install redis (or 'briefcase-ai[budgets-redis]')") from exc
            client = redis.Redis.from_url(client)
        self.client = client
        self.prefix = prefix
        self._reserve = client.register_script(_RESERVE_LUA)
        self._adjust = client.register_script(_ADJUST_LUA)

    def reserve(self, key: str, nano: int, tokens: int, limit_nano: Optional[int],
                limit_tokens: Optional[int], expires_at: Optional[int],
                force: bool = False) -> Tuple[bool, int, int]:
        ok, used_n, used_t = self._reserve(
            keys=[self.prefix + key],
            args=[int(nano), int(tokens), -1 if limit_nano is None else int(limit_nano),
                  -1 if limit_tokens is None else int(limit_tokens), "1" if force else "0",
                  int(expires_at or 0)])
        return bool(int(ok)), int(used_n), int(used_t)

    def adjust(self, key: str, d_nano: int, d_tokens: int, d_inflight_nano: int,
               d_inflight_tokens: int, expires_at: Optional[int]) -> None:
        self._adjust(keys=[self.prefix + key],
                     args=[int(d_nano), int(d_tokens), int(d_inflight_nano),
                           int(d_inflight_tokens), int(expires_at or 0)])

    def get(self, key: str) -> Counter:
        values = self.client.hmget(self.prefix + key, "u", "t", "iu", "it")
        return tuple(int(v or 0) for v in values)  # type: ignore[return-value]

    def scan(self, prefix: str) -> List[Tuple[str, Counter]]:
        out = []
        for raw in self.client.scan_iter(match=_glob_escape(self.prefix + prefix) + "*"):
            name = raw.decode() if isinstance(raw, bytes) else str(raw)
            key = name[len(self.prefix):]
            out.append((key, self.get(key)))
        return sorted(out)

    def reset(self, prefix: str = "") -> int:
        keys = list(self.client.scan_iter(match=_glob_escape(self.prefix + prefix) + "*"))
        if keys:
            self.client.delete(*keys)
        return len(keys)

    def describe(self) -> str:
        try:
            kwargs = self.client.connection_pool.connection_kwargs
            return f"redis {kwargs.get('host', '?')}:{kwargs.get('port', '?')}/{kwargs.get('db', 0)}"
        except Exception:
            return "redis"


def _glob_escape(text: str) -> str:
    return "".join("\\" + c if c in "*?[]\\" else c for c in text)


# -- SQL ----------------------------------------------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS {table} (
    budget_key VARCHAR(255) NOT NULL PRIMARY KEY,
    used_nano BIGINT NOT NULL DEFAULT 0,
    used_tokens BIGINT NOT NULL DEFAULT 0,
    inflight_nano BIGINT NOT NULL DEFAULT 0,
    inflight_tokens BIGINT NOT NULL DEFAULT 0,
    expires_at BIGINT NULL
)"""

_INSERT = {
    "sqlite": "INSERT OR IGNORE INTO {table} (budget_key) VALUES (?)",
    "postgresql": "INSERT INTO {table} (budget_key) VALUES (?) ON CONFLICT (budget_key) DO NOTHING",
    "mysql": "INSERT IGNORE INTO {table} (budget_key) VALUES (?)",
}


class SQLBudgetStore:
    """Counters in one SQL table (schema in :data:`SCHEMA`), through plain
    DB-API connections.

    A reservation is one conditional statement::

        UPDATE briefcase_budget_usage
           SET used_nano = used_nano + :n, ...
         WHERE budget_key = :key AND used_nano + :n <= :limit ...

    which takes the row lock and re-checks the condition against the latest
    committed row (PostgreSQL and MySQL InnoDB at READ COMMITTED or higher;
    SQLite serialises writers), so concurrent reservations can never push a
    counter past its limit. A missing row is created with an insert-if-absent
    in its own short transaction, and a transaction that hits a deadlock,
    lock timeout or serialisation failure is rolled back and retried (up to
    8 times, with backoff).

    Args:
        connect: ``() -> DB-API connection``. Called once per thread (and
            again in a forked child).
        dialect: ``"sqlite"``, ``"postgresql"`` or ``"mysql"``.
        table: Table name (default ``briefcase_budget_usage``).
        create: Create the table if it does not exist (default True).

    Use :func:`store_from_url` for ``sqlite:///path``, ``postgresql://...``
    (psycopg 3 or psycopg2) and ``mysql://...`` (PyMySQL), or pass a
    SQLAlchemy ``Engine`` as ``connect`` (its ``raw_connection`` is used).
    """

    def __init__(self, connect: Any, dialect: Optional[str] = None,
                 table: str = "briefcase_budget_usage", create: bool = True) -> None:
        if hasattr(connect, "raw_connection") and hasattr(connect, "dialect"):
            name = connect.dialect.name
            dialect = dialect or {"postgres": "postgresql"}.get(name, name)
            connect = connect.raw_connection
        if dialect not in _INSERT:
            raise ValueError(f"dialect must be one of {sorted(_INSERT)}, got {dialect!r}")
        if not table.replace("_", "").isalnum():
            raise ValueError(f"table name must be alphanumeric/underscore, got {table!r}")
        self._connect = connect
        self.dialect = dialect
        self.table = table
        self._local = threading.local()
        self._paramstyle: Optional[str] = None
        if create:
            self._execute_commit(SCHEMA.format(table=table), ())

    # -- connections ------------------------------------------------------------

    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(self._local, "pid", None) != os.getpid():
            conn = self._connect()
            self._local.conn = conn
            self._local.pid = os.getpid()
            if self._paramstyle is None:
                raw = getattr(conn, "dbapi_connection", None) or conn  # SQLAlchemy pool proxy
                module = __import__(type(raw).__module__.split(".")[0])
                self._paramstyle = getattr(module, "paramstyle", "qmark")
        return conn

    def _sql(self, sql: str) -> str:
        sql = sql.format(table=self.table)
        if self._paramstyle in ("format", "pyformat"):
            sql = sql.replace("%", "%%").replace("?", "%s")
        return sql

    def _execute_commit(self, sql: str, params: Iterable[Any]) -> None:
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute(self._sql(sql), tuple(params))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # -- operations -----------------------------------------------------------------

    def _retry(self, operation: Callable[[Any, Any], Any]) -> Any:
        """Run ``operation(conn, cursor)`` in a transaction; retry it on a
        deadlock / serialisation failure / busy database (the statements are
        idempotent per attempt because a failed attempt is rolled back)."""
        delay = 0.005
        for attempt in range(8):
            conn = self._conn()
            cur = conn.cursor()
            try:
                result = operation(conn, cur)
                conn.commit()
                return result
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt == 7 or not _transient(exc):
                    raise
            finally:
                cur.close()
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _ensure_row(self, key: str) -> None:
        """Create the counter row in its own short transaction (so the
        insert's locks are not held while the counter is updated)."""
        def insert(conn: Any, cur: Any) -> None:
            cur.execute(self._sql(_INSERT[self.dialect]), (key,))

        self._retry(insert)

    def reserve(self, key: str, nano: int, tokens: int, limit_nano: Optional[int],
                limit_tokens: Optional[int], expires_at: Optional[int],
                force: bool = False) -> Tuple[bool, int, int]:
        where = ["budget_key = ?"]
        params: List[Any] = [nano, tokens, nano, tokens, expires_at, key]
        if not force and limit_nano is not None:
            where.append("used_nano + ? <= ?")
            params += [nano, limit_nano]
        if not force and limit_tokens is not None:
            where.append("used_tokens + ? <= ?")
            params += [tokens, limit_tokens]
        update = self._sql(
            "UPDATE {table} SET used_nano = used_nano + ?, used_tokens = used_tokens + ?, "
            "inflight_nano = inflight_nano + ?, inflight_tokens = inflight_tokens + ?, "
            "expires_at = ? WHERE " + " AND ".join(where))
        select = self._sql("SELECT used_nano, used_tokens FROM {table} WHERE budget_key = ?")

        def attempt(conn: Any, cur: Any) -> Optional[Tuple[bool, int, int]]:
            cur.execute(update, tuple(params))
            ok = cur.rowcount == 1
            cur.execute(select, (key,))
            row = cur.fetchone()
            if row is None:
                return None
            return ok, int(row[0]), int(row[1])

        for _ in range(3):
            result = self._retry(attempt)
            if result is not None:
                return result
            self._ensure_row(key)
        raise RuntimeError(f"budget counter row {key!r} could not be created")

    def adjust(self, key: str, d_nano: int, d_tokens: int, d_inflight_nano: int,
               d_inflight_tokens: int, expires_at: Optional[int]) -> None:
        update = self._sql(
            "UPDATE {table} SET used_nano = used_nano + ?, used_tokens = used_tokens + ?, "
            "inflight_nano = inflight_nano + ?, inflight_tokens = inflight_tokens + ?, "
            "expires_at = ? WHERE budget_key = ?")
        params = (d_nano, d_tokens, d_inflight_nano, d_inflight_tokens, expires_at, key)

        def attempt(conn: Any, cur: Any) -> bool:
            cur.execute(update, params)
            return cur.rowcount == 1

        for _ in range(3):
            if self._retry(attempt):
                return
            self._ensure_row(key)
        raise RuntimeError(f"budget counter row {key!r} could not be created")

    def get(self, key: str) -> Counter:
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute(self._sql("SELECT used_nano, used_tokens, inflight_nano, inflight_tokens "
                                  "FROM {table} WHERE budget_key = ?"), (key,))
            row = cur.fetchone()
            conn.commit()
        finally:
            cur.close()
        return tuple(int(v) for v in row) if row else (0, 0, 0, 0)  # type: ignore[return-value]

    def scan(self, prefix: str) -> List[Tuple[str, Counter]]:
        conn = self._conn()
        cur = conn.cursor()
        escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        try:
            cur.execute(self._sql("SELECT budget_key, used_nano, used_tokens, inflight_nano, "
                                  "inflight_tokens FROM {table} WHERE budget_key LIKE ? ESCAPE '!' "
                                  "AND (expires_at IS NULL OR expires_at > ?) ORDER BY budget_key"),
                        (escaped + "%", int(time.time())))
            rows = cur.fetchall()
            conn.commit()
        finally:
            cur.close()
        return [(str(r[0]), (int(r[1]), int(r[2]), int(r[3]), int(r[4]))) for r in rows]

    def reset(self, prefix: str = "") -> int:
        conn = self._conn()
        cur = conn.cursor()
        escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        try:
            cur.execute(self._sql("DELETE FROM {table} WHERE budget_key LIKE ? ESCAPE '!'"),
                        (escaped + "%",))
            count = cur.rowcount
            conn.commit()
        finally:
            cur.close()
        return int(count)

    def describe(self) -> str:
        return f"sql ({self.dialect}, table {self.table})"


def _transient(exc: BaseException) -> bool:
    """A deadlock, lock wait timeout, serialisation failure or busy SQLite
    database: worth retrying."""
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if code in ("40001", "40P01", "55P03"):
        return True
    args = getattr(exc, "args", ())
    if args and args[0] in (1205, 1213):  # MySQL lock wait timeout / deadlock
        return True
    return "database is locked" in str(exc) or "database table is locked" in str(exc)


def store_from_url(url: str, timeout: Optional[float] = None) -> Any:
    """A store from a URL: ``memory``, ``redis://...`` / ``rediss://...``,
    ``sqlite:///absolute/path.db`` (``~`` is expanded), ``postgresql://...``
    or ``mysql://user:password@host:port/database``. ``timeout`` bounds
    connecting and each command (seconds), where the driver supports it."""
    url = url.strip()
    if url in ("", "memory"):
        return MemoryBudgetStore()
    if url.startswith(("redis://", "rediss://", "unix://")):
        if timeout is None:
            return RedisBudgetStore(url)
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise ImportError("RedisBudgetStore needs the redis package: "
                              "pip install redis (or 'briefcase-ai[budgets-redis]')") from exc
        return RedisBudgetStore(redis.Redis.from_url(url, socket_timeout=timeout,
                                                     socket_connect_timeout=timeout))
    if url.startswith("sqlite:///"):
        import sqlite3

        path = os.path.expanduser(url[len("sqlite:///"):])
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        def connect_sqlite() -> Any:
            return sqlite3.connect(path, timeout=timeout or 30)

        return SQLBudgetStore(connect_sqlite, "sqlite")
    if url.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg

            def connect_pg() -> Any:
                return psycopg.connect(url)
        except ImportError:
            try:
                import psycopg2  # type: ignore[import-untyped,import-not-found,unused-ignore]

                def connect_pg() -> Any:
                    return psycopg2.connect(url)
            except ImportError as exc:
                raise ImportError("a postgresql:// budget store needs psycopg: "
                                  "pip install 'psycopg[binary]'") from exc
        return SQLBudgetStore(connect_pg, "postgresql")
    if url.startswith("mysql://"):
        try:
            import pymysql  # type: ignore[import-untyped,import-not-found,unused-ignore]
            from pymysql.constants import CLIENT  # type: ignore[import-untyped,import-not-found,unused-ignore]
        except ImportError as exc:
            raise ImportError("a mysql:// budget store needs PyMySQL: pip install pymysql") from exc
        from urllib.parse import unquote, urlparse

        parts = urlparse(url)

        def connect_mysql() -> Any:
            return pymysql.connect(host=parts.hostname or "localhost", port=parts.port or 3306,
                                   user=unquote(parts.username or ""),
                                   password=unquote(parts.password or ""),
                                   database=(parts.path or "/").lstrip("/") or None,
                                   autocommit=False,
                                   # rowcount = rows matched, not rows changed
                                   client_flag=CLIENT.FOUND_ROWS)

        return SQLBudgetStore(connect_mysql, "mysql")
    raise ValueError(f"unknown budget store URL {url!r}: use memory, redis://, sqlite:///, "
                     "postgresql:// or mysql://")


__all__ = ["MemoryBudgetStore", "RedisBudgetStore", "SCHEMA", "SQLBudgetStore", "store_from_url"]
