"""SurrealDB connection singleton (DBPool) + timed query proxy."""

from __future__ import annotations

import asyncio
import logging
import time

from query_stats import record_query
from settings_registry import ConfigError
from surrealdb import AsyncSurreal
from surrealdb.errors import ConnectionUnavailableError

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
# Uncertain-delivery failures (reader death / transport loss) re-issue on a SLOWER
# ramp — a dial refused during a SurrealDB restart blip clears in seconds, not
# milliseconds; 4 attempts at a 2s ramp keep re-dialing across ~12s of downtime
# (measured on gray: a `docker compose restart surreal` is back healthy in ~7s).
_UNCERTAIN_BACKOFF_S = 2.0


class SurrealReaderDiedError(RuntimeError):
    """The connection dropped with this statement in flight — delivery is UNKNOWN.

    # WHY: raised by _TimedDB in place of the SDK's ConnectionUnavailableError
    # (surrealdb-py 3 fails every pending request with it when the socket or its
    # reader stops, and raises it when a send or a dial fails), so the retry set
    # and its callers key on one name of ours, not on an SDK class.
    """


def _is_write_conflict(exc: BaseException) -> bool:
    """True for the retryable transaction-write-conflict SurrealDB reports.

    Keyed on the structured kind surrealdb-py 3 parses (QueryError
    .is_transaction_conflict) and, failing that, on the message — a server that
    sends no structured detail reaches us as a plain ServerError.
    """
    from surrealdb.errors import QueryError, ServerError

    if isinstance(exc, QueryError) and exc.is_transaction_conflict:
        return True
    return isinstance(exc, ServerError) and "write conflict" in str(exc).lower()


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
            "secrets-init generates it into the secrets volume on every `up`; "
            "a docker-compose.yml older than v0.21.0 has no secrets-init — download "
            "it again (README → Update) and run `docker compose up -d --build`"
        )
    return password


def surreal_credentials() -> dict[str, str]:
    """Root signin credentials, the password re-read from its file on every call."""
    return {"username": config.SURREAL_USER, "password": surreal_password()}


def open_surreal(url: str) -> AsyncSurreal:
    """An unopened SurrealDB connection with the options every Lore connection uses."""
    # WHY timeout=None, streaming=False: surrealdb-py 2.0 had no per-request
    # timeout and buffered every answer; 3.0 defaults to a 30 s cap (long
    # migrations and backfills exceed it) and to streamed answers.
    # INVARIANT: credentials go in as `auth=` and nothing calls signin() on the
    # connection. Why: surrealdb-py 3 re-signs in with `auth=` after every
    # reconnect; an explicit signin() takes over from it and the reconnect then
    # replays the 1-hour session token, so a connection that dropped after an
    # hour comes back unauthenticated.
    return AsyncSurreal(url, auth=surreal_credentials, timeout=None, streaming=False)


async def _connect() -> AsyncSurreal:
    """Create a new SurrealDB connection, authenticate, and select namespace."""
    conn = open_surreal(config.SURREAL_URL)
    try:
        await conn.use(config.SURREAL_NS, config.SURREAL_DB)
    except Exception:
        await _close(conn)
        raise
    # WHY: surrealdb-py 3 reconnects and replays the session inside the
    # connection and logs it only at DEBUG, so the pool's probe never sees a
    # drop; these listeners keep a restarted engine visible in the backend log.
    # Subscribed after use(), so "connected" fires only on a reconnect.
    conn.subscribe("reconnecting", lambda: logger.warning("SurrealDB connection lost — reconnecting"))
    conn.subscribe("connected", lambda version: logger.info("SurrealDB reconnected (server %s)", version))
    conn.subscribe("disconnected", lambda: logger.error("SurrealDB connection dropped — reconnect gave up"))
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
                    # WHY: bound the probe so a wedged connection can never pin the
                    # pool lock; the SDK's own request timeout is off (open_surreal).
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
                    # WHY: a ConfigError is the install's wiring, not a DB still
                    # booting — retrying it only hides the cause behind "not ready".
                    except ConfigError:
                        raise
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
                    except ConfigError:
                        raise
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

    async def _run_retrying(self, call, sql, params, site, *, idempotent=False):
        """Time one statement and re-issue it while SurrealDB reports a write conflict.

        # INVARIANT: ONLY the retryable write conflict is re-issued, and only up to
        # DB_WRITE_CONFLICT_ATTEMPTS times. Why: the conflict guarantees the
        # transaction committed nothing, so the statement is safe to repeat; no
        # other error carries that guarantee, and repeating a non-idempotent
        # statement on a guess would double a write. The cap keeps a permanently
        # contended row from pinning a request forever.
        #
        # The ONE opt-in extension: with idempotent=True the caller declares the
        # statement safe to re-issue even when its delivery is UNCERTAIN —
        # SurrealReaderDiedError (the socket or its reader died mid-query, or a
        # dial was refused during a SurrealDB blip) then joins the retry set. The
        # re-issue rides the same connection object, which the SDK reconnects and
        # re-authenticates itself (open_surreal). These
        # failures carry NO committed-nothing guarantee, so default OFF: a retried
        # CREATE with a lost response would duplicate a row.
        """
        for attempt in range(DB_WRITE_CONFLICT_ATTEMPTS):
            start = time.perf_counter()
            backoff = _WRITE_CONFLICT_BACKOFF_S
            try:
                try:
                    return await call(sql, params)
                except ConnectionUnavailableError as lost:
                    raise SurrealReaderDiedError(str(lost)) from lost
            except Exception as e:
                conflict = _is_write_conflict(e)
                uncertain = isinstance(e, SurrealReaderDiedError)
                retryable = conflict or (idempotent and uncertain)
                if not retryable or attempt == DB_WRITE_CONFLICT_ATTEMPTS - 1:
                    raise
                if not conflict:
                    backoff = _UNCERTAIN_BACKOFF_S
                logger.warning(
                    "%s on site=%s (attempt %d/%d) — re-issuing",
                    "Write conflict" if conflict else "uncertain delivery (transport loss)",
                    site, attempt + 1, DB_WRITE_CONFLICT_ATTEMPTS,
                )
            finally:
                record_query(site, (time.perf_counter() - start) * 1000.0)
            await asyncio.sleep(backoff * (attempt + 1))

    async def query(self, sql, params=None, *, site="unspecified", idempotent=False):
        # WHY: surrealdb-py 3 answers one result PER STATEMENT; every call site
        # reads the 2.x shape, the first statement's result.
        results = await self._run_retrying(
            self._conn.query, sql, params, site, idempotent=idempotent
        )
        return results[0] if results else None

    async def query_raw(self, sql, params=None, *, site="unspecified", idempotent=False):
        return await self._run_retrying(
            self._conn.query_raw, sql, params, site, idempotent=idempotent
        )


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
