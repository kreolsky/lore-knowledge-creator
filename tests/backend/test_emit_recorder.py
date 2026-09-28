"""Tests for the shared emit double — `emit_recorder.EmitRecorder`.

Production callers read `event_bus.emit` at call time, so the double patches
ONE name. These tests pin that contract, including the capture case: a call
made from a real consumer module (`documents.update`) must land in `.calls`,
so a recorder patching a name nobody reads cannot pass as a vacuous green.
"""

from emit_recorder import EmitRecorder

import event_bus


async def test_records_name_and_kwargs_and_restores_the_live_symbol():
    live = event_bus.emit
    with EmitRecorder.active() as rec:
        assert event_bus.emit is not live
        await event_bus.emit("evt", a=1)
    assert event_bus.emit is live
    assert rec.calls == [("evt", {"a": 1})]
    assert rec.names() == ["evt"]
    assert rec.of("evt") == [{"a": 1}]


async def test_consumer_module_call_is_captured(test_db):
    """`documents.update._rebuild_mentions` emits through the module attribute —
    the recorder must see it, or every converted assertion would be vacuous."""
    from documents import update

    with EmitRecorder.active() as rec:
        await update._rebuild_mentions(test_db, "emitrec-d1", "p1", "no links here")
    assert rec.of("content_flushed") == [
        {"entity_type": "doc", "entity_id": "emitrec-d1", "project_id": "p1"},
    ]


async def test_passthrough_calls_the_live_emit():
    seen: list = []

    async def live(event_type, **kwargs):
        seen.append(event_type)

    original = event_bus.emit
    event_bus.emit = live
    try:
        with EmitRecorder.active(passthrough=True) as rec:
            await event_bus.emit("through", x=1)
        assert rec.names() == ["through"]
        assert seen == ["through"]
        assert event_bus.emit is live
    finally:
        event_bus.emit = original


async def test_dead_fake_reinstalled_by_external_undo_is_recovered():
    """A patcher started inside the window saves our fake as its "original" and
    re-installs it after we exited; the next activation must rebind the live
    symbol first, or it would record into a closed window."""
    live = event_bus.emit
    with EmitRecorder.active():
        externally_saved = event_bus.emit
    event_bus.emit = externally_saved
    try:
        with EmitRecorder.active() as second:
            await event_bus.emit("recovered")
        assert second.names() == ["recovered"]
        assert event_bus.emit is live
    finally:
        event_bus.emit = live
