"""Tests for chat session context persistence — unified context_ids stored
per-session in chat_sessions, updatable via PATCH, not inherited on session
creation."""



def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


def _patch_session(client, token, sid, body):
    return client.patch(f"/api/chat/sessions/{sid}", json=body, cookies={"lore_session": token})


def _list_sessions(client, token, params):
    return client.get("/api/chat/sessions", params=params, cookies={"lore_session": token})


async def test_create_session_has_empty_context(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["context_ids"] == []


async def test_patch_context_ids_persists(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    sid = resp.json()["session_id"]
    resp = await _patch_session(client, token, sid, {
        "context_ids": ["d1", "d2"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["context_ids"] == ["d1", "d2"]
    # Verify it's persisted by re-listing
    resp = await _list_sessions(client, token, {"project_id": pid, "document_id": doc_id})
    sessions = resp.json()
    found = next(s for s in sessions if s["session_id"] == sid)
    assert found["context_ids"] == ["d1", "d2"]


async def test_patch_empty_context_clears(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    sid = resp.json()["session_id"]
    await _patch_session(client, token, sid, {"context_ids": ["d1"]})
    resp = await _patch_session(client, token, sid, {"context_ids": []})
    assert resp.status_code == 200
    assert resp.json()["context_ids"] == []


async def test_patch_omitted_context_unchanged(client, admin_user, project_with_doc):
    """Omitting context_ids in PATCH must not clear existing values."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    sid = resp.json()["session_id"]
    await _patch_session(client, token, sid, {"context_ids": ["d1", "d2"]})
    # PATCH only title — context must remain
    resp = await _patch_session(client, token, sid, {"title": "Renamed"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Renamed"
    assert data["context_ids"] == ["d1", "d2"]


async def test_context_not_inherited_on_new_session(client, admin_user, project_with_doc):
    """Creating a new chat must NOT copy context from previous chat."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    sid1 = resp.json()["session_id"]
    await _patch_session(client, token, sid1, {"context_ids": ["d1", "d2"]})
    # Second session — context should be empty
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    data = resp.json()
    assert data["context_ids"] == []
