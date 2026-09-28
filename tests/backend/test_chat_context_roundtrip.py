"""Roundtrip tests for unified context_ids wire format (Spec §5).

Verifies the API surface contract — PATCH writes the unified array, GET returns it
unchanged; reference_id is computed READ-only from document_id.
"""



def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


def _patch_session(client, token, sid, body):
    return client.patch(f"/api/chat/sessions/{sid}", json=body, cookies={"lore_session": token})


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


async def test_patch_context_ids_persists_unified(client, admin_user, project_with_doc):
    """§5: PATCH {context_ids: [d1, r1]} → GET returns context_ids: [d1, r1]."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)

    sid = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    resp = await _patch_session(client, token, sid, {"context_ids": [doc_id, ref_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["context_ids"] == [doc_id, ref_id]

    # Re-list, confirm order preserved
    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})
    found = next(s for s in resp.json() if s["session_id"] == sid)
    assert found["context_ids"] == [doc_id, ref_id]


async def test_patch_empty_clears_context(client, admin_user, project_with_doc):
    """§5: {context_ids: []} → GET returns []."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    await _patch_session(client, token, sid, {"context_ids": [doc_id]})
    resp = await _patch_session(client, token, sid, {"context_ids": []})
    assert resp.status_code == 200
    assert resp.json()["context_ids"] == []


async def test_patch_omitted_preserves_context(client, admin_user, project_with_doc):
    """§5: PATCH title only → context unchanged."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    await _patch_session(client, token, sid, {"context_ids": [doc_id]})
    resp = await _patch_session(client, token, sid, {"title": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["context_ids"] == [doc_id]
    assert resp.json()["title"] == "Renamed"


async def test_response_splits_reference_id_for_ref_doc_session(client, admin_user, project_with_doc):
    """§1: Session on ref-doc → reference_id populated, document_id stays the ref
    (a reference IS the chat's parent — NOT remapped to the owning document), and
    reference_title carries the reference's OWN title."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)

    sid = (await _post_session(client, token, {"project_id": pid, "reference_id": ref_id})).json()["session_id"]
    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": ref_id})
    found = next(s for s in resp.json() if s["session_id"] == sid)
    assert found["reference_id"] == ref_id
    assert found["document_id"] == ref_id
    assert found["reference_title"] == "ref"


async def test_response_null_reference_id_for_normal_doc_session(client, admin_user, project_with_doc):
    """§1: Session on a regular document → reference_id is null."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})
    found = next(s for s in resp.json() if s["session_id"] == sid)
    assert found["reference_id"] is None
    assert found["document_id"] == doc_id


async def test_note_session_clears_context_preserves_reference_id(client, admin_user, project_with_doc):
    """§1 (notes): is_note=true → context_ids=[], reference_id preserved for grouping."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)

    resp = await _post_session(client, token, {
        "project_id": pid,
        "reference_id": ref_id,
        "is_note": True,
        "anchor_offset_start": 0,
        "anchor_offset_end": 1,
        "document_id": doc_id,
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["is_note"] is True
    assert data["context_ids"] == []
    assert data["reference_id"] == ref_id


async def test_empty_ref_map_safe_for_all_doc_context(client, admin_user, project_with_doc):
    """Defensive: context with no ref-doc IDs → no crash, all entries treated as docs."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {"project_id": pid, "document_id": doc_id})).json()["session_id"]
    resp = await _patch_session(client, token, sid, {"context_ids": [doc_id]})
    assert resp.status_code == 200
    assert resp.json()["context_ids"] == [doc_id]
