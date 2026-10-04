"""SurrealDB _recv_task/_connect monkeypatches + record-id validation (base layer of db).

Imported FIRST by db.__init__ so the patches install as a side effect of `import db`
and run before any get_db() can wedge the SDK reader. Split out of the former
backend/db.py (behavior-preserving).
"""

from __future__ import annotations

import asyncio
import logging
import re

import websockets
from surrealdb.connections.async_ws import AsyncWsSurrealConnection as _AsyncWsConn
from surrealdb.data.cbor import decode as _cbor_decode
from websockets.exceptions import ConnectionClosed, WebSocketException

try:
    # WHY guarded (not a bare import): contract.py defers SDK symbol imports whose
    # module path has drifted across versions; Url is one of them and a hard import
    # would turn drift into an `import db` crash — the None fallback defers the
    # failure to the dial, which raises loudly instead.
    from surrealdb.connections.url import Url
except ImportError:
    Url = None

logger = logging.getLogger("db")


class SurrealReaderDiedError(RuntimeError):
    """The WS reader of this connection exited, killing its in-flight queries.

    Raised (via fut.set_exception in _patched_recv_task) into every query awaiting
    a response when the reader exits — INSTEAD of the SDK's bare future cancel,
    which surfaced as a contextless asyncio.CancelledError. A named exception lets
    the proxy (db.pool._TimedDB) distinguish "our connection died" from a caller
    cancellation and from a DB error, and lets idempotent-marked call sites opt in
    to a re-issue (see pool.py's retry contract). Delivery of the original
    statement is UNCERTAIN (it may have executed before the response was lost), so
    unlike the write conflict this error alone justifies no retry.
    """


# WHY: surrealdb-py _recv_task calls fut.set_result() WITHOUT a fut.done() guard,
# which raises InvalidStateError when the caller cancelled mid-query (e.g. WS reaper
# kills a client whose query is in flight). On 1.0.4 that crash killed the only WS
# reader outright → every later db.query() hung forever (backend wedge). On 2.0.0 the
# upstream body wraps the loop in try/except, so the crash now EXITS the loop and the
# `finally` cancels all pending futures — the reader still dies, just more quietly.
# Either way the connection is lost. This patch mirrors the 2.0.0 body and adds,
# deliberately:
#   1. the missing `and not fut.done()` guard (the original wedge fix), and
#   2. reader-death HANDOFF: the finally marks every pending future with
#      SurrealReaderDiedError instead of bare-cancel + clear. Why not clear: the SDK
#      _send's own `finally: del self.qry[query_id]` then KeyErrors (dict already
#      emptied) and the KeyError MASKS the real cause — every entry in self.qry is
#      owned by exactly one _send (registered immediately before its try/finally),
#      so each _send deleting its own entry drains the dict with no leak and no
#      KeyError. set_exception (not cancel) turns the request-side symptom from
#      `CancelledError → KeyError` into one named, retryable-by-policy error.
# See lessons/2026-04-29-surrealdb-recv-task-race.md.
# INVARIANT: keep this in sync with AsyncWsSurrealConnection._recv_task on SDK bumps —
# Why: it overrides a private SDK internal; a body that drifts from upstream silently
# drops new cleanup/error handling (the bug this guards against returns). The two
# deviations above (fut.done() guard, reader-death handoff instead of clear) are
# DELIBERATE — an SDK bump must re-apply them, not revert to upstream's cleanup.
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
        cause = SurrealReaderDiedError(
            "SurrealDB WS reader exited; this query's delivery is unknown"
        )
        for fut in self.qry.values():
            if not fut.done():
                fut.set_exception(cause)
                # WHY: mark the exception retrieved — a consumer that was already
                # cancelled will never await this future, and an unretrieved future
                # exception logs a spurious asyncio warning. Awaiting consumers still
                # receive it normally.
                fut.exception()
        # DELIBERATE: no self.qry.clear() — see WHY block above.


