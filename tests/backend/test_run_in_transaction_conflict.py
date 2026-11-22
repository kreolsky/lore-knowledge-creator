"""run_in_transaction re-issues a transaction SurrealDB failed with a write conflict.

On SurrealDB v4 concurrent BEGIN/COMMIT blocks touching one record conflict on most
rounds; the conflict arrives on the COMMIT entry of the query_raw envelope
(details.kind TransactionConflict), which never raises, so the proxy-level retry
cannot see it.
"""
from __future__ import annotations

import asyncio

import pytest

from db import run_in_transaction
from db.pool import DB_WRITE_CONFLICT_ATTEMPTS

# WHY: a transaction loses a conflict only to a sibling that committed, so with N racers
# it can lose N-1 times; more racers than attempts makes the test fail by construction.
_RACERS = DB_WRITE_CONFLICT_ATTEMPTS


# TEST-WHY: cross-process-contract — a v4 write conflict inside BEGIN/COMMIT is re-issued, not raised
@pytest.mark.asyncio
async def test_concurrent_transactions_on_one_row_all_commit(test_db):
    await test_db.query("UPSERT txn_conflict_probe:one SET n = 0")
    try:
        await asyncio.gather(*(
            run_in_transaction(test_db, ["UPDATE txn_conflict_probe:one SET n += 1"], {})
            for _ in range(_RACERS)
        ))
        assert await test_db.query("SELECT VALUE n FROM txn_conflict_probe:one") == [_RACERS]
    finally:
        await test_db.query("DELETE txn_conflict_probe")
