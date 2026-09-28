"""Tests for REST API response formats — PATCH returns resource, POST returns full record, GET unwrapped."""


import pytest

_REQUIRED_DOC_KEYS = {"document_id", "project_id", "title", "content", "path", "created_at", "updated_at"}
_REQUIRED_REF_KEYS = {"reference_id", "project_id", "title", "media_type", "content", "created_at", "updated_at"}
_REQUIRED_NOTE_KEYS = {"note_id", "content", "author_id", "created_at"}
_REQUIRED_PROJECT_KEYS = {"project_id", "name", "index_doc_id", "owner_id", "is_public", "created_at"}
_REQUIRED_CHECKPOINT_KEYS = {"checkpoint_id", "document_id", "content", "label", "comment", "created_at"}
_REQUIRED_API_KEY_KEYS = {"key_id", "label", "document_id", "created_at"}


def _assert_has_keys(data: dict, required: set[str]) -> None:
    missing = required - set(data.keys())
    assert not missing, f"Missing keys: {missing}"


# ─── GET /documents/{id} — unwrapped ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_document_returns_flat_resource(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.get(f"/api/documents/{idx_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_DOC_KEYS)
    assert "headings" in data
    assert "document" not in data, "Response should NOT be wrapped in {document: ...}"


# ─── POST /documents — full serialized record ───────────────────────────────


@pytest.mark.asyncio
async def test_create_document_returns_full_record(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Full Record", "content": "some text"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_DOC_KEYS)
    assert data["title"] == "Full Record"
    assert data["content"] == "some text"
    assert data["project_id"] == pid


# ─── PATCH /documents/{id} — returns updated resource ───────────────────────


@pytest.mark.asyncio
async def test_patch_document_content_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "PatchContent"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "# Hello\n\nWorld"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_DOC_KEYS)
    assert data["document_id"] == doc_id
    assert data["content"] == "# Hello\n\nWorld"
    assert "headings" in data
    assert len(data["headings"]) == 1
    assert data["headings"][0]["text"] == "Hello"
    assert "success" not in data


@pytest.mark.asyncio
async def test_patch_document_title_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Old Title"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"title": "New Title"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_DOC_KEYS)
    assert data["document_id"] == doc_id
    assert data["title"] == "New Title"
    assert "headings" in data
    assert "success" not in data


@pytest.mark.asyncio
async def test_patch_document_parent_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Parent Doc"},
        cookies={"lore_session": token},
    )
    parent_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Child Doc"},
        cookies={"lore_session": token},
    )
    child_id = resp.json()["document_id"]

    resp = await client.patch(
        f"/api/documents/{child_id}",
        json={"parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_DOC_KEYS)
    assert data["document_id"] == child_id
    assert data["parent_id"] == parent_id
    assert "success" not in data


@pytest.mark.asyncio
async def test_patch_document_auto_backup_included(client, admin_user, project_with_doc, enqueue_recorder):
    """Content-loss PATCH returns the inline auto_backup in the response (REST path
    stays inline so the no-collab client can surface the snapshot — see plan #6)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Backup Test", "content": "A" * 2000},
        cookies=cookies,
    )
    doc_id = resp.json()["document_id"]

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "short"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_DOC_KEYS)
    assert "auto_backup_loss_task" not in enqueue_recorder.names()
    assert data.get("auto_backup")


# ─── PATCH /references/{id} — returns updated resource ──────────────────────


@pytest.mark.asyncio
async def test_patch_reference_content_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "Patch Ref", "media_type": "markdown", "content": "old"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]

    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"content": "new content"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_REF_KEYS)
    assert data["reference_id"] == ref_id
    assert data["content"] == "new content"
    assert "success" not in data


@pytest.mark.asyncio
async def test_patch_reference_title_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "Old Ref", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]

    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"title": "New Ref"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_REF_KEYS)
    assert data["reference_id"] == ref_id
    assert data["title"] == "New Ref"
    assert "success" not in data


# ─── PATCH /notes/{id} — removed (notes are now chat_sessions) ───────────────


# ─── PATCH /projects/{id} — returns updated resource ────────────────────────


@pytest.mark.asyncio
async def test_patch_project_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"name": "Renamed Project"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_PROJECT_KEYS)
    assert data["project_id"] == pid
    assert data["name"] == "Renamed Project"
    assert "my_access" in data
    assert data["my_access"] == "full"
    assert "success" not in data


@pytest.mark.asyncio
async def test_patch_project_toggle_public(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"is_public": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_public"] is True
    assert "success" not in data


# ─── PATCH /checkpoints/{id} — returns updated resource ─────────────────────


@pytest.mark.asyncio
async def test_patch_checkpoint_returns_resource(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Checkpoint Test"},
        cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "test-cp", "comment": "original"},
        cookies=cookies,
    )
    cp_id = resp.json()["checkpoint_id"]

    resp = await client.patch(
        f"/api/checkpoints/{cp_id}",
        json={"comment": "updated"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_CHECKPOINT_KEYS)
    assert data["checkpoint_id"] == cp_id
    assert data["comment"] == "updated"
    assert data["label"] == "test-cp"
    assert "success" not in data


# ─── PATCH /api-keys/{id} — returns updated resource ────────────────────────


@pytest.mark.asyncio
async def test_patch_api_key_returns_resource(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post(
        "/api/api-keys",
        json={"document_id": idx_id, "label": "old label"},
        cookies=cookies,
    )
    key_id = resp.json()["key_id"]

    resp = await client.patch(
        f"/api/api-keys/{key_id}",
        json={"label": "new label"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_API_KEY_KEYS)
    assert data["key_id"] == key_id
    assert data["label"] == "new label"
    assert "success" not in data


# ─── POST /projects — full serialized record ────────────────────────────────


@pytest.mark.asyncio
async def test_create_project_returns_full_record(client, admin_user):
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "New Project"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_PROJECT_KEYS)
    assert data["name"] == "New Project"
    assert data["my_access"] == "full"
    assert data["is_public"] is False
    assert data["owner_id"] is not None
    assert data["index_doc_id"] is not None


# ─── POST /references — already returns full record (serialize_record) ──────


@pytest.mark.asyncio
async def test_create_reference_returns_full_record(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "New Ref", "media_type": "markdown", "content": "text"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    _assert_has_keys(data, _REQUIRED_REF_KEYS)
    assert data["title"] == "New Ref"
    assert data["content"] == "text"


# ─── Validation (Phase 3) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_document_empty_title_rejected(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": ""},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


# Note: note validation tests removed — notes are now chat_sessions.
