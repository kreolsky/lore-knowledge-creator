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

    # Snapshot the class's _recv_task BEFORE reload. importlib.reload(db) re-runs the
    # module body and re-binds AsyncWsSurrealConnection._recv_task to a NEW
    # _patched_recv_task object (the reloaded module's). The autouse _ensure_surrealdb_real
    # fixture restores sys.modules["db"] afterward but NOT the class attribute — so the
    # stale (reloaded) function leaks. It is not identity-equal to the canonical
    # db._patched_recv_task, which makes test_sdk_contract's contract #3 identity check
    # fail when both files land on the same -n 4 gateway worker. Restore the pre-reload
    # patch object here so the class surface is left canonical.
    saved_recv_task = getattr(_Conn, "_recv_task", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import db
    importlib.reload(db)
    yield db
    if saved_recv_task is not None:
        _Conn._recv_task = saved_recv_task


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
