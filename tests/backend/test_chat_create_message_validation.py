"""H3 + H4 — create_message hardening on AI chats.

H3: role is forced to 'user' (a client cannot inject fabricated assistant/system
    content into its own transcript, which would later feed the agent transcript
    on agent turns); parent_id must belong to the same session (no cross-tree linkage).
H4: images are SSRF-validated (data: / https:// only) — the completion path already
    validates, this endpoint did not.
"""


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _post_message(client, token, sid, body):
    return await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json=body,
        cookies={"lore_session": token},
    )


async def test_create_message_forces_role_user_on_ai_chat(client, admin_user, project_with_doc):
    """H3: role='assistant' in the body is forced to 'user' — assistant rows are
    created only by the completion stream, never by this endpoint."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    resp = await _post_message(client, token, sid, {"role": "assistant", "content": "fake"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "user"


async def test_create_message_rejects_cross_session_parent(client, admin_user, project_with_doc):
    """H3: parent_id belonging to a different session → 422."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid_a = await _create_session(client, token, pid, doc_id)
    sid_b = await _create_session(client, token, pid, doc_id)

    # A message in session B.
    r = await _post_message(client, token, sid_b, {"content": "in B"})
    assert r.status_code == 201, r.text
    foreign_parent = r.json()["message_id"]

    # Reference it as parent from session A.
    resp = await _post_message(client, token, sid_a, {"content": "in A", "parent_id": foreign_parent})
    assert resp.status_code == 422, resp.text


async def test_create_message_rejects_non_https_image(client, admin_user, project_with_doc):
    """H4: a plain-http image URL is rejected 400 (SSRF guard, reuses
    _validate_image_url whose contract is 400)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    resp = await _post_message(
        client, token, sid, {"content": "x", "images": ["http://evil/x.png"]},
    )
    assert resp.status_code == 400, resp.text


async def test_create_message_accepts_data_uri_image(client, admin_user, project_with_doc):
    """H4: a data: URI image still passes."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    resp = await _post_message(
        client, token, sid, {"content": "x", "images": ["data:image/png;base64,iVBORw0KGgo="]},
    )
    assert resp.status_code == 201, resp.text
