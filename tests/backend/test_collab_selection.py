"""Collab WS selection-message handling (plan stage-1 §3.6 presence/region-lock).

A client reports its active selection over the WS channel as a JSON `selection`
message. The backend tracks it in the advisory selection registry (used by the
region-lock pre-apply check). Selections are read-only metadata — allowed for all
access levels (mirrors awareness). Cleared on leave and when a collapsed caret is
reported.
"""

import json

import pytest
from collab import selection_registry as reg
from helpers import join_collab_ws, project_collab_url


@pytest.fixture(autouse=True)
def _reset_registry():
    reg._reset()
    yield
    reg._reset()


class TestSelectionDispatch:
    # (The former per-entity twin of the tracked-selection test died with the
    # per-entity route in plan fewer-layers — byte-identical to the remaining
    # channel's test once the channel was gone.)

    def test_collapsed_caret_clears_selection(self, sync_app, collab_project):
        pid, doc_id, admin_token, *_ = collab_project
        uid = collab_project[4]

        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            json.loads(ws.receive_text())  # init (join ack)
            ws.send_text(json.dumps({
                "type": "selection", "entity_id": doc_id,
                "from_cp": 10, "to_cp": 20,
            }))
            ws.send_text(json.dumps({"type": "flush", "entity_id": doc_id}))
            json.loads(ws.receive_text())

            ws.send_text(json.dumps({
                "type": "selection", "entity_id": doc_id,
                "from_cp": 5, "to_cp": 5,
            }))
            ws.send_text(json.dumps({"type": "flush", "entity_id": doc_id}))
            json.loads(ws.receive_text())

            assert reg._selections.get(doc_id, {}).get(uid) is None

    def test_selection_is_tracked(self, sync_app, collab_project):
        pid, doc_id, admin_token, *_ = collab_project
        uid = collab_project[4]

        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            json.loads(ws.receive_text())  # init (join ack)

            ws.send_text(json.dumps({
                "type": "selection", "entity_id": doc_id,
                "from_cp": 30, "to_cp": 40,
            }))
            # Flush round-trip guarantees the server processed the selection on the
            # same receive loop before we read the in-memory registry.
            ws.send_text(json.dumps({"type": "flush", "entity_id": doc_id}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "flush_ack"

            sel = reg._selections.get(doc_id, {}).get(uid)
            assert sel is not None
            assert (sel["from_cp"], sel["to_cp"]) == (30, 40)

    def test_leave_clears_selection(self, sync_app, collab_project):
        import time

        pid, doc_id, admin_token, *_ = collab_project
        uid = collab_project[4]

        ws_mux = sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        )
        ws_mux.__enter__()
        try:
            ws_mux.send_text(json.dumps({
                "type": "join", "entity_type": "doc", "entity_id": doc_id,
            }))
            json.loads(ws_mux.receive_text())
            ws_mux.send_text(json.dumps({
                "type": "selection", "entity_id": doc_id,
                "from_cp": 1, "to_cp": 9,
            }))
            ws_mux.send_text(json.dumps({"type": "flush", "entity_id": doc_id}))
            json.loads(ws_mux.receive_text())
            assert reg._selections.get(doc_id, {}).get(uid) is not None
        finally:
            ws_mux.__exit__(None, None, None)

        # The server clears the selection in the WS finally block (leave_collab_session)
        # asynchronously after the disconnect; poll until it lands.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if reg._selections.get(doc_id, {}).get(uid) is None:
                break
            time.sleep(0.02)
        assert reg._selections.get(doc_id, {}).get(uid) is None
