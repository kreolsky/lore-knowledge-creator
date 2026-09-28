"""Gap #1 — CRDT convergence of two concurrent editors over the real project WS.

No existing test drives TWO live WS replicas of the same doc through the binary
Yjs sync protocol and asserts their local Y.Docs converge. The persistence tests
drive ONE session's doc via apply_binary_edit (single replica); the presence e2e
test exchanges only awareness frames. This fills the gap: two Starlette TestClient
WS connections perform a real step1/step2 handshake, then one applies an edit whose
binary update the server fans out to the other, and both client Y.Docs converge.
"""

import json

import pytest
from collab.sync import (
    MSG_SYNC,
    MSG_SYNC_STEP2,
    MSG_SYNC_UPDATE,
    create_sync_step1_message,
    create_update_message,
    unwrap_binary,
    wrap_binary,
)
from pycrdt import Doc, Text


def _join(ws, doc_id):
    ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))


def _recv(ws, *, want_type=None, want_bytes=False, limit=20):
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


def _sync_handshake(ws, entity_id: str, local_doc: Doc) -> None:
    """Perform the Yjs step1→step2 handshake so the client replica matches the server."""
    state_vector = local_doc.get_state()
    frame = wrap_binary(entity_id, MSG_SYNC, create_sync_step1_message(state_vector))
    ws.send_bytes(frame)
    reply = _recv(ws, want_bytes=True)
    _eid, _mtype, payload = unwrap_binary(reply)
    assert payload[0] == MSG_SYNC
    assert payload[1] == MSG_SYNC_STEP2
    update = payload[2:]
    if update:
        local_doc.apply_update(update)


@pytest.mark.usefixtures("_clear_sessions")
class TestCrdtConvergence:
    def test_two_editors_converge_over_ws(self, sync_app, collab_project):
        pid, doc_id, admin_token, user_token, *_ = collab_project

        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as a, sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": user_token}
        ) as b:
            _join(a, doc_id)
            _recv(a, want_type="init")
            _join(b, doc_id)
            _recv(b, want_type="init")
            # Drain A's view of B's join broadcast so it doesn't pollute binary reads.
            _recv(a, want_type="user_joined")

            doc_a = Doc()
            doc_a.get("content", type=Text)
            doc_b = Doc()
            doc_b.get("content", type=Text)

            # Both replicas sync the server's initial state ("Hello world").
            _sync_handshake(a, doc_id, doc_a)
            _sync_handshake(b, doc_id, doc_b)
            assert str(doc_a.get("content", type=Text)) == str(doc_b.get("content", type=Text))

            # A applies a distinct edit; B must converge to the same merged state.
            state_before = doc_a.get_state()
            doc_a.get("content", type=Text).insert(0, "A-edit ")
            diff = doc_a.get_update(state_before)
            a.send_bytes(wrap_binary(doc_id, MSG_SYNC, create_update_message(diff)))

            # B receives the server-fanned-out binary update and applies it.
            frame = _recv(b, want_bytes=True)
            _eid, _mtype, payload = unwrap_binary(frame)
            assert payload[0] == MSG_SYNC
            assert payload[1] == MSG_SYNC_UPDATE
            doc_b.apply_update(payload[2:])

            content_a = str(doc_a.get("content", type=Text))
            content_b = str(doc_b.get("content", type=Text))
            assert content_a == content_b
            assert "A-edit" in content_a
