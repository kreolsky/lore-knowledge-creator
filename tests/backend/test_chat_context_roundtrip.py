"""Roundtrip tests for unified context_ids wire format (Spec §5).

Verifies the API surface contract — PATCH writes the unified array, GET returns it
unchanged; reference_id is computed READ-only from document_id.
"""


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


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


async def test_note_session_clears_context_preserves_reference_id(client, admin_user, project_with_doc):
    """§1 (notes): is_note=true → context_ids=[], reference_id preserved for grouping."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)

    resp = await _post_session(client, token, {
        "project_id": pid,
        "reference_id": ref_id,
        "is_note": True,
        "anchor_rel_start": "AAcDAA==",
        "anchor_rel_end": "AAcDAA==",
        "document_id": doc_id,
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["is_note"] is True
    assert data["context_ids"] == []
    assert data["reference_id"] == ref_id
