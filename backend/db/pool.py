"""SurrealDB connection singleton (DBPool) + timed query proxy.

Depends on db._patch being imported first (the _recv_task monkeypatch must be
installed before any get_db() — enforced by db.__init__ import order).
"""

from __future__ import annotations

import asyncio
import logging
import time

from query_stats import record_query
from settings_registry import ConfigError
from surrealdb import AsyncSurreal

import config

logger = logging.getLogger("db")

# ARCH: Singleton AsyncSurreal — one connection per process, initialized on first call.
# ARCH: Auto-reconnect with bounded liveness probe every _CHECK_INTERVAL seconds.
#       Probe wrapped in asyncio.wait_for(timeout=2.0) so a hung SDK reader can't
#       wedge the pool. Double-check locking (asyncio.Lock) prevents concurrent
#       reconnect races. During startup, get_db() retries with exponential backoff
#       so the backend survives SurrealDB not being ready yet (VM reboot race).
# ARCH: DBPool class encapsulates all connection state — no module-level globals to forget.

_CHECK_INTERVAL = 30.0
_STARTUP_MAX_WAIT = 60.0
_STARTUP_BACKOFF_BASE = 0.5

# ARCH: SurrealDB resolves two concurrent writes to the same record by failing one
# with a conflict it explicitly marks retryable; the loser committed NOTHING, so
# re-issuing the single autocommit statement is safe and is the only way the
# caller gets the real outcome instead of a 500. Retrying at the proxy covers
# every write path (proposal claims, collab flush, ydoc append/compact) rather
# than one call site at a time. Measured on the live dev DB: a two-way race on
# one row raised the conflict in 3 of 200 attempts.
DB_WRITE_CONFLICT_ATTEMPTS = 4
_WRITE_CONFLICT_BACKOFF_S = 0.02


def _is_write_conflict(exc: BaseException) -> bool:
    """True for the retryable transaction-write-conflict SurrealDB reports.

    Matched on the message: the SDK raises a generic QueryError for every server
    error and carries no conflict-specific class or code to key on.
    """
    from surrealdb.errors import QueryError

    return isinstance(exc, QueryError) and "write conflict" in str(exc).lower()


def surreal_password() -> str:
    """The database password: the file secrets-init generated
    (config.SURREAL_PASS_FILE — a wiring constant, no env leg).

    Read on every connect; a missing or empty file raises ConfigError — the
    backend refuses to sign in with a blank password rather than lock itself
    out of its own database with a silent mismatch.
    """
    path = config.SURREAL_PASS_FILE
    password = path.read_text().strip() if path.is_file() else ""
    if not password:
        raise ConfigError(
            f"the database password file {path} is missing or empty — "
            "secrets-init generates it into the secrets volume on every `up`"
        )
    return password


async def _connect() -> AsyncSurreal:
    """Create a new SurrealDB connection, authenticate, and select namespace."""
    conn = AsyncSurreal(config.SURREAL_URL)
    try:
        await conn.signin({"username": config.SURREAL_USER, "password": surreal_password()})
        await conn.use(config.SURREAL_NS, config.SURREAL_DB)
    except Exception:
        await _close(conn)
        raise
    return conn


async def _close(conn: AsyncSurreal) -> None:
    """Best-effort close of a SurrealDB connection."""
    try:
        await conn.close()
    except Exception:
        logger.warning("SurrealDB connection close failed", exc_info=True)


class DBPool:
    """Encapsulated SurrealDB connection singleton with auto-reconnect.

    # WHY: DBPool class encapsulates all connection state — no module-level globals to forget.
    # INVARIANT: _startup_complete switches from retry-with-backoff to limited-retry mode.  Why: before startup the pool retries aggressively to survive a cold-boot race; after, it backs off so a later DB outage doesn't hammer it.
    """

    _instance: DBPool | None = None

    def __new__(cls) -> DBPool:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._db = None
            cls._instance._db_lock = asyncio.Lock()
            cls._instance._last_check_ts = 0.0
            cls._instance._startup_complete = False
        return cls._instance

    async def get_db(self) -> AsyncSurreal:
        """Return the shared SurrealDB connection, reconnecting if needed."""
        if self._db is not None and (time.monotonic() - self._last_check_ts) < _CHECK_INTERVAL:
            return self._db

        async with self._db_lock:
            if self._db is not None and (time.monotonic() - self._last_check_ts) < _CHECK_INTERVAL:
                return self._db

            if self._db is not None:
                try:
                    # WHY: bound the probe — if the SDK's _recv_task ever dies for any
                    # reason, query() hangs forever and the pool can't recover.
                    await asyncio.wait_for(self._db.query("RETURN 1"), timeout=2.0)
                    self._last_check_ts = time.monotonic()
                    return self._db
                except Exception:
                    logger.warning("SurrealDB liveness probe failed — reconnecting")
                    await _close(self._db)
                    self._db = None

            if not self._startup_complete:
                elapsed = 0.0
                delay = _STARTUP_BACKOFF_BASE
                while True:
                    try:
                        self._db = await _connect()
                        self._last_check_ts = time.monotonic()
                        self._startup_complete = True
                        logger.info("SurrealDB connected")
                        return self._db
                    except Exception:
                        elapsed += delay
                        if elapsed > _STARTUP_MAX_WAIT:
                            logger.error("SurrealDB unavailable after %.0fs — giving up", elapsed)
                            raise
                        logger.warning("SurrealDB not ready, retrying in %.1fs…", delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 8.0)
            else:
                delay = 1.0
                for attempt in range(3):
                    try:
                        self._db = await _connect()
                        self._last_check_ts = time.monotonic()
                        logger.info("SurrealDB reconnected (attempt %d)", attempt + 1)
                        return self._db
                    except Exception:
                        if attempt == 2:
                            logger.error("SurrealDB reconnect failed after 3 attempts")
                            raise
                        logger.warning("SurrealDB reconnect attempt %d failed, retrying in %.0fs", attempt + 1, delay)
                        await asyncio.sleep(delay)
                        delay *= 2

    async def reset_db(self) -> None:
        """Force-close the current connection so the next get_db() reconnects."""
        async with self._db_lock:
            if self._db is not None:
                await _close(self._db)
                self._db = None
                self._last_check_ts = 0.0
                logger.warning("SurrealDB connection reset — will reconnect on next request")

    def mark_startup_complete(self) -> None:
        """Switch get_db() from startup-retry mode to single-attempt mode."""
        self._startup_complete = True


