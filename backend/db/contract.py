"""SurrealDB SDK contract detectors + transaction helper.

verify_sdk_contract is called from main.lifespan to refuse startup loudly on SDK
drift (the monkeypatch + helpers depend on assumptions surrealdb-py gives no
stability guarantee on). The four _assert_* detectors are pure and unit-tested;
this module wires them to the live SDK. Split out of the former backend/db.py
(behavior-preserving).
"""

from __future__ import annotations

import asyncio
from typing import Any

from surrealdb import AsyncSurreal
from surrealdb.connections.async_ws import AsyncWsSurrealConnection as _AsyncWsConn
from surrealdb.errors import SurrealError
from websockets.exceptions import ConnectionClosed, WebSocketException

from db._patch import _ORIG_RECV_TASK, _patched_recv_task
from db.pool import get_db, reset_db

# WHY: is_record_id duck-types RecordID by class name; verify_sdk_contract (contract #4)
# needs a real instance to confirm that name match still holds. Imported defensively —
# the symbol's module path drifts across SDK versions (see extract_id's 1.0.4 note), so a
# hard top-level import would crash module load on a bump. None here makes the drift a
# loud startup refusal (verify_sdk_contract) instead of an import-time crash.
try:
    from surrealdb import RecordID as _RecordID
except Exception:  # noqa: BLE001 — symbol may relocate across SDK versions
    _RecordID = None


def _extract_query_raw_errors(raw) -> list[dict]:
    """Return the error entries in a surrealdb query_raw() response, or [] on success.

    query_raw NEVER raises. Failures surface two ways: a top-level `error` dict
    (parse/transport error — no per-statement `result` list at all) OR per-statement
    result entries with `status == "ERR"`. Inspecting BOTH is the contract that makes
    the failure mode unambiguous across SDK upgrades (a future revert of query()'s
    raising behavior on 2.0.0 cannot reintroduce silent success). Shared by
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
    the per-statement result list on success; raises RuntimeError (carrying the
    underlying DB cause) if any statement failed.

    # WHY: separate BEGIN / op / COMMIT RPCs (surrealdb-py 1.0.4) do NOT share a
    # transaction scope — each commits independently, so the old transaction()
    # context manager never rolled anything back. Worse, query() on 1.0.4 swallowed
    # DB errors (returned None, never raises) — see the Pre-flight probe in the
    # cp_store audit: on 2.0.0 query() RAISES instead (so that swallow no longer
    # applies), but query_raw() is still the only call that exposes per-statement
    # status, which we inspect here (via the shared _extract_query_raw_errors, which
    # ALSO guards the top-level error case) to surface the failure unambiguously
    # regardless of which SDK contract holds. See lessons/2026-06-01-surrealdb-single-string-transaction.md.
    """
    sql = "BEGIN TRANSACTION; " + "; ".join(statements) + "; COMMIT TRANSACTION"
    raw = await db.query_raw(sql, params)
    results = raw.get("result", []) if isinstance(raw, dict) else []
    errors = _extract_query_raw_errors(raw)
    if errors:
        # Drop the generic "not executed due to a failed transaction" entries so
        # the message carries the real root cause (and the THROW sentinel). A
        # top-level error already has no result entries, so this filter is a no-op
        # for the parse/transport case.
        cause = [
            e for e in errors
            if (e.get("details") or {}).get("kind") != "NotExecuted"
        ] or errors
        raise RuntimeError(
            "transaction failed: " + "; ".join(
                str(e.get("result") if e.get("result") is not None else e.get("message", e))
                for e in cause
            )
        )
    return results


class SdkContractError(RuntimeError):
    """A surrealdb SDK contract the backend relies on has drifted.

    Raised by verify_sdk_contract (called from main.lifespan) when the monkeypatched
    _recv_task, is_record_id name duck-type, or query/query_raw error-shape assumptions
    no longer hold. Surfacing drift here makes startup refuse to serve loudly instead of
    wedging the reader or breaking serialization quietly.
    """


# ─── SDK contract detectors ──────────────────────────────────────────────────
# Each is a pure assertion over a constructed shape, unit-tested in
# test_sdk_contract.py. verify_sdk_contract drives them against the live connection.
# See lessons/2026-06-14-surrealdb-2.0-strict-errors.md for the 1.0.4→2.0.0 drift these guard.


