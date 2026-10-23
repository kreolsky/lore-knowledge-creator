"""create_system_note failure guard.

A failed FIRST-message write deletes the just-created session — no empty
anchorless note card remains. Mirrors the public path's guard pinned by
test_public_notes.py::test_a_failed_first_message_leaves_no_empty_note.

ONE patch seam (import-shape zero-net-growth): a routing fake over
create_record serves both failure arms (session write, first-message write).
"""
import notes_service
import pytest

from db import get_db


@pytest.mark.asyncio
async def test_failed_writes_leave_no_empty_note(
    client, admin_user, project_with_doc, monkeypatch,
):
    pid, doc_id, admin_uid = project_with_doc

    real_create_record = notes_service.create_record
    fail_table: dict[str, str | None] = {"on": None}

    async def selective_create(table: str, uid: str, data: dict):
        if table == fail_table["on"]:
            raise RuntimeError(f"{table} write failed")
        return await real_create_record(table, uid, data)

    monkeypatch.setattr(notes_service, "create_record", selective_create)

    # Arm 1 — the session write itself fails: None, no row (the unchanged arm).
    fail_table["on"] = "chat_sessions"
    assert await notes_service.create_system_note(
        project_id=pid, document_id=doc_id, title="t", body="b",
        author_id=admin_uid,
    ) is None

    # Arm 2 — the FIRST-message write fails: the just-created session is
    # deleted with it. INVARIANT (notes_service): never raises — None + a log.
    fail_table["on"] = "messages"
    result = await notes_service.create_system_note(
        project_id=pid, document_id=doc_id, title="Ошибка", body="body",
        author_id=admin_uid,
    )
    assert result is None

    db = await get_db()
    live = await db.query(
        "SELECT VALUE meta::id(id) FROM chat_sessions WHERE document_id = $did "
        "AND is_note = true AND deleted_at IS NONE",
        {"did": doc_id},
    )
    assert live == []
