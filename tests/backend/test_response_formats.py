"""Tests for REST API response formats — PATCH returns resource, POST returns full record, GET unwrapped."""


import pytest

_REQUIRED_CHECKPOINT_KEYS = {"checkpoint_id", "document_id", "content", "label", "comment", "created_at"}
_REQUIRED_API_KEY_KEYS = {"key_id", "label", "document_id", "created_at"}


def _assert_has_keys(data: dict, required: set[str]) -> None:
    missing = required - set(data.keys())
    assert not missing, f"Missing keys: {missing}"


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
