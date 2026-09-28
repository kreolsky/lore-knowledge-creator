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


async def test_deleted_ref_chat_excluded_from_document_scope(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    r1 = await _create_ref(client, token, pid, doc_id)
    ref_chat = (await _post_session(client, token, {"project_id": pid, "document_id": r1})).json()["session_id"]
    resp = await client.delete(f"/api/chat/sessions/{ref_chat}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text

    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})
    ids = {s["session_id"] for s in resp.json()}
    assert ref_chat not in ids


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


async def _create_doc(client, token, pid, parent_id=None, title="doc"):
    body = {
        "project_id": pid,
        "parent_id": parent_id,
        "title": title,
        "media_type": "markdown",
        "content": "x",
    }
    resp = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def test_project_wide_ai_visibility(client, admin_user, project_with_doc):
    """Decision: AI chats are visible project-wide. A chat owned by doc A is visible
    when listing from doc B (and vice versa) — the open document is no longer a
    server-side visibility filter."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    doc_b = await _create_doc(client, token, pid, title="Doc B")

    chat_a = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    chat_b = (await _post_session(client, token, {"project_id": pid, "document_id": doc_b})).json()["session_id"]

    # Listing from doc A sees BOTH chats; listing from doc B sees BOTH chats.
    from_a = {s["session_id"] for s in (await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})).json()}
    from_b = {s["session_id"] for s in (await _list_sessions(client, token, {"project_id": pid, "document_id": doc_b})).json()}

    assert from_a >= {chat_a, chat_b}
    assert from_b >= {chat_a, chat_b}


async def test_document_title_labels_owning_document(client, admin_user, project_with_doc):
    """A document-session's document_title is its OWN title (thin client). A ref-
    scoped chat carries NO document_title — its parent IS the reference, labelled
    from reference_title (the ref's own title). INVARIANT: a reference-scoped chat's
    parent is the reference, never its owning document (user rule 2026-07-09)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    # Name the owning document so the label is verifiable.
    await client.patch(
        f"/api/documents/{doc_id}", json={"title": "Owner Doc"},
        cookies={"lore_session": token},
    )
    r1 = await _create_ref(client, token, pid, doc_id)

    doc_chat = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    ref_chat = (await _post_session(client, token, {"project_id": pid, "reference_id": r1})).json()["session_id"]

    rows = (await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})).json()
    by_id = {s["session_id"]: s for s in rows}
    # Doc-owned chat → the document's title.
    assert by_id[doc_chat]["document_title"] == "Owner Doc"
    assert by_id[doc_chat]["reference_title"] is None
    # Ref-owned chat → labelled from the reference's OWN title, not the owning doc.
    assert by_id[ref_chat]["document_title"] is None
    assert by_id[ref_chat]["reference_title"] == "ref"


async def test_project_wide_privacy_scoped_by_user(client, admin_user, regular_user, project_with_doc):
    """Project-wide still means 'all MY chats' — another member's AI chat is never
    visible (AI chats carry user_id + owner-only access)."""
    pid, doc_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    doc_b = await _create_doc(client, admin_token, pid, title="Doc B")
    admin_chat_b = (await _post_session(client, admin_token, {"project_id": pid, "document_id": doc_b})).json()["session_id"]

    # Regular user lists from EITHER document and never sees admin's chat on doc B.
    from_a = {s["session_id"] for s in (await _list_sessions(client, user_token, {"project_id": pid, "document_id": doc_id})).json()}
    from_b = {s["session_id"] for s in (await _list_sessions(client, user_token, {"project_id": pid, "document_id": doc_b})).json()}
    assert admin_chat_b not in from_a
    assert admin_chat_b not in from_b
