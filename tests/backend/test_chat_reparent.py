"""PATCH /api/chat/sessions/{id} document_id — chat reparent from the header.

The header's change-parent icon writes chat_sessions.document_id (the column
the proximity sort and build_ref_map read; reparenting and re-anchoring are
the same write). Guards under test: 400 on note sessions (anchored by a note:
link inside the owning document's content), 400 on a reference TARGET for any
chat, a uniform 404 on a target of ANOTHER project whether or not the caller
is a member there, 403 when the caller lacks at least commentator access on
the target, and explicit null clearing to project-level. A reference chat
moves to a document and becomes a plain document chat.
"""


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


def _patch_session(client, token, sid, body):
    return client.patch(f"/api/chat/sessions/{sid}", json=body, cookies={"lore_session": token})


async def _create_doc(client, token, pid, title="doc"):
    body = {
        "project_id": pid,
        "title": title,
        "media_type": "markdown",
        "content": "x",
    }
    resp = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


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


async def test_reparent_reference_session_to_document(client, admin_user, project_with_doc):
    """A reference chat moves to any document and becomes a plain doc chat —
    the column now holds the document id, so reference_id derives to None."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    doc_b = await _create_doc(client, token, pid, title="Doc B")
    r1 = await _create_ref(client, token, pid, doc_id)
    sid = (await _post_session(client, token, {"project_id": pid, "reference_id": r1})).json()["session_id"]

    resp = await _patch_session(client, token, sid, {"document_id": doc_b})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["document_id"] == doc_b
    assert data["reference_id"] is None

    rows = (
        await client.get(
            "/api/chat/sessions",
            params={"project_id": pid, "document_id": doc_b},
            cookies={"lore_session": token},
        )
    ).json()
    by_id = {s["session_id"]: s for s in rows}
    assert by_id[sid]["reference_id"] is None


async def test_reparent_reference_session_to_project_level(client, admin_user, project_with_doc):
    """Explicit null detaches a reference chat to project level, like a doc chat."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    r1 = await _create_ref(client, token, pid, doc_id)
    sid = (await _post_session(client, token, {"project_id": pid, "reference_id": r1})).json()["session_id"]

    resp = await _patch_session(client, token, sid, {"document_id": None})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("document_id") is None
    assert resp.json().get("reference_id") is None


# TEST-WHY: cross-process-contract — the project wall's uniform 404 is pinned over the real
# API+DB stack with no patched infra: missing, deleted and cross-project targets are ONE answer
async def test_reparent_target_in_another_project_404_and_row_unchanged(
    client, admin_user, regular_user, project_with_doc,
    ):
    """A document of a project the caller does not belong to answers the same
    uniform 404 as a document that does not exist: the answer names no project
    and confirms no document, and the row keeps its parent."""
    pid, doc_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    # A second project the regular user is NOT a member of holds the target.
    other_pid = (
        await client.post("/api/projects", json={"name": "Reparent Target"}, cookies={"lore_session": admin_token})
    ).json()["project_id"]
    target = await _create_doc(client, admin_token, other_pid, title="Foreign Doc")

    sid = (
        await _post_session(client, user_token, {"project_id": pid, "document_id": doc_id})
    ).json()["session_id"]

    resp = await _patch_session(client, user_token, sid, {"document_id": target})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Target document not found"

    rows = (
        await client.get(
            "/api/chat/sessions",
            params={"project_id": pid, "document_id": doc_id},
            cookies={"lore_session": user_token},
        )
    ).json()
    by_id = {s["session_id"]: s for s in rows}
    assert by_id[sid]["document_id"] == doc_id


# TEST-WHY: refused-principal — a member of BOTH projects could move a chat onto the other project's document; the target check now answers the same 404 a non-member gets, and a member lowered to readonly still gets the 403
async def test_reparent_onto_accessible_document_of_another_project_404(
    client, admin_user, regular_user, project_with_doc,
    ):
    """The caller CAN read the target (a full member of its project too) — the
    refusal is the project rule, not access: a chat moves only within its own
    project, and the row keeps its parent. The access rule then still holds on
    its own: lowered to readonly in the chat's project, the caller cannot move
    the chat onto a document of THAT project either."""
    pid, doc_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    other_pid = (
        await client.post("/api/projects", json={"name": "Second Project"}, cookies={"lore_session": admin_token})
    ).json()["project_id"]
    for member_of in (pid, other_pid):
        resp = await client.post(
            f"/api/admin/projects/{member_of}/members",
            json={"user_id": user_uid, "access_level": "full"},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code in (200, 201), resp.text
    target = await _create_doc(client, admin_token, other_pid, title="Second Project Doc")
    doc_b = await _create_doc(client, admin_token, pid, title="Doc B")
    # The refused principal is genuinely allowed to see the target.
    readable = await client.get(f"/api/documents/{target}", cookies={"lore_session": user_token})
    assert readable.status_code == 200, readable.text

    sid = (
        await _post_session(client, user_token, {"project_id": pid, "document_id": doc_id})
    ).json()["session_id"]
    resp = await _patch_session(client, user_token, sid, {"document_id": target})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Target document not found"

    resp = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code in (200, 201), resp.text
    resp = await _patch_session(client, user_token, sid, {"document_id": doc_b})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Commentator access required on the target document"

    rows = (
        await client.get(
            "/api/chat/sessions",
            params={"project_id": pid, "document_id": doc_id},
            cookies={"lore_session": user_token},
        )
    ).json()
    by_id = {s["session_id"]: s for s in rows}
    assert by_id[sid]["document_id"] == doc_id
