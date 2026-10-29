"""Integration tests for note-chat sessions (Stage 2).

Covers:
- An anchored note carries both relative positions or neither.
- Note sessions reject the completions (SSE) endpoint.
- list_sessions ?is_note=true drops the user_id predicate (visible to other
  project members with commentator+ access).
- list_sessions unifies a non-reference document's sessions with sessions of
  its is_reference=true children.
"""

import asyncio

import pytest
from emit_recorder import EmitRecorder

from db import create_record, get_db

# ─── Realtime emit capture (plan: notes-realtime-debt-fixes) ─────────────────
#
# The route handlers resolve `emit` off event_bus at call time, so the shared
# `emit_recorder` fixture records every nudge. Emits are fire-and-forget
# (background tasks), so tests drain the loop until the expected event lands.

def _note_events(recorder: EmitRecorder, event_type: str) -> list[dict]:
    return recorder.of(event_type)


async def _drain(recorder: EmitRecorder, event_type: str, *, timeout: float = 10.0) -> None:
    """Yield to the loop until the background emit lands (or timeout)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _note_events(recorder, event_type):
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_delete_note_session_emits_deleted(
    client, admin_user, project_with_doc, emit_recorder,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    note = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies=cookies,
    )
    note_id = note.json()["session_id"]
    emit_recorder.calls.clear()
    await client.delete(f"/api/chat/sessions/{note_id}", cookies=cookies)

    await _drain(emit_recorder, "note_session_deleted")
    events = _note_events(emit_recorder, "note_session_deleted")
    assert events, "note_session_deleted must be emitted"
    assert events[0]["event"]["session_id"] == note_id
    assert events[0]["entity_id"] == doc_id


# ─── Own-only edit/delete ACL (owner = root) ─────────────────────────────────
#
# Access model for note messages:
#   edit   → author OR project owner
#   delete → project owner (cascade) OR author of a LEAF (single row)
# Non-owner author of a non-leaf → 409; anyone else → 403.

async def _make_note(client, pid, doc_id, token):
    """Create an anchorless note session; return its session_id."""
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


async def _grant_access(pid, uid, level):
    """Grant a project-member access level for the duration of the test."""
    pm_id = f"test-pm-{uid}"
    await create_record("project_members", pm_id, {
        "project_id": pid, "user_id": uid, "access_level": level,
    })
    return pm_id


async def _revoke_access(pm_id):
    db = await get_db()
    await db.query("DELETE type::record('project_members', $id)", {"id": pm_id})


@pytest.mark.asyncio
async def test_note_non_author_edit_forbidden(
    client, admin_user, regular_user, project_with_doc,
):
    """A project member (commentator, not owner) cannot edit another's message."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, admin_token)
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "admin wrote this"}, cookies={"lore_session": admin_token},
        )).json()

        resp = await client.patch(
            f"/api/chat/messages/{msg['message_id']}",
            json={"content": "hijack"}, cookies={"lore_session": other_token},
        )
        assert resp.status_code == 403
    finally:
        await _revoke_access(pm_id)


@pytest.mark.asyncio
async def test_note_author_deletes_own_leaf(
    client, admin_user, regular_user, project_with_doc,
):
    """Author may delete their own LEAF message; earlier messages stay intact."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, other_token)
        m1 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "first"}, cookies={"lore_session": other_token},
        )).json()
        m2 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "second", "parent_id": m1["message_id"]},
            cookies={"lore_session": other_token},
        )).json()

        # m2 is a leaf → 200, single row.
        resp = await client.delete(
            f"/api/chat/messages/{m2['message_id']}", cookies={"lore_session": other_token},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 1

        # m1 (the parent) is still present.
        msgs = await client.get(
            f"/api/chat/sessions/{note_id}/messages", cookies={"lore_session": other_token},
        )
        ids = [m["message_id"] for m in msgs.json()["messages"]]
        assert m1["message_id"] in ids
        assert m2["message_id"] not in ids
    finally:
        await _revoke_access(pm_id)


@pytest.mark.asyncio
async def test_note_non_author_delete_forbidden(
    client, admin_user, regular_user, project_with_doc,
):
    """A commentator (not owner, not author) may not delete another's message (403)."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, admin_token)
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "admin's leaf"}, cookies={"lore_session": admin_token},
        )).json()

        resp = await client.delete(
            f"/api/chat/messages/{msg['message_id']}", cookies={"lore_session": other_token},
        )
        assert resp.status_code == 403
    finally:
        await _revoke_access(pm_id)


@pytest.mark.asyncio
async def test_note_full_access_non_owner_delete_forbidden(
    client, admin_user, regular_user, project_with_doc,
):
    """Full-access but NON-owner must NOT be able to delete another's message (403).

    Pins the removal of the old `access_level == 'full'` moderation override —
    only the project owner is root now.
    """
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "full")
    try:
        note_id = await _make_note(client, pid, doc_id, admin_token)
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "owner's message"}, cookies={"lore_session": admin_token},
        )).json()

        resp = await client.delete(
            f"/api/chat/messages/{msg['message_id']}", cookies={"lore_session": other_token},
        )
        assert resp.status_code == 403
    finally:
        await _revoke_access(pm_id)
