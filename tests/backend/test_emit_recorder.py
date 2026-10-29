"""Tests for the shared emit double — `emit_recorder.EmitRecorder`.

Production callers read `event_bus.emit` at call time, so the double patches
ONE name. These tests pin that contract, including the capture case: a call
made from a real consumer module (`documents.update`) must land in `.calls`,
so a recorder patching a name nobody reads cannot pass as a vacuous green.
"""

from emit_recorder import EmitRecorder


async def test_consumer_module_call_is_captured(test_db):
    """`documents.update._rebuild_mentions` emits through the module attribute —
    the recorder must see it, or every converted assertion would be vacuous."""
    from documents import update

    with EmitRecorder.active() as rec:
        await update._rebuild_mentions(test_db, "emitrec-d1", "p1", "no links here")
    assert rec.of("content_flushed") == [
        {"entity_type": "doc", "entity_id": "emitrec-d1", "project_id": "p1"},
    ]
