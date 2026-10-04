"""Regression test for the _recv_task monkeypatch in db.py.

Reproduces the production wedge from 2026-04-29: surrealdb-py 1.0.4's
_recv_task crashes with InvalidStateError when fut.set_result() runs on
an already-cancelled future. After the crash, no further responses are
delivered — every subsequent db.query() hangs forever.

The patch guards set_result with fut.done() so the reader survives.
"""

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _ensure_surrealdb_real():
    """Make sure the real surrealdb package is loaded (not the schema-test mock).

    INVARIANT: restore sys.modules["db"] to the conftest-patched module on teardown.
    Why: this test reloads `db` into a FRESH module (unpatched get_db + a fresh,
    non-loop-keyed _pool). If that fresh module is left in sys.modules, every later
    test that lazily does `from db import get_db/fetch_one` (e.g. auth.py during WS
    auth) gets the unpatched _pool.get_db, which caches one connection bound to
    whatever loop first populates it (the session loop). TestClient WebSocket tests
    then run on a portal loop and hit "Future attached to a different loop". This
    leaked into ~21 unrelated WS-test failures only in the full suite (2026-05-30).
    """
    saved_db = sys.modules.get("db")
    for mod_name in list(sys.modules):
        if mod_name == "surrealdb" or mod_name.startswith("surrealdb."):
            mod = sys.modules[mod_name]
            if isinstance(getattr(mod, "AsyncSurreal", None), MagicMock):
                del sys.modules[mod_name]
    if "db" in sys.modules:
        del sys.modules["db"]
    yield
    if saved_db is not None:
        sys.modules["db"] = saved_db
    else:
        sys.modules.pop("db", None)


