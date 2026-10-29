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
