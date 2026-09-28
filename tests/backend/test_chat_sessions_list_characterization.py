"""Characterization test for list_sessions (plan chat-debt-paydown 3.3 refactor).

Pure-move refactor safety net: this pins three list_sessions behaviors that the
helper extraction MUST NOT change. Written and confirmed GREEN against the
current monolithic list_sessions BEFORE the decomposition, then kept green under it.

Pins:
  * note preview fields derived from messages (first/last/count/last_message_at),
  * last-activity ordering (newest message first) across AI sessions,
  * the with_active_messages piggyback for a preferred_session_id on the page.
"""

import pytest


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _list(client, token, pid, doc_id, **params):
    q = {"project_id": pid, "document_id": doc_id, **params}
    resp = await client.get("/api/chat/sessions", params=q, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_list_sessions_note_preview_ordering_and_piggyback(
    client, admin_user, project_with_doc
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    # Two AI sessions; s_newer gets the later message so it sorts FIRST by last
    # activity.
    s_older = await _create_session(client, token, pid, doc_id)
    s_newer = await _create_session(client, token, pid, doc_id)
    await client.post(
        f"/api/chat/sessions/{s_older}/messages",
        json={"role": "user", "content": "older ai msg"}, cookies=cookies,
    )
    assert (await client.post(
        f"/api/chat/sessions/{s_newer}/messages",
        json={"role": "user", "content": "newer ai msg"}, cookies=cookies,
    )).status_code == 201

    # One note session with two messages (first + last preview distinct).
    note = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies=cookies,
    )
    assert note.status_code == 201, note.text
    note_id = note.json()["session_id"]
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "note first"}, cookies=cookies,
    )
    await client.post(
        f"/api/chat/sessions/{note_id}/messages",
        json={"content": "note last"}, cookies=cookies,
    )

    # 1. Note list: preview fields DERIVED from messages (not the stale title).
    notes = await _list(client, token, pid, doc_id, is_note="true")
    assert isinstance(notes, list)
    note_row = next(n for n in notes if n["session_id"] == note_id)
    assert note_row["message_count"] == 2
    assert note_row["first_message_preview"] == "note first"
    assert note_row["last_message_preview"] == "note last"
    assert note_row["last_message_at"] is not None

    # 2. AI list: last-activity ordering — the newer-message session sorts first.
    ai = await _list(client, token, pid, doc_id)
    assert isinstance(ai, list)
    ids = [s["session_id"] for s in ai]
    assert ids.index(s_newer) < ids.index(s_older)
    newer_row = next(s for s in ai if s["session_id"] == s_newer)
    assert newer_row["last_message_at"] is not None

    # 3. Piggyback: a valid preferred_session_id on the page returns its messages
    # in one round-trip (active_messages non-null, scoped to that session).
    data = await _list(client, token, pid, doc_id,
                       with_active_messages="true", preferred_session_id=s_newer)
    assert isinstance(data, dict)
    am = data["active_messages"]
    assert am["session_id"] == s_newer
    assert [m["content"] for m in am["messages"]] == ["newer ai msg"]
