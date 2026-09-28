"""SurrealDB _recv_task monkeypatch + record-id validation (base layer of db).

Imported FIRST by db.__init__ so the patch install is a side effect of `import db`
and runs before any get_db() can wedge the SDK reader. Split out of the former
backend/db.py (behavior-preserving).
"""

from __future__ import annotations

import asyncio
import logging
import re

from surrealdb.connections.async_ws import AsyncWsSurrealConnection as _AsyncWsConn
from surrealdb.data.cbor import decode as _cbor_decode
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger("db")


# WHY: surrealdb-py _recv_task calls fut.set_result() WITHOUT a fut.done() guard,
# which raises InvalidStateError when the caller cancelled mid-query (e.g. WS reaper
# kills a client whose query is in flight). On 1.0.4 that crash killed the only WS
# reader outright → every later db.query() hung forever (backend wedge). On 2.0.0 the
# upstream body wraps the loop in try/except, so the crash now EXITS the loop and the
# `finally` cancels all pending futures — the reader still dies, just more quietly.
# Either way the connection is lost. This patch mirrors the 2.0.0 body verbatim and only
# adds the missing `and not fut.done()` guard, keeping upstream's try/except/finally
# cleanup intact. See lessons/2026-04-29-surrealdb-recv-task-race.md.
# INVARIANT: keep this in sync with AsyncWsSurrealConnection._recv_task on SDK bumps —
# Why: it overrides a private SDK internal; a body that drifts from upstream silently
# drops new cleanup/error handling (the bug this guards against returns).
async def _patched_recv_task(self) -> None:
    assert self.socket
    try:
        async for data in self.socket:
            response = _cbor_decode(data)
            if response_id := response.get("id"):
                if (fut := self.qry.get(response_id)) and not fut.done():
                    fut.set_result(response)
            elif response_result := response.get("result"):
                live_id = str(response_result["id"])
                for queue in self.live_queues.get(live_id, []):
                    queue.put_nowait(response_result)
            else:
                self.check_response_for_error(response, "_recv_task")
    except (ConnectionClosed, WebSocketException, asyncio.CancelledError):
        pass  # WHY: expected reader exit on disconnect/cancel; see WHY block above.
    except Exception as e:  # noqa: BLE001 — mirror upstream's catch-all
        logger.debug("Unexpected error in _recv_task: %s", e)
    finally:
        for fut in self.qry.values():
            if not fut.done():
                fut.cancel()
        self.qry.clear()


# WHY capture BEFORE patching: verify_sdk_contract (contract #3) asserts the SDK
# originally defined a _recv_task for our patch to override. If a future bump renames or
# removes it, _ORIG_RECV_TASK is None and the detector refuses to start — otherwise our
# patch would silently shadow a dead name while the real reader runs unpatched (wedge).
# Single source of truth: db.contract imports this for the _assert_recv_task_patched
# default argument (the default is bound at contract import, which __init__ runs AFTER
# _patch, so it captures the pre-patch value here).
_ORIG_RECV_TASK = getattr(_AsyncWsConn, "_recv_task", None)
# DEBT: monkey-patch of the private SDK internal `_recv_task` — Why deferred: upstream
#   surrealdb-py has no hook for the missing `fut.done()` guard; drop this when the SDK
#   fixes it (re-check on every SDK bump per the INVARIANT above).
# ARCH: monkeypatch AsyncWsSurrealConnection._recv_task at import time to skip
# set_result() on already-done futures — fixes the backend wedge (SDK reader dies when a
# caller cancels mid-query). Installed as a side effect of `import db` (this module is
# imported first by db/__init__).
_AsyncWsConn._recv_task = _patched_recv_task

# SECURITY: Validate record IDs before embedding in RELATE queries.
# SurrealDB RELATE doesn't support parameterized record IDs, so we
# must ensure IDs contain only safe characters (UUID format).
# M-4: ASCII-only to prevent Unicode homoglyph injection
SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')


def validate_record_id(rid: str) -> str:
    """Raise ValueError if rid contains unsafe characters for SurrealDB queries."""
    if not SAFE_ID_RE.match(rid):
        raise ValueError(f"Invalid record ID: {rid!r}")
    return rid
