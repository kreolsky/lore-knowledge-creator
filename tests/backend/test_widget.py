"""Integration tests for widget routes (API key auth)."""

import io

import pytest


async def _create_api_key(client, admin_token, project_id):
    """Helper: create a doc + API key, return (doc_id, api_token)."""
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": "WidgetTestDoc"},
        cookies={"lore_session": admin_token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": "widget-test"},
        cookies={"lore_session": admin_token},
    )
    return doc_id, resp.json()["token"]


# ─── Upload via the shared import dispatcher
# The widget accepts ANY supported file now; every kind lands as a reference
# under the key's bound document. ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_widget_upload_csv_lands_as_text_ref_not_table(
    client, admin_user, project_with_doc,
):
    """CSV/TSV via the widget lands as a TEXT reference — table dispatch exists
    only in the browser (useReferenceUpload). Pinned so the live drive does not
    expect a table."""
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("data.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200, resp.text
    ref = await fetch_one("documents", resp.json()["reference_id"])
    assert ref["media_type"] == "markdown"
    assert "a,b" in ref["content"]


@pytest.mark.asyncio
async def test_widget_upload_image_lands_as_image_ref(client, admin_user, project_with_doc):
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("shot.png", io.BytesIO(png), "image/png")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200, resp.text
    ref = await fetch_one("documents", resp.json()["reference_id"])
    assert ref["media_type"] == "image"


@pytest.mark.asyncio
async def test_widget_status_pending(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    # Upload first to get a reference
    audio_bytes = b"\x1aE\xdf\xa3" + b"\x00" * 96
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("rec.webm", io.BytesIO(audio_bytes), "audio/webm")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    ref_id = resp.json()["reference_id"]
    # Check status
    resp = await client.get(
        f"/api/widget/status/{ref_id}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["content"] is None  # not ready yet


@pytest.mark.asyncio
async def test_widget_status_unauthorized(client):
    resp = await client.get(
        "/api/widget/status/some-ref",
        headers={"Authorization": "Bearer invalid_token_here"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_widget_info(client, admin_user, project_with_doc):
    """Info endpoint returns the key's target project and document names."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.get(
        "/api/widget/info",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == pid
    assert data["project_name"] == "Test Project"
    assert data["document_id"] == doc_id
    assert data["document_title"] == "WidgetTestDoc"


@pytest.mark.asyncio
async def test_widget_info_unauthorized(client):
    resp = await client.get(
        "/api/widget/info",
        headers={"Authorization": "Bearer invalid_token_here"},
    )
    assert resp.status_code == 401