# WHY capture BEFORE patching: verify_sdk_contract (contract #3) asserts the SDK
# originally defined a _recv_task for our patch to override. If a future bump renames or
# removes it, _ORIG_RECV_TASK is None and the detector refuses to start — otherwise our
# patch would silently shadow a dead name while the real reader runs unpatched (wedge).
# Single source of truth: db.contract imports this for the _assert_recv_task_patched
# default argument (the default is bound at contract import, which __init__ runs AFTER
# _patch, so it captures the pre-patch value here).
_ORIG_RECV_TASK = getattr(_AsyncWsConn, "_recv_task", None)


# WHY: upstream connect() early-returns on `if self.socket` — a LINGERING socket whose
# reader task has died (the one case _patched_recv_task cannot prevent) passes that
# check, so every later _send() writes into a dead connection: the send raises
# ConnectionClosed and requests keep 500ing until the pool's ≤30s liveness probe
# reconnects. This patch early-returns only when the reader is ALIVE, and otherwise
# (under a per-instance lock, re-checking) closes the remnant, reconnects and REPLAYS
# the session — making _send's own leading `await self.connect()` self-heal the dead
# connection with no pool change. Why the replay: a fresh WS socket carries NO session
# — SurrealDB answers every query with `Anonymous access not allowed`. The replay
# re-runs `signin` with the credentials _patched_signin stashed, then `use`, in the
# pool's own order — never `authenticate(self.token)`: the token is the JWT from the
# pool's one signin and expires (1h for root), so a token replay fails on any process
# up longer than that. It runs whenever the connection has signed in before, not when
# a socket happens to be set: a failed dial or the pool's close() leaves socket None,
# and the next dial must still replay. A replay that raises closes the connection and
# re-raises (fail closed), so no query ever runs on an anonymous socket. All of it
# runs INSIDE the lock, so no concurrent _send can slip a query past an un-replayed
# socket. The upstream body below the reconnect check is kept verbatim.
# INVARIANT: keep in sync with AsyncWsSurrealConnection.connect on SDK bumps —
# Why: same as _recv_task above (private internal, silent drift drops upstream handling).
async def _dial(conn: _AsyncWsConn, url: str | None) -> None:
    # Upstream connect() body below the liveness check, kept verbatim apart from
    # the guarded Url import (module head WHY).
    if url is not None:
        if Url is None:
            raise RuntimeError(
                "surrealdb.connections.url moved (SDK drift) — cannot parse the ws url"
            )
        conn.url = Url(url)
        conn.raw_url = f"{conn.url.raw_url}/rpc"
        conn.host = conn.url.hostname
        conn.port = conn.url.port
    conn.socket = await websockets.connect(
        conn.raw_url,
        max_size=None,
        subprotocols=[websockets.Subprotocol("cbor")],
    )
    conn.loop = asyncio.get_running_loop()
    conn.recv_task = asyncio.create_task(conn._recv_task())


async def _replay_session(conn: _AsyncWsConn) -> None:
    # Session replay after a reconnect (WHY in the _patched_connect header). Both
    # calls go through _send → connect() → the reader started by the caller is
    # alive → no lock re-entry.
    if _ORIG_SIGNIN is None or _ORIG_USE is None:
        raise RuntimeError(
            "surrealdb AsyncWsSurrealConnection has no 'signin'/'use' to delegate to — "
            "the SDK moved it; the reconnect session replay cannot work"
        )
    await _ORIG_SIGNIN(conn, conn._session_signin)
    if getattr(conn, "_session_ns", None) is not None:
        await _ORIG_USE(conn, conn._session_ns, conn._session_db, None)


