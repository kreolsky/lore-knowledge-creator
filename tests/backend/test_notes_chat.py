"""Integration tests for note-chat sessions (Stage 2).

Covers:
- Note sessions require anchor_offset_start/end at create.
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


async def _drain(recorder: EmitRecorder, event_type: str, *, timeout: float = 2.0) -> None:
    """Yield to the loop until the background emit lands (or timeout)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if _note_events(recorder, event_type):
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_create_note_message_emits_message_changed(
    client, admin_user, project_with_doc, emit_recorder,
):
    """A new note message emits note_message_changed(action=added) targeting the
    document's collab session (entity_id = the document_id)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    note = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies=cookies,
    )
    # The create_session emit (note_session_created) is also captured; clear it so
    # the message-mutation assertion is unambiguous.
    emit_recorder.calls.clear()
    note_id = note.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "hello realtime"},
        cookies=cookies,
    )
    assert resp.status_code == 201

    await _drain(emit_recorder, "note_message_changed")
    events = _note_events(emit_recorder, "note_message_changed")
    assert events, "note_message_changed must be emitted on message create"
    frame = events[0]["event"]
    assert frame["action"] == "added"
    assert frame["session_id"] == note_id
    assert events[0]["entity_type"] == "doc"
    # Decision 1: entity_id is the resolved (parent) document — here a plain doc.
    assert events[0]["entity_id"] == doc_id
    assert frame["message"]["content"] == "hello realtime"
    # Preview derived server-side.
    assert frame["preview"]["count"] == 1
    assert frame["preview"]["first"] == "hello realtime"


@pytest.mark.asyncio
async def test_note_on_reference_emits_parent_doc_id(
    client, admin_user, project_with_doc, emit_recorder,
):
    """Decision 1 regression: a note anchored to a reference-doc must emit
    entity_id = <parent document id>, NOT the ref-doc id. Otherwise the frame is
    broadcast to a collab session nobody has joined and is silently dropped."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": doc_id,
        },
        cookies=cookies,
    )
    ref_id = ref_resp.json()["document_id"]

    note = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": ref_id, "is_note": True},
        cookies=cookies,
    )
    assert note.status_code == 201
    note_id = note.json()["session_id"]

    await _drain(emit_recorder, "note_session_created")
    created = _note_events(emit_recorder, "note_session_created")
    assert created, "note_session_created must be emitted on note create"
    assert created[0]["entity_id"] == doc_id, \
        "Reference-anchored note must broadcast to the PARENT doc session"

    emit_recorder.calls.clear()
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "ref note body"},
        cookies=cookies,
    )
    await _drain(emit_recorder, "note_message_changed")
    changed = _note_events(emit_recorder, "note_message_changed")
    assert changed, "note_message_changed must be emitted"
    assert changed[0]["entity_id"] == doc_id, \
        "Reference-anchored note message must broadcast to the PARENT doc session"


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


@pytest.mark.asyncio
async def test_session_preview_matches_list_aggregate(
    client, admin_user, project_with_doc, emit_recorder,
):
    """_session_preview (per-session helper used by the emit path) produces the
    same shape as the list_sessions grouped aggregate for a 2-message note."""
    from routes.chat.serializers import _session_preview

    from db import get_db

    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    note = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies=cookies,
    )
    note_id = note.json()["session_id"]
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "first message body"}, cookies=cookies,
    )
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "second message body"}, cookies=cookies,
    )

    db = await get_db()
    preview = await _session_preview(db, note_id)
    assert preview["count"] == 2
    assert preview["first"] == "first message body"
    assert preview["last"] == "second message body"

    # The last emitted note_message_changed frame carries the same derived preview.
    # Wait for BOTH message frames (two messages posted) before reading the final one.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 2.0
    while loop.time() < deadline:
        if len(_note_events(emit_recorder, "note_message_changed")) >= 2:
            break
        await asyncio.sleep(0.01)
    changed = _note_events(emit_recorder, "note_message_changed")
    assert changed
    assert changed[-1]["event"]["preview"] == preview


