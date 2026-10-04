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


@pytest.mark.asyncio
async def test_widget_upload(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    audio_bytes = b"\x1aE\xdf\xa3" + b"\x00" * 96
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("rec.webm", io.BytesIO(audio_bytes), "audio/webm")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["reference_id"]


@pytest.mark.asyncio
async def test_widget_upload_attributes_key_owner_as_author(
    client, admin_user, project_with_doc,
):
    """The widget key carries a human principal (its owner — the same user
    transcription is enqueued for), so the upload IS attributed (review fix R4)."""
    from db import fetch_one

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("rec.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200
    row = await fetch_one("documents", resp.json()["reference_id"])
    assert row["created_by"] == admin_uid
    assert row["created_by_name"] == "testadmin"


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
async def test_widget_upload_emits_reference_created(client, admin_user, project_with_doc):
    """Widget upload emits reference_created so project WS clients are notified."""
    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)

    events = []
    async def capture(**kw):
        events.append(kw)

    on("reference_created", capture)
    try:
        resp = await client.post(
            "/api/widget/upload",
            files={"file": ("rec.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
            headers={"Authorization": f"Bearer {api_token}"},
        )
        assert resp.status_code == 200
        ref_id = resp.json()["reference_id"]
        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
        assert events[0]["project_id"] == pid
        assert events[0]["document_id"] == doc_id
    finally:
        off("reference_created", capture)


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


@pytest.mark.asyncio
async def test_widget_call_after_project_delete_410(client, admin_user, project_with_doc):
    """A widget key on a doc whose PROJECT was deleted answers 410 — project
    delete marks only the project row (the document keeps deleted_at = NONE),
    so the document liveness check alone would keep a dead project's widget
    alive. Same 410 family as the deleted-document case."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)

    # False-green check: the key works BEFORE the delete.
    resp = await client.get(
        "/api/widget/info",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200

    resp = await client.delete(f"/api/projects/{pid}", cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        "/api/widget/info",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 410
