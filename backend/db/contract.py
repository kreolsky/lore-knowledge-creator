"""SurrealDB transaction helper + the query_raw error extractor it shares with cp_store."""

from __future__ import annotations

import asyncio
import logging
import random

from surrealdb import AsyncSurreal
from surrealdb.errors import parse_query_error

from db.pool import (
    _WRITE_CONFLICT_BACKOFF_S,
    DB_WRITE_CONFLICT_ATTEMPTS,
    _is_write_conflict,
)

logger = logging.getLogger(__name__)


def _extract_query_raw_errors(raw) -> list[dict]:
    """Return the error entries in a surrealdb query_raw() response, or [] on success.

    query_raw NEVER raises. Failures surface two ways: a top-level `error` dict
    (parse/transport error — no per-statement `result` list at all) OR per-statement
    result entries with `status == "ERR"`. Inspecting BOTH is the contract that makes
    the failure mode unambiguous across SDK upgrades. Shared by
    run_in_transaction (with NotExecuted filtering) and cp_store.put_content.
    """
    if not isinstance(raw, dict):
        return []
    top_error = raw.get("error")
    if isinstance(top_error, dict):
        # A top-level error has no per-statement list; represent it as a single
        # entry so callers build a uniform message.
        return [top_error]
    results = raw.get("result", [])
    return [r for r in results if isinstance(r, dict) and r.get("status") == "ERR"]


async def run_in_transaction(
    db: AsyncSurreal, statements: list[str], params: dict
) -> list:
    """Run statements atomically in a single SurrealDB transaction.

    Joins them into one "BEGIN TRANSACTION; …; COMMIT TRANSACTION" query — a
    single RPC, so a mid-block error or THROW rolls the whole thing back. Returns
    the per-statement result list on success; re-issues the whole block on a
    write conflict; raises RuntimeError (carrying the underlying DB cause) on any
    other failed statement or once the conflict retries run out.

    # WHY: separate BEGIN / op / COMMIT RPCs do NOT share a transaction scope —
    # each commits independently. query_raw() is the call used because it exposes
    # per-statement status: query() raises the FIRST error entry, which inside a
    # failed transaction is the generic NotExecuted entry, not the cause (on v4 the
    # write conflict arrives on the COMMIT entry, details.kind TransactionConflict).
    # See lessons/2026-06-01-surrealdb-single-string-transaction.md.
    """
    sql = "BEGIN TRANSACTION; " + "; ".join(statements) + "; COMMIT TRANSACTION"
    for attempt in range(DB_WRITE_CONFLICT_ATTEMPTS):
        raw = await db.query_raw(sql, params)
        errors = _extract_query_raw_errors(raw)
        if not errors:
            return raw.get("result", []) if isinstance(raw, dict) else []
        conflict = any(_is_write_conflict(parse_query_error(e)) for e in errors)
        # INVARIANT: the whole transaction is re-issued ONLY when one of its entries
        # is the retryable write conflict, at most DB_WRITE_CONFLICT_ATTEMPTS times.
        # Why: the conflict guarantees the transaction committed nothing (same
        # guarantee as the autocommit retry in db/pool.py); query_raw never raises,
        # so that proxy-level retry never sees it. Any other error raises at once.
        if not conflict or attempt == DB_WRITE_CONFLICT_ATTEMPTS - 1:
            raise _transaction_error(errors)
        logger.warning(
            "Write conflict on site=transaction (attempt %d/%d) — re-issuing",
            attempt + 1, DB_WRITE_CONFLICT_ATTEMPTS,
        )
        # WHY jitter: N transactions that conflicted together and slept the same
        # backoff re-collide on the retry, so only one wins per round and the cap
        # runs out at N > attempts; a random spread lets the retries serialize.
        await asyncio.sleep(_WRITE_CONFLICT_BACKOFF_S * (attempt + 1) * random.uniform(0.5, 2.0))
    raise AssertionError("unreachable: the last attempt returns or raises")


def _transaction_error(errors: list[dict]) -> RuntimeError:
    """RuntimeError naming the real cause of a failed transaction."""
    # Drop the generic "not executed due to a failed transaction" entries so
    # the message carries the real root cause (and the THROW sentinel). A
    # top-level error already has no result entries, so this filter is a no-op
    # for the parse/transport case.
    cause = [
        e for e in errors
        if (e.get("details") or {}).get("kind") != "NotExecuted"
    ] or errors
    return RuntimeError(
        "transaction failed: " + "; ".join(
            str(e.get("result") if e.get("result") is not None else e.get("message", e))
            for e in cause
        )
    )