async def _patched_connect(self, url: str | None = None) -> None:
    if (
        self.socket is not None
        and self.recv_task is not None
        and not self.recv_task.done()
    ):
        return
    # WHY lazy: asyncio.Lock binds to the running loop at creation; connections are
    # single-loop (the pool caches per loop), so creating it here on first reconnect
    # is safe and avoids touching SDK __init__.
    lock = getattr(self, "_reconnect_lock", None)
    if lock is None:
        lock = self._reconnect_lock = asyncio.Lock()
    async with lock:
        if (
            self.socket is not None
            and self.recv_task is not None
            and not self.recv_task.done()
        ):
            return  # another coroutine reconnected while we waited on the lock
        if self.socket is not None:
            try:
                await self.socket.close()
            except Exception:  # noqa: BLE001 — remnant is dead; nothing to preserve
                pass
            self.socket = None
            self.recv_task = None
        await _dial(self, url)
        if getattr(self, "_session_signin", None) is not None:
            try:
                await _replay_session(self)
            except BaseException:
                await self.close()  # fail closed — see WHY above
                raise


# WHY capture BEFORE patching (same pattern as _ORIG_RECV_TASK): _patched_signin and
# the replay must delegate to the genuine upstream signin().
_ORIG_SIGNIN = getattr(_AsyncWsConn, "signin", None)


# WHY: stash the credentials the pool signed in with so _patched_connect's session
# replay can re-run signin on a reconnected socket. Stashed only AFTER upstream
# succeeds — a rejected signin stashes nothing, and the very first connect (no stash
# yet) never replays. Delegates to the upstream body unchanged.
async def _patched_signin(self, vars, session_id=None):
    if _ORIG_SIGNIN is None:
        raise RuntimeError(
            "surrealdb AsyncWsSurrealConnection has no 'signin' to delegate to — "
            "the SDK moved it; db.signin() would silently authenticate nothing"
        )
    tokens = await _ORIG_SIGNIN(self, vars, session_id)
    self._session_signin = dict(vars)
    return tokens


# WHY capture BEFORE patching (same pattern as _ORIG_RECV_TASK): _patched_use must
# delegate to the genuine upstream use(), and a future SDK bump that renames/reshapes
# use() must fail loudly instead of the stash silently shadowing a dead method.
_ORIG_USE = getattr(_AsyncWsConn, "use", None)


# WHY: stash the ns/db the pool selected so _patched_connect's session replay can
# re-issue them on a reconnected socket. Delegates to the upstream body unchanged.
async def _patched_use(self, namespace, database, session_id=None) -> None:
    self._session_ns = namespace
    self._session_db = database
    if _ORIG_USE is None:
        raise RuntimeError(
            "surrealdb AsyncWsSurrealConnection has no 'use' to delegate to — "
            "the SDK moved it; db.use() would silently select nothing"
        )
    await _ORIG_USE(self, namespace, database, session_id)


# DEBT: monkey-patches of the private SDK internals `_recv_task`, `connect`, `signin`
#   and `use` —
#   Why deferred: upstream surrealdb-py has no hook for the missing `fut.done()` guard,
#   dead-reader reconnect, or its session replay; drop these when the SDK fixes them
#   (re-check on every SDK bump per the INVARIANTs above).
# ARCH: monkeypatch AsyncWsSurrealConnection at import time — (1) _recv_task: skip
#   set_result() on already-done futures AND hand pending queries a named
#   SurrealReaderDiedError on reader exit (fixes the backend wedge + the
#   CancelledError→KeyError masking); (2) connect: reconnect when the reader is dead
#   and replay signin+use, failing closed (fixes the ≤30s dead-connection window and
#   the anonymous-socket NotAllowedError after a reader exit); (3) signin / use: stash
#   the pool's credentials and ns/db selection for that replay. Installed as a side
#   effect of `import db` (this module is imported first by db/__init__).
_AsyncWsConn._recv_task = _patched_recv_task
_AsyncWsConn.connect = _patched_connect
_AsyncWsConn.signin = _patched_signin
_AsyncWsConn.use = _patched_use

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