def _assert_query_raises_on_error(outcome: Any) -> None:
    """Contract 1: db.query() RAISES SurrealError on a failing statement.

    `outcome` is whatever query() returned or raised. surrealdb 1.0.4 silently returned a
    str/None on DB errors; 2.0.0 raises a typed SurrealError. create_record's
    `except SurrealError` guard depends on the raising behavior — a silent return would
    bypass it and surface as a generic failure further from the cause.
    """
    if isinstance(outcome, SurrealError):
        return
    raise SdkContractError(
        "SDK contract #1 broken: db.query() must RAISE SurrealError on a failing statement "
        "(surrealdb 2.0.0 behavior), but it returned "
        f"{type(outcome).__name__}: {outcome!r}. create_record's `except SurrealError` guard "
        "would silently miss this."
    )


def _assert_query_raw_envelope(raw: Any) -> None:
    """Contract 2 (structural): db.query_raw() returns a dict carrying a 'result' list.

    run_in_transaction does `raw.get('result', [])` and inspects per-statement
    `status == 'ERR'`. A non-dict, or a dict without a 'result' list, breaks both.
    (The per-statement error surfacing itself is verified live in verify_sdk_contract via
    _extract_query_raw_errors.)
    """
    if isinstance(raw, dict) and isinstance(raw.get("result"), list):
        return
    raise SdkContractError(
        "SDK contract #2 broken: db.query_raw() must return a dict carrying a 'result' list "
        "(run_in_transaction does raw.get('result', [])); got "
        f"{type(raw).__name__}: {raw!r}."
    )


def _assert_recv_task_patched(cls: type, *, original_recv_task: Any = _ORIG_RECV_TASK) -> None:
    """Contract 3: the _recv_task monkeypatch (fut.done() guard) overrides the REAL reader.

    Two conditions, both required for the patch to actually protect anything:
      1. `original_recv_task` (the SDK's pre-patch _recv_task, captured at import into
         _ORIG_RECV_TASK) was a real method — i.e. our patch overrode the genuine reader.
         If upstream renames/removes _recv_task, this is None: our patch then shadows a
         dead name while the real reader runs unpatched and can wedge the process.
      2. The class's current _recv_task IS _patched_recv_task (the patch is installed).

    Defaults to the module-captured _ORIG_RECV_TASK (bound at import); tests pass None to
    simulate rename/removal. Detected by identity — a plain async function assigned to a
    class is accessed as itself (no __func__), so the identity check is direct.
    """
    if original_recv_task is None:
        raise SdkContractError(
            "SDK contract #3 broken: AsyncWsSurrealConnection had no original '_recv_task' "
            "for the monkeypatch to override — the SDK reader likely moved to a different "
            "method, leaving the fut.done() guard on a dead name (reader-wedge risk)."
        )
    method = getattr(cls, "_recv_task", None)
    if getattr(method, "__func__", method) is not _patched_recv_task:
        raise SdkContractError(
            "SDK contract #3 broken: AsyncWsSurrealConnection._recv_task is NOT the patched "
            "_patched_recv_task — the fut.done() reader-wedge fix is inactive."
        )


def _assert_record_id_name(value: Any) -> None:
    """Contract 4: a RecordID instance reports 'RecordID' in type(v).__name__.

    is_record_id duck-types by class name (a hard import would break on a bump — see
    ARCH note on is_record_id). An SDK rename of RecordID would make every serialize_record
    / coerce_record_ids RecordID branch silently stop matching.
    """
    if "RecordID" not in type(value).__name__:
        raise SdkContractError(
            "SDK contract #4 broken: a surrealdb RecordID must report 'RecordID' in "
            "type(v).__name__ (is_record_id duck-types by class name); got type name "
            f"{type(value).__name__!r}. serialize_record/coerce_record_ids would silently "
            "stop matching RecordIDs."
        )


