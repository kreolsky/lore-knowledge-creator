"""Tests for document history hooks, REST endpoint, and export."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_reference_creation_no_history(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": idx_id,
            "title": "Ref",
            "media_type": "markdown",
            "is_reference": True,
            "content": "ref text",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    ref_id = resp.json()["document_id"]

    resp = await client.get(f"/api/documents/{ref_id}/history", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_history_requires_read_access(client, admin_user, regular_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    _, user_token = regular_user
    resp = await client.get(
        f"/api/documents/{idx_id}/history",
        cookies={"lore_session": user_token},
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_export_docx(client, admin_user, project_with_doc, http_pool):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Export Doc", "content": "Hello **world**"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]

    fake_resp = AsyncMock()
    fake_resp.status_code = 200
    fake_resp.content = b"PK\x03\x04fake-docx"
    fake_resp.text = ""

    mock_instance = AsyncMock()
    mock_instance.post = AsyncMock(return_value=fake_resp)
    http_pool("docx", mock_instance)

    resp = await client.get(
        f"/api/documents/{doc_id}/export?format=docx",
        cookies={"lore_session": token},
    )

    assert resp.status_code == 200
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in resp.headers.get("content-type", "")
    cd = resp.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in cd
    assert "Export%20Doc.docx" in cd
