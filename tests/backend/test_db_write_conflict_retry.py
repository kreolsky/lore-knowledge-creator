"""Retry of SurrealDB's retryable write conflict at the db proxy.

SurrealDB answers two concurrent writes touching the same record by failing one
with `Transaction conflict: Transaction write conflict. This transaction can be
retried`. The failing transaction committed NOTHING, so the statement is safe to
re-issue — measured against the live dev DB, the conditional proposal claim hit
it in 3 of 200 two-way races and surfaced as a 500 to the losing caller instead
of the clean "already claimed" outcome.

These tests bind the retry contract on _TimedDB.query / query_raw: retry the
conflict, never anything else, and give up after a bounded number of attempts.
"""
from __future__ import annotations

import pytest
from surrealdb.errors import QueryError

import db.pool as db_pool

_CONFLICT = (
    "Transaction conflict: Transaction write conflict. This transaction can be retried"
)


class _Conn:
    """AsyncSurreal stand-in: fails the first `fail_times` calls, then succeeds."""

    def __init__(self, exc: Exception, fail_times: int) -> None:
        self._exc, self._left = exc, fail_times
        self.calls = 0

    async def query(self, sql, params=None):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise self._exc
        return [{"id": "row:1"}]

    async def query_raw(self, sql, params=None):
        return await self.query(sql, params)


@pytest.mark.asyncio
async def test_query_retries_the_retryable_write_conflict():
    """One conflict → the statement is re-issued and the caller sees the result,
    not the exception."""
    conn = _Conn(QueryError(kind="Query", message=_CONFLICT), fail_times=1)
    proxy = db_pool._TimedDB(conn)

    assert await proxy.query("UPDATE x SET y = 1") == [{"id": "row:1"}]
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_query_raw_retries_too():
    """query_raw carries the same contract — the raw path issues the same writes."""
    conn = _Conn(QueryError(kind="Query", message=_CONFLICT), fail_times=1)
    proxy = db_pool._TimedDB(conn)

    assert await proxy.query_raw("UPDATE x SET y = 1") == [{"id": "row:1"}]
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_conflict_surfaces():
    """A conflict that never clears is raised, not retried forever — the caller
    must still be able to fail. Attempts are capped by DB_WRITE_CONFLICT_ATTEMPTS."""
    conn = _Conn(QueryError(kind="Query", message=_CONFLICT), fail_times=99)
    proxy = db_pool._TimedDB(conn)

    with pytest.raises(QueryError):
        await proxy.query("UPDATE x SET y = 1")
    assert conn.calls == db_pool.DB_WRITE_CONFLICT_ATTEMPTS


@pytest.mark.asyncio
async def test_other_query_errors_are_not_retried():
    """Only the conflict SurrealDB marks retryable is re-issued. A parse/permission
    error re-runs nothing: retrying it would multiply a failure that cannot clear.

    INVARIANT: a non-conflict error propagates from the FIRST attempt. Why: the
    retry is justified solely by the conflict's "nothing committed, can be
    retried" semantics — no other error carries that guarantee, and a
    non-idempotent statement must not run twice on a guess.
    """
    conn = _Conn(QueryError(kind="Query", message="Parse error: unexpected token"), fail_times=99)
    proxy = db_pool._TimedDB(conn)

    with pytest.raises(QueryError):
        await proxy.query("UPDATE x SET y = 1")
    assert conn.calls == 1


@pytest.mark.asyncio
async def test_non_query_exceptions_are_not_retried():
    """A transport/other exception is not a conflict — propagates untouched."""
    conn = _Conn(RuntimeError("socket closed"), fail_times=99)
    proxy = db_pool._TimedDB(conn)

    with pytest.raises(RuntimeError):
        await proxy.query("UPDATE x SET y = 1")
    assert conn.calls == 1


# ─── Reader-death re-issue (opt-in via idempotent=True) ──────────────────────

_READER_DEATH = "SurrealDB WS reader exited; this query's delivery is unknown"


@pytest.mark.asyncio
async def test_reader_death_reissued_when_idempotent():
    """An idempotent-marked statement survives a reader death: the re-issue rides
    the same connection object, which self-heals inside _send (db/_patch.py)."""
    from db._patch import SurrealReaderDiedError

    conn = _Conn(SurrealReaderDiedError(_READER_DEATH), fail_times=1)
    proxy = db_pool._TimedDB(conn)

    assert await proxy.query("UPDATE x SET y = 1", idempotent=True) == [{"id": "row:1"}]
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_reader_death_query_raw_reissued_when_idempotent():
    """query_raw carries the same opt-in contract."""
    from db._patch import SurrealReaderDiedError

    conn = _Conn(SurrealReaderDiedError(_READER_DEATH), fail_times=1)
    proxy = db_pool._TimedDB(conn)

    assert await proxy.query_raw("UPDATE x SET y = 1", idempotent=True) == [{"id": "row:1"}]
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_reader_death_not_retried_by_default():
    """Default stays conflict-only: without the caller's idempotent declaration the
    reader-death error propagates from the FIRST attempt (the statement may have
    executed — re-issuing on a guess could double a non-idempotent write)."""
    from db._patch import SurrealReaderDiedError

    conn = _Conn(SurrealReaderDiedError(_READER_DEATH), fail_times=99)
    proxy = db_pool._TimedDB(conn)

    with pytest.raises(SurrealReaderDiedError):
        await proxy.query("UPDATE x SET y = 1")
    assert conn.calls == 1


@pytest.mark.asyncio
async def test_reader_death_retries_are_bounded():
    """A reader that keeps dying (permanently broken connection) is raised, not
    retried forever — same cap as the write conflict."""
    from db._patch import SurrealReaderDiedError

    conn = _Conn(SurrealReaderDiedError(_READER_DEATH), fail_times=99)
    proxy = db_pool._TimedDB(conn)

    with pytest.raises(SurrealReaderDiedError):
        await proxy.query("UPDATE x SET y = 1", idempotent=True)
    assert conn.calls == db_pool.DB_WRITE_CONFLICT_ATTEMPTS


@pytest.mark.asyncio
async def test_transport_loss_reissued_when_idempotent():
    """A dial refused during a SurrealDB blip is the same uncertain-delivery class
    as reader death — re-issued only under the caller's idempotent declaration."""
    conn = _Conn(ConnectionRefusedError("connection refused"), fail_times=1)
    proxy = db_pool._TimedDB(conn)

    assert await proxy.query("UPDATE x SET y = 1", idempotent=True) == [{"id": "row:1"}]
    assert conn.calls == 2


@pytest.mark.asyncio
async def test_transport_loss_not_retried_by_default():
    """Without idempotent=True a transport loss propagates from the first attempt,
    exactly as before the opt-in existed."""
    conn = _Conn(ConnectionRefusedError("connection refused"), fail_times=99)
    proxy = db_pool._TimedDB(conn)

    with pytest.raises(ConnectionRefusedError):
        await proxy.query("UPDATE x SET y = 1")
    assert conn.calls == 1
