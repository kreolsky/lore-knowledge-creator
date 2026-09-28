"""Integration tests for API key routes."""

import pytest


@pytest.mark.asyncio
async def test_create_api_key(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Create a doc for the key
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "KeyDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "test-key"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["label"] == "test-key"
    assert data["token"].startswith("lore_")
    assert data["key_id"]


@pytest.mark.asyncio
async def test_list_api_keys(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "ListKeyDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "key1"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/api-keys?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    keys = resp.json()
    assert any(k["label"] == "key1" for k in keys)


@pytest.mark.asyncio
async def test_rename_api_key(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "RenameKeyDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "old-name"},
        cookies={"lore_session": token},
    )
    key_id = resp.json()["key_id"]
    resp = await client.patch(
        f"/api/api-keys/{key_id}",
        json={"label": "new-name"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_api_key(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "DelKeyDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "to-delete"},
        cookies={"lore_session": token},
    )
    key_id = resp.json()["key_id"]
    resp = await client.delete(f"/api/api-keys/{key_id}", cookies={"lore_session": token})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_key_widget_auth(client, admin_user, project_with_doc):
    """Test that a created API key can authenticate widget endpoints."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "WidgetDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "widget-key"},
        cookies={"lore_session": token},
    )
    api_token = resp.json()["token"]
    # Use the API key to check widget status (should get 404 for non-existent ref, not 401)
    resp = await client.get(
        "/api/widget/status/nonexistent-ref",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 404  # auth worked, ref not found
