"""Project-level chat sessions — the server half of agent-first mode.

An AI chat created with neither document_id nor reference_id, and the session
list served without a document. Pinned over the real API+DB stack: the create
gate falls back to the project (404 non-member, 403 below full access), the
document-less list is gated on the project and still serves only the caller's
own threads, and a note — document-owned by contract — is refused without a
document at both ends.
"""


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


def _list_sessions(client, token, params):
    return client.get("/api/chat/sessions", params=params, cookies={"lore_session": token})


async def _set_member(client, admin_token, pid, uid, level):
    resp = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": uid, "access_level": level},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code in (200, 201), resp.text


# TEST-WHY: cross-process-contract — the project-level create and the document-less list are pinned over the real API+DB stack with no patched infra
async def test_project_level_ai_chat_is_created_and_listed(client, admin_user, project_with_doc):
    """An AI chat with neither document_id nor reference_id is created at project
    level — no document, reference, host or agent target on the row — and the
    list without a document serves it, exactly as a document-scoped list does."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    resp = await _post_session(client, token, {"project_id": pid})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    sid = created["session_id"]
    assert created["is_note"] is False
    assert created["document_id"] is None
    assert created["reference_id"] is None
    assert created["host_document_id"] is None
    assert created.get("target_doc_id") is None

    resp = await _list_sessions(client, token, {"project_id": pid})
    assert resp.status_code == 200, resp.text
    listed = {s["session_id"]: s for s in resp.json()}
    assert listed[sid]["document_id"] is None
    assert listed[sid]["is_note"] is False

    # The AI list is project-wide: the same thread from any document of the project.
    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})
    assert resp.status_code == 200, resp.text
    assert sid in {s["session_id"] for s in resp.json()}


# TEST-WHY: cross-process-contract — a note is document-owned at both ends: the create validator and the list's shape refusal are answered over the real stack
async def test_note_without_document_is_refused(client, admin_user, project_with_doc):
    """A note with neither document_id nor reference_id is refused at create
    (422), and a notes listing without a document is refused (400): the note
    path keeps its document gate while the AI path no longer has one."""
    pid, _, _ = project_with_doc
    _, token = admin_user

    resp = await _post_session(client, token, {"project_id": pid, "is_note": True})
    assert resp.status_code == 422, resp.text

    resp = await _list_sessions(client, token, {"project_id": pid, "is_note": True})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "document_id is required to list notes"


# TEST-WHY: refused-principal — a project-level chat is gated on the project itself: a non-member gets the uniform 404 on create and on the document-less list, and a member below full access is refused the create
async def test_project_level_chat_gate_is_the_project(
    client, admin_user, regular_user, project_with_doc,
):
    """Non-member: create and document-less list both answer 404. A commentator
    member may list (own threads only — the admin's chat stays private) but
    cannot create an AI chat: 403. Raised to full, both succeed."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user
    admin_chat = (await _post_session(client, admin_token, {"project_id": pid})).json()["session_id"]

    resp = await _post_session(client, user_token, {"project_id": pid})
    assert resp.status_code == 404, resp.text
    resp = await _list_sessions(client, user_token, {"project_id": pid})
    assert resp.status_code == 404, resp.text

    await _set_member(client, admin_token, pid, user_uid, "commentator")
    resp = await _post_session(client, user_token, {"project_id": pid})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Full project access required for agent mode"
    resp = await _list_sessions(client, user_token, {"project_id": pid})
    assert resp.status_code == 200, resp.text
    assert [s["session_id"] for s in resp.json()] == []

    await _set_member(client, admin_token, pid, user_uid, "full")
    user_chat = (await _post_session(client, user_token, {"project_id": pid})).json()["session_id"]
    resp = await _list_sessions(client, user_token, {"project_id": pid})
    assert resp.status_code == 200, resp.text
    assert {s["session_id"] for s in resp.json()} == {user_chat}
    assert admin_chat not in {s["session_id"] for s in resp.json()}