@pytest.mark.asyncio
async def test_ai_chat_message_does_not_emit_note_event(
    client, admin_user, project_with_doc, emit_recorder,
):
    """A non-note (AI chat) message must NOT emit the note realtime nudge —
    guards the is_note gate so AI-chat traffic stays off the notes channel."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    chat = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "m1"},
        cookies=cookies,
    )
    chat_id = chat.json()["session_id"]
    emit_recorder.calls.clear()
    await client.post(
        f"/api/chat/sessions/{chat_id}/messages",
        json={"content": "ai message"}, cookies=cookies,
    )
    # Yield so any (incorrectly-queued) background note emit would land.
    await asyncio.sleep(0.05)
    assert not _note_events(emit_recorder, "note_message_changed")
    assert not _note_events(emit_recorder, "note_session_created")


# ─── Pydantic validator ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_anchorless_note_succeeds(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_note_inverted_anchor_rejected(
    client, admin_user, project_with_doc,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid, "document_id": doc_id, "is_note": True,
            "anchor_offset_start": 50, "anchor_offset_end": 10,
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_note_session(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid, "document_id": doc_id, "is_note": True,
            "anchor_offset_start": 10, "anchor_offset_end": 20,
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["is_note"] is True


# ─── Completion guard ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_note_session_rejects_completion(
    client, admin_user, project_with_doc,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    sess_resp = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid, "document_id": doc_id, "is_note": True,
            "anchor_offset_start": 0, "anchor_offset_end": 5,
        },
        cookies=cookies,
    )
    sid = sess_resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        cookies=cookies,
    )
    assert resp.status_code == 400
    assert "LLM-disabled" in resp.json()["detail"]


# ─── Visibility (user_id predicate drop) ────────────────────────────────────

@pytest.mark.asyncio
async def test_note_visible_to_other_member(
    client, admin_user, regular_user, project_with_doc,
):
    """A note created by admin must be visible to a commentator project member."""
    pid, doc_id, admin_uid = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    # Grant the regular user commentator access.
    await create_record("project_members", "test-pm-002", {
        "project_id": pid, "user_id": other_uid, "access_level": "commentator",
    })
    try:
        # Admin creates a note.
        sess = await client.post(
            "/api/chat/sessions",
            json={
                "project_id": pid, "document_id": doc_id, "is_note": True,
                "anchor_offset_start": 0, "anchor_offset_end": 5,
            },
            cookies={"lore_session": admin_token},
        )
        note_id = sess.json()["session_id"]

        # Other user lists notes; sees the admin's note.
        resp = await client.get(
            f"/api/chat/sessions?project_id={pid}&document_id={doc_id}&is_note=true",
            cookies={"lore_session": other_token},
        )
        assert resp.status_code == 200
        ids = [s["session_id"] for s in resp.json()]
        assert note_id in ids

        # AI-chat list (is_note=false) for other user remains private: empty.
        resp_ai = await client.get(
            f"/api/chat/sessions?project_id={pid}&document_id={doc_id}&is_note=false",
            cookies={"lore_session": other_token},
        )
        assert resp_ai.status_code == 200
        assert resp_ai.json() == []
    finally:
        db = await get_db()
        # type::record() — surrealdb 2.0.0 parses a bare hyphenated id (test-pm-002)
        # as arithmetic ("test - pm - 002"); the explicit constructor avoids that.
        await db.query("DELETE type::record('project_members', 'test-pm-002')")


# ─── Unified scope (doc + reference children) ────────────────────────────────

@pytest.mark.asyncio
async def test_list_sessions_unifies_reference_children(
    client, admin_user, project_with_doc,
):
    """AI visibility is project-wide: a chat anchored to a reference (a reference IS a
    document) is visible when listing from the parent document, and vice versa. Note
    scope (is_note=true) is the branch that KEEPS ancestor/sibling/children scoping —
    see test_list_notes_* below."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    # Create a reference under the document.
    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": doc_id,
        },
        cookies=cookies,
    )
    ref_id = ref_resp.json()["document_id"]

    # One chat anchored to the document.
    s1 = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "m1"},
        cookies=cookies,
    )
    own = s1.json()["session_id"]

    # One chat anchored to the reference.
    s2 = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": ref_id, "model": "m2"},
        cookies=cookies,
    )
    ref_chat = s2.json()["session_id"]

    # Listing at the document should include both (project-wide AI visibility).
    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}",
        cookies=cookies,
    )
    ids = {s["session_id"] for s in resp.json()}
    assert own in ids
    assert ref_chat in ids

    # Listing at the reference itself includes both too (project-wide).
    resp_ref = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={ref_id}",
        cookies=cookies,
    )
    ids_ref = {s["session_id"] for s in resp_ref.json()}
    assert ref_chat in ids_ref
    assert own in ids_ref


