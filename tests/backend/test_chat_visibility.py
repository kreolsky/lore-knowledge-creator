"""Tests for chat visibility scope:

AI chats (is_note false) — project-wide: every AI chat the user owns in a project is
visible from ANY document in that project (flat list per (project, user)). The open
document/reference only affects ghost-attach + the resolver's active-chat pick, never
the server-side visibility filter. Privacy holds automatically (AI chats carry
user_id + owner-only access).

Note-chats (is_note true) keep their branch scope (ancestor/sibling/children) — served
by the separate is_note=true branch in list_sessions.
"""


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


def _list_sessions(client, token, params):
    return client.get("/api/chat/sessions", params=params, cookies={"lore_session": token})


async def _create_ref(client, token, pid, document_id):
    body = {
        "project_id": pid,
        "parent_id": document_id,
        "title": "ref",
        "media_type": "markdown",
        "is_reference": True,
        "content": "x",
    }
    resp = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def test_reference_sees_own_and_parent_sibling_chats(client, admin_user, project_with_doc):
    """A reference sees its own chats + parent doc chats + sibling ref chats."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    r1 = await _create_ref(client, token, pid, doc_id)
    r2 = await _create_ref(client, token, pid, doc_id)
    doc_chat = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    ref_chat_1 = (await _post_session(client, token, {"project_id": pid, "document_id": r1})).json()["session_id"]
    ref_chat_2 = (await _post_session(client, token, {"project_id": pid, "reference_id": r2})).json()["session_id"]

    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": r1})
    assert resp.status_code == 200
    ids = {s["session_id"] for s in resp.json()}
    assert ref_chat_1 in ids
    assert ref_chat_2 in ids
    assert doc_chat in ids


async def test_other_user_chats_not_visible(client, admin_user, regular_user, project_with_doc):
    """Visibility join must still scope by user_id."""
    pid, doc_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    # grant access to regular user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    r1 = await _create_ref(client, admin_token, pid, doc_id)
    admin_ref_chat = (await _post_session(client, admin_token, {"project_id": pid, "document_id": r1})).json()["session_id"]

    resp = await _list_sessions(client, user_token, {"project_id": pid, "document_id": doc_id})
    ids = {s["session_id"] for s in resp.json()}
    assert admin_ref_chat not in ids
