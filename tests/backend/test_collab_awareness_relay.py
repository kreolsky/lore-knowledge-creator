"""Awareness frames are relay-only — never persisted to or applied on the Y.Doc.

Regression guard: the frontend now sends MSG_AWARENESS frames (presence cursors).
Routing them through the sync/persist path appends them to the ydoc_updates log,
and `apply_update()` on awareness bytes raises ValueError on the next load() —
bricking the document. Awareness must be broadcast to peers and nothing else.
"""

from unittest.mock import AsyncMock, patch

import pytest
from collab.session import CollabSession, ConnectedClient
from collab.sync import MSG_AWARENESS, wrap_binary
from pycrdt import Doc, Text


def _mk_session(**kw) -> CollabSession:
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "hello"
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


class TestAwarenessRelayOnly:
    async def test_awareness_is_relayed_but_not_persisted(self, _cleanup):
        session = _mk_session()
        _cleanup.append(session)
        sender = _mk_client(user_id="sender")
        peer = _mk_client(user_id="peer")
        session.clients[id(sender.ws)] = sender
        session.clients[id(peer.ws)] = peer

        frame = wrap_binary("d1", MSG_AWARENESS, b"\x01\x02\x03")

        with patch("ydoc_store.append_update", new=AsyncMock()) as append_mock:
            await session.handle_binary_message(frame, sender.ws, is_multiplexed=True)

        # Not persisted, not flagged dirty, never appended to the update log.
        append_mock.assert_not_called()
        assert session._dirty is False

        # Relayed to peers, excluding the sender.
        assert len(session._pending_broadcasts) == 1
        msg, excludes = session._pending_broadcasts[0]
        assert msg == frame
        assert sender.ws in excludes

    async def test_readonly_client_awareness_is_relayed(self, _cleanup):
        # Awareness is read-only metadata — allowed even for viewers.
        session = _mk_session()
        _cleanup.append(session)
        viewer = _mk_client(user_id="viewer", access_level="readonly")
        session.clients[id(viewer.ws)] = viewer

        frame = wrap_binary("d1", MSG_AWARENESS, b"\x01\x02\x03")
        await session.handle_binary_message(frame, viewer.ws, is_multiplexed=True)

        assert session._dirty is False
        assert len(session._pending_broadcasts) == 1