# ─── Note reference_id preservation ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_note_on_reference_returns_reference_id(
    client, admin_user, project_with_doc,
):
    """Note sessions anchored to a reference must expose reference_id for
    frontend grouping and navigation. The 2026-05 refactor accidentally
    cleared reference_id to None for note sessions, breaking the NotesPanel
    grouping and ref-mode filtering."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    # Create a reference under the document.
    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": doc_id,
        },
        cookies=cookies,
    )
    ref_id = ref_resp.json()["document_id"]

    # Create a note inside the reference.
    note_resp = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid, "document_id": ref_id, "is_note": True,
            "anchor_offset_start": 0, "anchor_offset_end": 5,
        },
        cookies=cookies,
    )
    assert note_resp.status_code == 201
    note_id = note_resp.json()["session_id"]

    # List sessions scoped to the parent document (includes reference children).
    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}&is_note=true",
        cookies=cookies,
    )
    assert resp.status_code == 200
    sessions = resp.json()
    note = next((s for s in sessions if s["session_id"] == note_id), None)
    assert note is not None, "Note must appear in parent document scope"
    assert note["reference_id"] == ref_id, \
        f"Note on reference must expose reference_id={ref_id}, got {note['reference_id']}"
    # INVARIANT: a reference-scoped session's document_id stays the reference (NOT
    # remapped to its owning document); reference_id carries the grouping key.
    assert note["document_id"] == ref_id, \
        f"Note on reference keeps document_id={ref_id}, got {note['document_id']}"
    # Context array is always empty for notes.
    assert note["context_ids"] == []
    # is_note flag preserved.
    assert note["is_note"] is True


# ─── Live preview derivation (plan: notes-system-refactor-image-attachments) ──
# INVARIANT: note pill text derives from messages, never from `title`. This hits
# the REAL list_sessions endpoint (the unit serializer test can't catch an
# id-form mismatch between messages.chat_id and the session RecordID). Pins that
# the grouped query + serializer join correctly across the Surreal boundary.

@pytest.mark.asyncio
async def test_list_notes_derives_previews_from_messages(
    client, admin_user, project_with_doc,
):
    """A note with two messages exposes derived preview fields in the list."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    sess = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid, "document_id": doc_id, "is_note": True,
            "anchor_offset_start": 0, "anchor_offset_end": 5,
        },
        cookies=cookies,
    )
    note_id = sess.json()["session_id"]

    # First message (becomes the pill body / first preview).
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "first message body"},
        cookies=cookies,
    )
    # Second message (becomes the tooltip / last preview, flips message_count>1).
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "second message body"},
        cookies=cookies,
    )

    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}&is_note=true",
        cookies=cookies,
    )
    assert resp.status_code == 200
    note = next((s for s in resp.json() if s["session_id"] == note_id), None)
    assert note is not None
    # The id-form bug would leave these None / 0.
    assert note["message_count"] == 2
    assert note["first_message_preview"] == "first message body"
    assert note["last_message_preview"] == "second message body"
    assert note["last_message_at"] is not None


