"""End-to-end presence verification over the real project collab WebSocket.

This is the automatable half of the two-browser manual check: it drives two live
WS clients through the actual route and asserts the presence WIRE behaviour —
- awareness frames relay peer-to-peer (the gutter-bar data),
- join broadcasts drive the chip source (user_joined),
- and, critically, awareness traffic does NOT dirty/persist the document (the fix).

Pixel rendering of the gutter bars / chips still needs a human + two browsers.
"""

import json

import pytest
from collab.registry import _session_key, _sessions
from collab.sync import (
    MSG_AWARENESS,
    MSG_AWARENESS_UPDATE,
    unwrap_binary,
    wrap_binary,
)


def _join(ws, doc_id):
    ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))


def _recv_until(ws, *, want_type=None, want_bytes=False, limit=10):
    """Drain interleaved frames until the wanted JSON type / a binary frame arrives."""
    for _ in range(limit):
        msg = ws.receive()
        if want_bytes and msg.get("bytes") is not None:
            return msg["bytes"]
        if msg.get("text") is not None:
            parsed = json.loads(msg["text"])
            if want_type and parsed.get("type") == want_type:
                return parsed
    raise AssertionError(f"frame not seen (type={want_type}, bytes={want_bytes})")


@pytest.mark.usefixtures("_clear_sessions")
class TestPresenceE2E:
    def test_awareness_relays_to_peer_without_dirtying_doc(self, sync_app, collab_project):
        pid, doc_id, admin_token, user_token, *_ = collab_project

        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as red, sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": user_token}
        ) as black:
            _join(red, doc_id)
            _recv_until(red, want_type="init")
            _join(black, doc_id)
            _recv_until(black, want_type="init")

            # Chip source: red is notified that black joined.
            joined = _recv_until(red, want_type="user_joined")
            assert joined["user"]["user_id"]

            # Gutter-bar source: red publishes an awareness (cursor) frame.
            inner = bytes([MSG_AWARENESS, MSG_AWARENESS_UPDATE]) + b"\x07cursor-payload"
            frame = wrap_binary(doc_id, MSG_AWARENESS, inner)
            red.send_bytes(frame)

            # Black receives the relayed frame verbatim (red excluded as sender).
            relayed = _recv_until(black, want_bytes=True)
            eid, msg_type, _payload = unwrap_binary(relayed)
            assert eid == doc_id
            assert msg_type == MSG_AWARENESS
            assert relayed == frame

            # The fix: relaying awareness must NOT flag the doc dirty — otherwise it
            # would be appended to the ydoc_updates log and brick load() (apply_update
            # on awareness bytes raises ValueError).
            session = _sessions[_session_key("doc", doc_id)]
            assert session._dirty is False
