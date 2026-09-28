"""Multiplexed JSON control frames must carry entity_id for client-side routing.

The multiplexed project WS (YjsProjectProvider) dispatches JSON frames by
msg.entity_id. Event-bus handlers that broadcast notifications (checkpoint
created, doc deleted, backlinks changed) without entity_id are silently dropped
by the client — no toast, no live refresh. These tests pin the contract.
"""

import json

import pytest
from collab.events import (
    _on_backlinks_changed_batch,
    _on_entity_deleted,
    _on_note_event,
)
from collab.registry import _get_or_create_session


class _CapturingWS:
    """WebSocket stand-in that records every JSON frame sent to the client."""
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def send_bytes(self, data: bytes) -> None:
        pass


@pytest.mark.asyncio
async def test_checkpoint_created_broadcast_carries_entity_id(_clear_sessions):
    doc_id = "doc-evt-1"
    session = await _get_or_create_session("doc", doc_id, "")
    ws = _CapturingWS()
    session.add_client(ws, "user-1", "User One", "full")

    await _on_note_event(
        "doc", doc_id,
        {"type": "checkpoint_created", "checkpoint": {"checkpoint_id": "c1", "content": "x"}},
    )

    assert ws.sent, "client received no frame"
    frame = ws.sent[-1]
    assert frame["type"] == "checkpoint_created"
    assert frame["entity_id"] == doc_id


@pytest.mark.asyncio
async def test_doc_deleted_broadcast_carries_entity_id(_clear_sessions):
    doc_id = "doc-evt-2"
    session = await _get_or_create_session("doc", doc_id, "")
    ws = _CapturingWS()
    session.add_client(ws, "user-1", "User One", "full")

    await _on_entity_deleted("doc", doc_id)

    assert ws.sent and ws.sent[-1]["type"] == "doc_deleted"
    assert ws.sent[-1]["entity_id"] == doc_id


@pytest.mark.asyncio
async def test_backlinks_changed_broadcast_carries_entity_id(_clear_sessions):
    doc_id = "doc-evt-3"
    session = await _get_or_create_session("doc", doc_id, "")
    ws = _CapturingWS()
    session.add_client(ws, "user-1", "User One", "full")

    await _on_backlinks_changed_batch([doc_id])

    assert ws.sent and ws.sent[-1]["type"] == "backlinks_changed"
    assert ws.sent[-1]["entity_id"] == doc_id
