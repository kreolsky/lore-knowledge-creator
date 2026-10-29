"""Item 2 — list_messages omits inline base64 images; images lazy-fetched per message.

list_messages must NOT transfer the heavy `images` column (multi-MB base64 data
URIs). Instead it returns `image_count`, and the client lazy-fetches the actual
images from GET /chat/sessions/{sid}/messages/{mid}/images, which returns them in
the SAME data-URI form MessageCreate.images accepts.
"""


IMG_A = "data:image/png;base64,iVBORw0KGgo="


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _create_message(client, token, sid, content, images=None, parent_id=None, role="user"):
    body = {"role": role, "content": content}
    if images is not None:
        body["images"] = images
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json=body,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_images_endpoint_404_for_foreign_message(client, admin_user, project_with_doc):
    """A message id that does not belong to the session must not leak via the path."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid_a = await _create_session(client, token, pid, doc_id)
    sid_b = await _create_session(client, token, pid, doc_id)
    msg = await _create_message(client, token, sid_a, "belongs to A", images=[IMG_A])
    mid = msg["message_id"]

    resp = await client.get(
        f"/api/chat/sessions/{sid_b}/messages/{mid}/images",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404, resp.text