@pytest.mark.asyncio
async def test_list_notes_single_message_equal_first_last(
    client, admin_user, project_with_doc,
):
    """A single-message note has equal first/last previews and count 1."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    sess = await client.post(
        "/api/chat/sessions",
        json={
            "project_id": pid, "document_id": doc_id, "is_note": True,
            "anchor_offset_start": 0, "anchor_offset_end": 5,
        },
        cookies=cookies,
    )
    note_id = sess.json()["session_id"]
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "only message"},
        cookies=cookies,
    )

    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}&is_note=true",
        cookies=cookies,
    )
    note = next((s for s in resp.json() if s["session_id"] == note_id), None)
    assert note["message_count"] == 1
    assert note["first_message_preview"] == note["last_message_preview"] == "only message"


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
async def test_note_author_edits_own_message(client, admin_user, regular_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, other_token)
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "original"}, cookies={"lore_session": other_token},
        )).json()

        # Author edits own → 200.
        resp = await client.patch(
            f"/api/chat/messages/{msg['message_id']}",
            json={"content": "edited"}, cookies={"lore_session": other_token},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "edited"

        # Author edits own message → 200 (owner-edit case covered separately).
    finally:
        await _revoke_access(pm_id)


@pytest.mark.asyncio
async def test_note_author_edits_own_message_that_has_replies(
    client, admin_user, regular_user, project_with_doc,
):
    """Replies block a non-owner author's DELETE (409), never their EDIT."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, other_token)
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "original"}, cookies={"lore_session": other_token},
        )).json()
        reply = await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "a reply", "parent_id": msg["message_id"]},
            cookies={"lore_session": admin_token},
        )
        assert reply.status_code == 201, reply.text

        resp = await client.patch(
            f"/api/chat/messages/{msg['message_id']}",
            json={"content": "edited"}, cookies={"lore_session": other_token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["content"] == "edited"
    finally:
        await _revoke_access(pm_id)


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
        ids = [m["message_id"] for m in msgs.json()]
        assert m1["message_id"] in ids
        assert m2["message_id"] not in ids
    finally:
        await _revoke_access(pm_id)


@pytest.mark.asyncio
async def test_note_author_delete_non_leaf_rejected(
    client, admin_user, regular_user, project_with_doc,
):
    """A non-owner author may NOT delete a message that has replies (409)."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, other_token)
        m1 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "root"}, cookies={"lore_session": other_token},
        )).json()
        await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "child", "parent_id": m1["message_id"]},
            cookies={"lore_session": other_token},
        )

        resp = await client.delete(
            f"/api/chat/messages/{m1['message_id']}", cookies={"lore_session": other_token},
        )
        assert resp.status_code == 409
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


@pytest.mark.asyncio
async def test_note_owner_edits_others_message(
    client, admin_user, regular_user, project_with_doc,
):
    """The project owner may edit any note message (owner = root)."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, other_token)
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "member wrote"}, cookies={"lore_session": other_token},
        )).json()

        resp = await client.patch(
            f"/api/chat/messages/{msg['message_id']}",
            json={"content": "owner edited"}, cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "owner edited"
    finally:
        await _revoke_access(pm_id)


@pytest.mark.asyncio
async def test_note_owner_deletes_others_message_cascades(
    client, admin_user, regular_user, project_with_doc,
):
    """The project owner may delete any note message with a cascade over replies."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    other_uid, other_token = regular_user

    pm_id = await _grant_access(pid, other_uid, "commentator")
    try:
        note_id = await _make_note(client, pid, doc_id, other_token)
        m1 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "root"}, cookies={"lore_session": other_token},
        )).json()
        m2 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "child", "parent_id": m1["message_id"]},
            cookies={"lore_session": other_token},
        )).json()
        m3 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "grandchild", "parent_id": m2["message_id"]},
            cookies={"lore_session": other_token},
        )).json()

        # Owner deletes m1 (other's, non-leaf) → cascade over the whole subtree.
        resp = await client.delete(
            f"/api/chat/messages/{m1['message_id']}", cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 3

        msgs = await client.get(
            f"/api/chat/sessions/{note_id}/messages", cookies={"lore_session": admin_token},
        )
        ids = {m["message_id"] for m in msgs.json()}
        assert m1["message_id"] not in ids
        assert m2["message_id"] not in ids
        assert m3["message_id"] not in ids
    finally:
        await _revoke_access(pm_id)