_pool = DBPool()

# ARCH: one _TimedDB proxy per live connection — get_db() returns the SAME instance
# across calls (the underlying conn is a singleton), so per-instance instrumentation
# (tests patch db.query) and the cached timing state stay consistent. Re-created only
# when the pool reconnects (new conn identity).
_timed_proxy: _TimedDB | None = None
_timed_proxy_conn_id: int | None = None


def get_timed_proxy(conn) -> _TimedDB:
    """Return the cached timed proxy for `conn`, creating/replacing it on reconnect."""
    global _timed_proxy, _timed_proxy_conn_id
    cid = id(conn)
    if _timed_proxy is None or _timed_proxy_conn_id != cid:
        _timed_proxy = _TimedDB(conn)
        _timed_proxy_conn_id = cid
    return _timed_proxy


class _TimedDB:
    """Transparent proxy over AsyncSurreal that records per-site query timing.

    get_db() returns this wrapper so every db.query()/query_raw() call is timed
    automatically under a `site` label (default "unspecified"); hot call sites pass
    site="..." for granular attribution (collab flush, ydoc append/compact, access
    checks, chat). Non-query attributes delegate to the real connection. See
    query_stats.

    # WHY: proxy chosen over a per-call-site helper so all 100+ db.query() callers
    #       are covered without churn; the optional keyword-only `site` adds labels
    #       only where attribution matters. No stack inspection (too slow for hot path).
    # NOTE: no __slots__ — tests instrument db.query per-instance (monkeypatch), which
    #       __slots__ would make read-only. Instance __dict__ shadows the class method.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def _run_retrying(self, call, sql, params, site):
        """Time one statement and re-issue it while SurrealDB reports a write conflict.

        # INVARIANT: ONLY the retryable write conflict is re-issued, and only up to
        # DB_WRITE_CONFLICT_ATTEMPTS times. Why: the conflict guarantees the
        # transaction committed nothing, so the statement is safe to repeat; no
        # other error carries that guarantee, and repeating a non-idempotent
        # statement on a guess would double a write. The cap keeps a permanently
        # contended row from pinning a request forever.
        """
        for attempt in range(DB_WRITE_CONFLICT_ATTEMPTS):
            start = time.perf_counter()
            try:
                return await call(sql, params)
            except Exception as e:
                if not _is_write_conflict(e) or attempt == DB_WRITE_CONFLICT_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "Write conflict on site=%s (attempt %d/%d) — retrying",
                    site, attempt + 1, DB_WRITE_CONFLICT_ATTEMPTS,
                )
            finally:
                record_query(site, (time.perf_counter() - start) * 1000.0)
            await asyncio.sleep(_WRITE_CONFLICT_BACKOFF_S * (attempt + 1))

    async def query(self, sql, params=None, *, site="unspecified"):
        return await self._run_retrying(self._conn.query, sql, params, site)

    async def query_raw(self, sql, params=None, *, site="unspecified"):
        return await self._run_retrying(self._conn.query_raw, sql, params, site)


async def get_db() -> AsyncSurreal:
    """Return the shared SurrealDB connection (timed proxy), reconnecting if needed."""
    conn = await _pool.get_db()
    return get_timed_proxy(conn)


async def reset_db() -> None:
    """Force-close the current connection so the next get_db() reconnects."""
    await _pool.reset_db()


def mark_startup_complete() -> None:
    """Switch get_db() from startup-retry mode to single-attempt mode."""
    _pool.mark_startup_complete()
