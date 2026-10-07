"""Integration tests for widget routes (API key auth)."""

import io
from unittest.mock import AsyncMock, patch

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
    # the upload goes through the shared
    # import dispatcher, so the status VALUE is "applied" (the Rust client
    # deserializes the field but never reads its value — it polls by id).
    assert data["status"] == "applied"
    assert data["reference_id"]
    assert set(data.keys()) == {"reference_id", "status"}


# ─── Upload via the shared import dispatcher
# The widget accepts ANY supported file now; every kind lands as a reference
# under the key's bound document. ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_widget_upload_markdown_lands_as_text_ref(client, admin_user, project_with_doc):
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("notes.md", io.BytesIO("# Field notes\n\nBody".encode()), "text/markdown")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data.keys()) == {"reference_id", "status"}
    ref = await fetch_one("documents", data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["media_type"] == "markdown"
    # The collab record factory leaves processing_status UNSET for a text
    # reference — its content IS the body, nothing is pending derivation
    # (absent = None = "no derived text expected").
    assert ref.get("processing_status") is None
    assert ref["parent_id"] == doc_id
    assert "Field notes" in ref["content"]


@pytest.mark.asyncio
async def test_widget_upload_over_cap_is_413_and_creates_nothing(
    client, admin_user, project_with_doc,
):
    """A part past its category cap is refused while it is being read — 413,
    and no reference lands under the key's document."""
    from db import get_db

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.put(
        "/api/admin/settings/MAX_MARKDOWN_SIZE_MB", json={"value": 1},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    try:
        resp = await client.post(
            "/api/widget/upload",
            files={"file": ("big.md", io.BytesIO(b"a" * (1024 * 1024 + 1)), "text/markdown")},
            headers={"Authorization": f"Bearer {api_token}"},
        )
    finally:
        await client.delete(
            "/api/admin/settings/MAX_MARKDOWN_SIZE_MB", cookies={"lore_session": admin_token},
        )
    assert resp.status_code == 413, resp.text
    db = await get_db()
    rows = await db.query(
        "SELECT id FROM documents WHERE parent_id = $doc AND is_reference = true",
        {"doc": doc_id},
    )
    assert rows == []


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
async def test_widget_upload_docx_converts_sync_to_ref(
    client, admin_user, project_with_doc,
):
    """docx via the widget converts synchronously in the web process (same
    precedent as MCP _convert_docx_sync) and lands as a queued markdown ref."""
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    docx_bytes = b"PK\x03\x04" + b"\x00" * 96  # ZIP magic, converter mocked below
    converted_md = "# Converted\n\nWidget docx body.\n"
    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock, return_value=converted_md):
        resp = await client.post(
            "/api/widget/upload",
            files={"file": ("report.docx", io.BytesIO(docx_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers={"Authorization": f"Bearer {api_token}"},
        )
    assert resp.status_code == 200, resp.text
    ref = await fetch_one("documents", resp.json()["reference_id"])
    assert ref["media_type"] == "markdown"
    # Sync conversion in the web process: the converted text IS the body —
    # nothing pending (unlike the MCP redeem's async "queued" path).
    assert ref.get("processing_status") is None
    assert "Widget docx body" in ref["content"]


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
async def test_widget_upload_zip_lands_as_file_ref(client, admin_user, project_with_doc):
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("bundle.zip", io.BytesIO(b"PK\x03\x04" + b"\x00" * 64), "application/zip")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 200, resp.text
    ref = await fetch_one("documents", resp.json()["reference_id"])
    assert ref["media_type"] == "file"


@pytest.mark.asyncio
async def test_widget_upload_pdf_is_rejected(client, admin_user, project_with_doc):
    """PDF has no branch in the shared import dispatcher — pinned as a 400, not
    silently stored. Adding PDF here is out of scope (plan: Not doing)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, api_token = await _create_api_key(client, admin_token, pid)
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4" + b"\x00" * 64), "application/pdf")},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert resp.status_code == 400, resp.text


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
