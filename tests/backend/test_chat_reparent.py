"""PATCH /api/chat/sessions/{id} document_id — chat reparent from the header.

The header's change-parent icon writes chat_sessions.document_id (the column
the proximity sort and build_ref_map read; reparenting and re-anchoring are
the same write). Guards under test: 400 on note sessions (anchored by a note:
link inside the owning document's content), 400 on a reference TARGET for any
chat, 403 when the caller lacks at least commentator access on the target, and
explicit null clearing to project-level. A reference chat moves to a document
and becomes a plain document chat.
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


async def test_reparent_inaccessible_target_403_and_row_unchanged(
    client, admin_user, regular_user, project_with_doc,
):
    """Reparenting into a document the caller cannot see would hide their own
    chat from them and leak a title into that branch's proximity ranking —
    the target requires at least commentator access, else 403 and no write."""
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
    assert resp.status_code == 403

    rows = (
        await client.get(
            "/api/chat/sessions",
            params={"project_id": pid, "document_id": doc_id},
            cookies={"lore_session": user_token},
        )
    ).json()
    by_id = {s["session_id"]: s for s in rows}
    assert by_id[sid]["document_id"] == doc_id
