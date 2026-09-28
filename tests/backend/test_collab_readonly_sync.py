"""Read-only / commentator clients MUST receive initial content over WS.

Regression guard: the write-access gate in handle_binary_message used to drop ALL
sync frames from non-'full' clients — including SYNC_STEP1, which is a read request
(the server replies STEP2 = current Y.Doc state). That left read-only/commentator
clients blank over WS; content only appeared after a hard reload (REST paint).
STEP1 must be served for every access level; STEP2/UPDATE (mutations) stay gated.
"""

from unittest.mock import AsyncMock

import pytest
from collab.session import CollabSession, ConnectedClient
from collab.sync import (
    MSG_SYNC,
    MSG_SYNC_STEP2,
    create_sync_step1_message,
    create_sync_step2_message,
    unwrap_binary,
    wrap_binary,
)
from pycrdt import Doc, Text


def _mk_session(**kw) -> CollabSession:
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "hello world"
    doc._lore_text = text
    return CollabSession(entity_type="doc", entity_id="d1", ydoc=doc, **kw)


def _mk_client(user_id="u1", access_level="full") -> ConnectedClient:
    ws = AsyncMock()
    ws.close = AsyncMock()
    return ConnectedClient(ws=ws, user_id=user_id, user_name=user_id, access_level=access_level)


@pytest.fixture(autouse=True)
def _cleanup():
    sessions: list[CollabSession] = []
    yield sessions
    for s in sessions:
        if s._batch_task and not s._batch_task.done():
            s._batch_task.cancel()


class TestReadonlySyncStep1:
    @pytest.mark.parametrize("access_level", ["readonly", "commentator", "notes"])
    async def test_step1_from_nonfull_client_gets_step2_reply(self, _cleanup, access_level):
        session = _mk_session()
        _cleanup.append(session)
        viewer = _mk_client(user_id="viewer", access_level=access_level)
        session.clients[id(viewer.ws)] = viewer

        # Fresh client: its (empty-doc) state vector → server must reply with full state.
        state_vector = Doc().get_state()
        frame = wrap_binary("d1", MSG_SYNC, create_sync_step1_message(state_vector))
        await session.handle_binary_message(frame, viewer.ws, is_multiplexed=True)

        viewer.ws.send_bytes.assert_called_once()
        sent = viewer.ws.send_bytes.call_args[0][0]
        eid, msg_type, payload = unwrap_binary(sent)
        assert eid == "d1"
        assert msg_type == MSG_SYNC
        assert payload[1] == MSG_SYNC_STEP2
        # Mutation flag never set by a read request.
        assert session._dirty is False

    async def test_step2_from_nonfull_client_is_dropped(self, _cleanup):
        # A mutation (STEP2) from a non-full client must NOT touch the Y.Doc.
        session = _mk_session()
        _cleanup.append(session)
        viewer = _mk_client(user_id="viewer", access_level="readonly")
        session.clients[id(viewer.ws)] = viewer

        evil = Doc()
        evil.get("content", type=Text)  # noqa
        update = evil.get_update()
        frame = wrap_binary("d1", MSG_SYNC, create_sync_step2_message(update))
        await session.handle_binary_message(frame, viewer.ws, is_multiplexed=True)

        assert session._dirty is False
        viewer.ws.send_bytes.assert_not_called()