# Two distinct probes: a parse error reliably RAISES via query() (contract 1), while a
# rolled-back THROW inside a transaction surfaces a per-statement 'status == ERR' via
# query_raw() (contract 2) — neither leaves persisted state.
_SDK_RAISE_PROBE = "THIS IS NOT VALID SURQL @@@"
_SDK_TX_ERR_PROBE = "BEGIN TRANSACTION; THROW 'sdk_contract_probe'; COMMIT TRANSACTION"

# Transient transport errors that are NOT SDK-contract drift: a flaky WS during a startup
# race / DB restart must not be conflated with genuine drift (which would mislabel a
# connectivity blip as an SDK failure and refuse to start). _run_probe retries these on a
# fresh connection; persistent ones propagate as a normal startup failure (the lifespan
# only refuses — and logs CRITICAL — on SdkContractError).
_TRANSIENT_EXC: tuple = (ConnectionClosed, WebSocketException, asyncio.TimeoutError, OSError)


async def _run_probe(probe, db, *, reconnect):
    """Run a live probe callable, returning (result, db).

    SurrealError is RETURNED (not raised) so the caller's detector decides whether it
    satisfies the contract. Transient transport errors retry on a fresh connection (via
    `reconnect`) so a startup-time blip self-heals instead of being mistaken for drift;
    if they persist, the last error propagates as a connectivity problem. `db` is returned
    because reconnect may swap the underlying connection between probes.
    """
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            return await probe(db), db
        except SurrealError as exc:
            return exc, db
        except _TRANSIENT_EXC as exc:
            last_exc = exc
            db = await reconnect(db)
    assert last_exc is not None  # _TRANSIENT_EXC caught at least once to get here
    raise last_exc


async def verify_sdk_contract(db=None) -> None:
    """Verify the surrealdb SDK contracts the monkeypatch + helpers depend on.

    # ARCH: called from main.lifespan after apply_schema(); raises SdkContractError on
    # drift so startup REFUSES to serve (a broken reader/serializer is worse than a
    # startup refusal). Pure w.r.t. the live connection — the probes are a rolled-back
    # THROW and a parse error, neither of which persists state.

    Contracts 3–4 are introspection-only (no connection); 1–2 drive a live (or injected)
    connection. The four _assert_* detectors are pure and unit-tested against constructed
    shapes; this orchestrator wires them to the live SDK. `db` is injectable for tests.
    Transient transport errors during the live probes are retried (not treated as drift).
    """
    # Contract 3: the _recv_task monkeypatch overrides the REAL reader (fut.done() guard).
    _assert_recv_task_patched(_AsyncWsConn)
    # Contract 4: a RecordID reports 'RecordID' in its type name (is_record_id parity).
    if _RecordID is None:
        raise SdkContractError(
            "SDK contract #4 broken: surrealdb.RecordID is not importable — is_record_id's "
            "name-based duck-type can no longer match a RecordID (guards serialize_record / "
            "coerce_record_ids)."
        )
    _assert_record_id_name(_RecordID("documents", "sdk_contract_probe"))
    # Contracts 1–2 need a live connection. With a real connection, retry transient
    # transport blips on a fresh conn; with an injected (test) db, never reconnect.
    if db is None:
        db = await get_db()

        async def _reconnect(_):
            await reset_db()
            return await get_db()
    else:

        async def _reconnect(current):  # noqa: ANN001 — injected test db; never retried
            return current

    # Contract 1: query() raises SurrealError on a failing statement.
    outcome, db = await _run_probe(lambda c: c.query(_SDK_RAISE_PROBE), db, reconnect=_reconnect)
    _assert_query_raises_on_error(outcome)
    # Contract 2: query_raw() returns a dict with a 'result' list AND surfaces the failure
    # as a per-statement 'status == ERR' (or top-level error).
    raw, _ = await _run_probe(lambda c: c.query_raw(_SDK_TX_ERR_PROBE), db, reconnect=_reconnect)
    _assert_query_raw_envelope(raw)
    if not _extract_query_raw_errors(raw):
        raise SdkContractError(
            "SDK contract #2 broken: a failing statement did not surface a per-statement "
            "'status == ERR' (or top-level error) in query_raw() — run_in_transaction would "
            f"treat a failed transaction as success. raw={raw!r}"
        )
