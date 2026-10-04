"""A client Yjs mutation the multiplexed WS discards leaves one telemetry row per socket+entity."""

import uuid

import pytest
from collab import drop_telemetry
from collab.sync import (
    MSG_AWARENESS,
    MSG_SYNC,
    MSG_SYNC_STEP1,
    MSG_SYNC_UPDATE,
    wrap_binary,
)
from routes.collab_project_ws import _route_binary_message


class _FakeWs:
    pass


class _FakeSession:
    def __init__(self):
        self.clients: dict = {}
        self.handled: list[bytes] = []

    async def handle_binary_message(self, data, ws, is_multiplexed):
        self.handled.append(data)


@pytest.fixture(autouse=True)
def _fresh_gate():
    drop_telemetry._reported.clear()


@pytest.fixture
def uid():
    return f"drop-{uuid.uuid4().hex[:8]}"


async def _rows(db, user_id: str) -> list[dict]:
    return await db.query(
        "SELECT kind, user_id, project_id, entity_id, detail FROM telemetry_event WHERE user_id = $u",
        {"u": user_id},
    )


def _update(entity_id: str) -> bytes:
    return wrap_binary(entity_id, MSG_SYNC, bytes([MSG_SYNC, MSG_SYNC_UPDATE, 1, 2, 3]))


@pytest.mark.asyncio
async def test_update_for_unjoined_entity_is_recorded_once_per_socket(test_db, uid):
    ws = _FakeWs()
    for _ in range(3):
        await _route_binary_message(ws, _update("e1"), {}, user_id=uid, project_id="p1")

    rows = await _rows(test_db, uid)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "server-drop-unjoined"
    assert (row["project_id"], row["entity_id"]) == ("p1", "e1")
    assert row["detail"]["sync_type"] == MSG_SYNC_UPDATE

    # A second socket dropping the same entity is a separate trace.
    await _route_binary_message(_FakeWs(), _update("e1"), {}, user_id=uid, project_id="p1")
    assert len(await _rows(test_db, uid)) == 2


@pytest.mark.asyncio
async def test_step1_and_awareness_for_unjoined_entity_are_not_losses(test_db, uid):
    ws = _FakeWs()
    step1 = wrap_binary("e1", MSG_SYNC, bytes([MSG_SYNC, MSG_SYNC_STEP1, 0]))
    awareness = wrap_binary("e1", MSG_AWARENESS, bytes([MSG_AWARENESS, 1, 0]))
    await _route_binary_message(ws, step1, {}, user_id=uid, project_id="p1")
    await _route_binary_message(ws, awareness, {}, user_id=uid, project_id="p1")
    assert await _rows(test_db, uid) == []


@pytest.mark.asyncio
async def test_update_from_socket_the_session_no_longer_lists_is_recorded(test_db, uid):
    ws = _FakeWs()
    session = _FakeSession()
    joined = {"e1": (session, object())}
    await _route_binary_message(ws, _update("e1"), joined, user_id=uid, project_id="p1")

    assert [r["kind"] for r in await _rows(test_db, uid)] == ["server-drop-unregistered"]
    # The frame still goes to the session, whose own guard decides — behaviour unchanged.
    assert len(session.handled) == 1


@pytest.mark.asyncio
async def test_update_from_registered_socket_records_nothing(test_db, uid):
    ws = _FakeWs()
    session = _FakeSession()
    session.clients[id(ws)] = object()
    await _route_binary_message(ws, _update("e1"), {"e1": (session, object())}, user_id=uid, project_id="p1")
    assert await _rows(test_db, uid) == []
    assert len(session.handled) == 1