@pytest.fixture()
def db_module():
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection as _Conn

    # Snapshot the class's patched attrs BEFORE reload. importlib.reload(db) re-runs
    # the module body and re-binds AsyncWsSurrealConnection._recv_task/_connect to NEW
    # _patched_* objects (the reloaded module's). The autouse _ensure_surrealdb_real
    # fixture restores sys.modules["db"] afterward but NOT the class attributes — so
    # the stale (reloaded) functions leak. They are not identity-equal to the canonical
    # db._patched_*, which makes test_sdk_contract's contract #3 identity check
    # fail when both files land on the same -n 4 gateway worker. Restore the pre-reload
    # patch objects here so the class surface is left canonical.
    saved_recv_task = getattr(_Conn, "_recv_task", None)
    saved_connect = getattr(_Conn, "connect", None)
    saved_signin = getattr(_Conn, "signin", None)
    saved_use = getattr(_Conn, "use", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import db
    importlib.reload(db)
    yield db
    if saved_recv_task is not None:
        _Conn._recv_task = saved_recv_task
    if saved_connect is not None:
        _Conn.connect = saved_connect
    if saved_signin is not None:
        _Conn.signin = saved_signin
    if saved_use is not None:
        _Conn.use = saved_use


@pytest.fixture()
def fake_dial(monkeypatch):
    """Install a fake for the SDK transport's websockets.connect (the dial _dial makes)."""

    def install(fake_connect):
        monkeypatch.setattr("websockets.connect", fake_connect)

    return install


class _FakeSocket:
    """Async iterable that yields raw bytes; resolves when items list is exhausted."""
    def __init__(self, items: list[bytes]):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._items.pop(0)


class _FakeConn:
    """Minimal stand-in for AsyncWsSurrealConnection — only the attrs _recv_task touches."""
    def __init__(self, socket):
        self.socket = socket
        self.qry: dict[str, asyncio.Future] = {}
        self.live_queues: dict[str, list] = {}

    def check_response_for_error(self, response, ctx):
        raise AssertionError(f"unexpected error response in {ctx}: {response}")


@pytest.mark.asyncio
async def test_patched_recv_task_survives_cancelled_future(db_module, monkeypatch):
    """The reader must not die when a response arrives for a cancelled future."""
    from surrealdb.data.cbor import encode

    fut_cancelled = asyncio.get_event_loop().create_future()
    fut_cancelled.cancel()

    fut_live = asyncio.get_event_loop().create_future()

    socket = _FakeSocket([
        encode({"id": "req-cancelled", "result": "stale"}),
        encode({"id": "req-live", "result": "ok"}),
    ])
    conn = _FakeConn(socket)
    conn.qry["req-cancelled"] = fut_cancelled
    conn.qry["req-live"] = fut_live

    # Bind the patched method as if it were defined on the class
    task = asyncio.create_task(db_module._patched_recv_task(conn))

    result = await asyncio.wait_for(fut_live, timeout=1.0)
    assert result == {"id": "req-live", "result": "ok"}

    await asyncio.wait_for(task, timeout=1.0)
    assert task.exception() is None


def test_patch_is_installed_on_class(db_module):
    """Importing db.py must replace AsyncWsSurrealConnection._recv_task with the patched one.

    Guards against accidental removal of the monkeypatch — without it, a single
    cancelled-mid-query future kills the SDK's WS reader and wedges the backend.
    """
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection
    assert AsyncWsSurrealConnection._recv_task is db_module._patched_recv_task


def test_connect_patch_is_installed_on_class(db_module):
    """The dead-reader reconnect patch must override the SDK's connect too."""
    # import-as, not db_module._patch: after _ensure_surrealdb_real deletes
    # sys.modules["db"], the fresh parent does NOT re-bind the already-imported
    # submodule as an attribute — only `import db._patch` resolves it.
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection

    import db._patch as db_patch  # noqa: PLC0415
    assert AsyncWsSurrealConnection.connect is db_patch._patched_connect
    assert AsyncWsSurrealConnection.signin is db_patch._patched_signin


@pytest.mark.asyncio
async def test_reader_death_marks_pending_future_with_reader_died(db_module):
    """Reader exit must hand pending queries a NAMED SurrealReaderDiedError — not a
    bare CancelledError (which masked the cause and read as caller cancellation) —
    and must NOT clear conn.qry: each _send deletes its own entry in its own
    finally, so an SDK-side clear is what produced the KeyError-masking 500s."""
    from db._patch import SurrealReaderDiedError

    pending = asyncio.get_running_loop().create_future()
    socket = _FakeSocket([])  # exhausts immediately → reader exits cleanly
    conn = _FakeConn(socket)
    conn.qry["req-1"] = pending

    task = asyncio.create_task(db_module._patched_recv_task(conn))

    with pytest.raises(SurrealReaderDiedError):
        await asyncio.wait_for(pending, timeout=1.0)
    await asyncio.wait_for(task, timeout=1.0)
    assert task.exception() is None
    assert "req-1" in conn.qry, "reader must leave entry deletion to the owning _send"


@pytest.mark.asyncio
async def test_patched_connect_reconnects_after_reader_death(db_module, fake_dial):
    """connect() must treat a lingering socket with a DEAD reader task as disconnected:
    close the remnant, dial fresh, start a new reader; and fast-path while the reader
    is alive (no double connects)."""
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection

    conn = AsyncWsSurrealConnection("ws://localhost:9999")
    dead_socket = _FakeSocket([])
    conn.socket = dead_socket
    # Dead reader as a DONE future — deterministically done() (a sleep(0) task is
    # not guaranteed to have run after one loop yield).
    conn.recv_task = asyncio.get_running_loop().create_future()
    conn.recv_task.set_result(None)

    created: list[str] = []

    class _NewSocket:
        async def send(self, data):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            pass

    async def fake_connect(url, **kwargs):
        created.append(url)
        return _NewSocket()

    fake_dial(fake_connect)

    await conn.connect()
    assert created == ["ws://localhost:9999/rpc"], "must dial once for the dead reader"
    assert conn.socket is not dead_socket
    assert conn.recv_task is not None and not conn.recv_task.done()

    await conn.connect()
    assert created == ["ws://localhost:9999/rpc"], "alive reader must fast-path, not redial"

    await conn.close()  # cancel the fresh reader task cleanly


class _EchoSocket:
    """Answers every sent request frame with a result frame of the same id, recording
    the request methods in order. Methods in `reject` are answered with an error frame
    instead (e.g. an `authenticate` whose token has expired)."""

    def __init__(self, reject: dict[str, str] | None = None):
        self._replies: asyncio.Queue = asyncio.Queue()
        self.methods: list[str] = []
        self.reject = reject or {}
        self.closed = False

    async def send(self, data):
        from surrealdb.data.cbor import decode as cbor_decode
        from surrealdb.data.cbor import encode as cbor_encode

        req = cbor_decode(data)
        method = req.get("method")
        self.methods.append(method)
        if method in self.reject:
            frame = {"id": req["id"], "error": {"code": -32000, "message": self.reject[method]}}
        else:
            frame = {"id": req["id"], "result": "ok"}
        await self._replies.put(cbor_encode(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._replies.get()

    async def close(self):
        self.closed = True


def _signed_in_conn_with_dead_reader():
    """A connection in the state the pool leaves it after signin + use, whose WS
    reader has since exited (socket still set, reader task done)."""
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection

    conn = AsyncWsSurrealConnection("ws://localhost:9999")
    conn.socket = _EchoSocket()
    # Done future — deterministically done() (see test_patched_connect above).
    conn.recv_task = asyncio.get_running_loop().create_future()
    conn.recv_task.set_result(None)
    # JWT-shaped — the SDK's request schema validates the token pattern. Stands for
    # the pool's signin token, which expires after an hour.
    conn.token = "aaa.bbb.ccc"
    conn._session_signin = {"username": "root", "password": "pw"}
    conn._session_ns = "test_ns"
    conn._session_db = "test_db"
    return conn


def _query(text: str):
    from surrealdb.request_message.message import RequestMessage
    from surrealdb.request_message.methods import RequestMethod

    return RequestMessage(RequestMethod.QUERY, query=text, params={})


@pytest.mark.asyncio
async def test_send_self_heals_after_reader_death(db_module, fake_dial):
    """The money path: with the reader dead, _send transparently reconnects through
    the patched connect(), REPLAYS the session by signing in again with the stashed
    credentials (never authenticate(token) — the token expires an hour after the
    pool's signin, so a token replay fails on any long-running process) and selecting
    ns/db, and completes over the fresh connection — with conn.qry left empty."""
    conn = _signed_in_conn_with_dead_reader()
    echo = _EchoSocket(reject={"authenticate": "The token has expired"})
    created: list[str] = []

    async def fake_connect(url, **kwargs):
        created.append(url)
        return echo

    fake_dial(fake_connect)

    resp = await conn._send(_query("RETURN 1"), "query", bypass=True)
    assert resp["result"] == "ok"
    assert created == ["ws://localhost:9999/rpc"]
    assert echo.methods == ["signin", "use", "query"], (
        "reconnect must replay signin + use BEFORE any caller query — "
        "otherwise the fresh socket is anonymous and queries fail NotAllowedError"
    )
    assert conn.qry == {}, "the owner must have deleted its own qry entry"
    assert conn.socket is echo and not conn.recv_task.done()

    # Second send rides the now-alive reader — no redial, no replay.
    resp2 = await conn._send(_query("RETURN 2"), "query", bypass=True)
    assert resp2["result"] == "ok"
    assert created == ["ws://localhost:9999/rpc"]
    assert echo.methods[-1] == "query"

    await conn.close()


@pytest.mark.asyncio
async def test_replay_runs_after_a_failed_dial(db_module, fake_dial):
    """A dial that fails (SurrealDB restarting) leaves socket None; the next dial must
    still replay the session — the connection signed in before, whatever the socket
    state — or the retried query runs on an anonymous socket."""
    conn = _signed_in_conn_with_dead_reader()
    echo = _EchoSocket()
    dials: list[str] = []

    async def fake_connect(url, **kwargs):
        dials.append(url)
        if len(dials) == 1:
            raise ConnectionRefusedError("surreal is restarting")
        return echo

    fake_dial(fake_connect)

    with pytest.raises(ConnectionRefusedError):
        await conn._send(_query("RETURN 1"), "query", bypass=True)
    assert conn.socket is None

    resp = await conn._send(_query("RETURN 1"), "query", bypass=True)
    assert resp["result"] == "ok"
    assert len(dials) == 2
    assert echo.methods == ["signin", "use", "query"]

    await conn.close()


@pytest.mark.asyncio
async def test_failed_replay_closes_the_socket(db_module, fake_dial):
    """A replay the server rejects must not leave a live anonymous socket behind:
    the connection fails closed, and the next call redials and replays."""
    conn = _signed_in_conn_with_dead_reader()
    rejecting = _EchoSocket(reject={"signin": "There was a problem with authentication"})
    healthy = _EchoSocket()
    sockets = [rejecting, healthy]

    async def fake_connect(url, **kwargs):
        return sockets.pop(0)

    fake_dial(fake_connect)

    with pytest.raises(Exception, match="problem with authentication"):
        await conn._send(_query("RETURN 1"), "query", bypass=True)
    assert rejecting.methods == ["signin"], "no query may reach an un-replayed socket"
    assert conn.socket is None and conn.recv_task is None
    assert rejecting.closed

    resp = await conn._send(_query("RETURN 1"), "query", bypass=True)
    assert resp["result"] == "ok"
    assert healthy.methods == ["signin", "use", "query"]

    await conn.close()


@pytest.mark.asyncio
async def test_signin_stashes_credentials_only_on_success(db_module, fake_dial):
    """A rejected signin stashes nothing (a later reconnect must not replay bad
    credentials); an accepted one stashes what it signed in with."""
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection

    sockets = [_EchoSocket(reject={"signin": "There was a problem with authentication"}),
               _EchoSocket()]

    async def fake_connect(url, **kwargs):
        return sockets.pop(0)

    fake_dial(fake_connect)

    bad = AsyncWsSurrealConnection("ws://localhost:9999")
    with pytest.raises(Exception, match="problem with authentication"):
        await bad.signin({"username": "root", "password": "wrong"})
    assert getattr(bad, "_session_signin", None) is None
    await bad.close()

    good = AsyncWsSurrealConnection("ws://localhost:9999")
    await good.signin({"username": "root", "password": "pw"})
    assert good._session_signin == {"username": "root", "password": "pw"}
    await good.close()
