"""Integration tests for file upload, download, delete, and status routes."""

import base64
import io

import pytest
from enqueue_recorder import EnqueueRecorder


@pytest.mark.asyncio
async def test_upload_image_file(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    # Create a doc for the reference
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "FileDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    # Upload a tiny PNG (1x1 pixel)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id, "title": "test.png"},
        files={"file": ("test.png", io.BytesIO(png_bytes), "image/png")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["media_type"] == "image"
    assert data["reference_id"]
    assert data["title"] == "test.png"


@pytest.mark.asyncio
async def test_upload_audio_file(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "AudioDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    audio_bytes = WEBM_BYTES
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("recording.webm", io.BytesIO(audio_bytes), "audio/webm")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["media_type"] == "audio"
    assert data["processing_status"] == "queued"


@pytest.mark.asyncio
async def test_upload_unsupported_type(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid},
        files={"file": ("evil.exe", io.BytesIO(b"\x00"), "application/octet-stream")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_zip_refused_pins_detect_media_type(
    client, admin_user, project_with_doc,
):
    """A magic-valid .zip is REFUSED on the human upload surface — archives are
    agent-only (import_file sandbox_path). Pins detect_media_type's non-widening:
    adding `application/zip` there would silently open the widget to zips."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid},
        files={"file": (
            "work.zip", io.BytesIO(b"PK\x03\x04" + b"\x00" * 64), "application/zip",
        )},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


# ─── Shared helpers ──────────────────────────────────────────────────────────

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Minimal WebM EBML header for magic byte validation
WEBM_BYTES = b"\x1aE\xdf\xa3" + b"\x00" * 96


async def _upload_image(client, token, pid, doc_id):
    """Upload a tiny PNG and return (reference_id, upload_data)."""
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id, "title": "test.png"},
        files={"file": ("test.png", io.BytesIO(PNG_BYTES), "image/png")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    return data["reference_id"], data


async def _upload_audio(client, token, pid, doc_id):
    """Upload fake audio and return (reference_id, upload_data)."""
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("recording.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    return data["reference_id"], data


# ─── GET /api/files/{reference_id}/{filename} ───────────────────────────────


@pytest.mark.asyncio
async def test_serve_uploaded_file(client, admin_user, project_with_doc):
    """Upload PNG → serve via GET → correct body and content-type."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "ServeDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)
    resp = await client.get(
        f"/api/files/{ref_id}/test.png",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == PNG_BYTES


@pytest.mark.asyncio
async def test_serve_file_cache_control(client, admin_user, project_with_doc):
    """GET /api/files/{ref_id}/{filename} 200 response carries immutable Cache-Control."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "ServeCacheDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)
    resp = await client.get(
        f"/api/files/{ref_id}/test.png",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_serve_file_ref_not_found(client, admin_user):
    _, token = admin_user
    resp = await client.get(
        "/api/files/nonexistent-ref/test.png",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404
    assert "cache-control" not in resp.headers


@pytest.mark.asyncio
async def test_serve_file_no_file_attached(client, admin_user, project_with_doc):
    """Markdown ref (no file) → GET → 404 'No file attached'."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "NoFileDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": doc_id, "title": "TextRef", "media_type": "markdown", "is_reference": True, "content": "text"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    resp = await client.get(
        f"/api/files/{ref_id}/anything.txt",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404
    assert "No file attached" in resp.json()["detail"]


# ─── DELETE /api/documents/{reference_id}/file ─────────────────────────────


@pytest.mark.asyncio
async def test_delete_reference_file(client, admin_user, project_with_doc):
    """Upload → DELETE file → file_path cleared, ref still exists."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "DelFileDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)
    resp = await client.delete(
        f"/api/documents/{ref_id}/file",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Verify file is no longer servable
    resp = await client.get(
        f"/api/files/{ref_id}/test.png",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404
    # Verify reference-document still exists
    resp = await client.get(
        f"/api/documents/{ref_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json().get("is_reference") is True


@pytest.mark.asyncio
async def test_delete_file_keeps_content_as_markdown(client, admin_user, project_with_doc):
    """Upload image → set content → DELETE file → media_type becomes 'markdown'."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "ContentDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)
    # Set content on the reference
    await client.patch(
        f"/api/documents/{ref_id}",
        json={"content": "Some notes about this image"},
        cookies={"lore_session": token},
    )
    # Delete file
    resp = await client.delete(
        f"/api/documents/{ref_id}/file",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Check media_type changed to markdown
    resp = await client.get(
        f"/api/documents/{ref_id}/status",
        cookies={"lore_session": token},
    )
    assert resp.json()["media_type"] == "markdown"


@pytest.mark.asyncio
async def test_delete_file_without_content_leaves_markdown_ref(client, admin_user, project_with_doc):
    """Upload image with NO content → DELETE file → an EMPTY markdown ref.

    Pins the always-markdown rule: a file-less image/audio/file ref that kept its
    media_type would render blank (no media surface, editor branch treats image as
    non-editor), and the frontend's optimistic patch already assumes markdown."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "NoContentDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)
    resp = await client.delete(
        f"/api/documents/{ref_id}/file",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/documents/{ref_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["media_type"] == "markdown"
    assert not data.get("file_path")


@pytest.mark.asyncio
async def test_delete_reference_file_emits_reference_updated(client, admin_user, project_with_doc):
    """DELETE /api/documents/{id}/file emits reference_updated for multi-client sync."""
    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "EmitDelDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)

    events = []
    async def capture(**kw):
        events.append(kw)

    on("reference_updated", capture)
    try:
        resp = await client.delete(
            f"/api/documents/{ref_id}/file",
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
        assert events[0]["project_id"] == pid
    finally:
        off("reference_updated", capture)


@pytest.mark.asyncio
async def test_delete_file_ref_not_found(client, admin_user):
    _, token = admin_user
    resp = await client.delete(
        "/api/documents/nonexistent-ref/file",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


# ─── GET /api/documents/{reference_id}/status ──────────────────────────────


@pytest.mark.asyncio
async def test_get_reference_status_audio(client, admin_user, project_with_doc):
    """Upload audio → status shows queued."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "StatusDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_audio(client, token, pid, doc_id)
    resp = await client.get(
        f"/api/documents/{ref_id}/status",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["processing_status"] == "queued"
    assert data["media_type"] == "audio"


@pytest.mark.asyncio
async def test_get_reference_status_markdown(client, admin_user, project_with_doc):
    """Markdown ref → status shows null processing_status."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "MdStatusDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": doc_id, "title": "MdRef", "media_type": "markdown", "is_reference": True, "content": "hello"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    resp = await client.get(
        f"/api/documents/{ref_id}/status",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["processing_status"] is None
    assert data["media_type"] == "markdown"
    assert data["content"] == "hello"


@pytest.mark.asyncio
async def test_get_reference_status_not_found(client, admin_user):
    _, token = admin_user
    resp = await client.get(
        "/api/documents/nonexistent/status",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


# ─── POST /api/documents/{reference_id}/retry ──────────────────────────────


@pytest.mark.asyncio
async def test_retry_transcription_from_error(client, admin_user, project_with_doc):
    """Audio ref in error state → POST /retry → status resets to 'queued', content cleared.

    WHY not upload audio: uploading via /api/documents/upload triggers enqueue_transcription,
    which spawns a background worker. The worker immediately sets processing_status='processing'
    and then calls STT_API_URL. Depending on network (DNS failure = instant, timeout = 600s,
    success = status becomes 'ready'), the final status is non-deterministic.
    This makes upload-based tests flaky and environment-dependent.

    Instead we create the ref via REST (no file → no worker), then set the required fields
    (media_type='audio', file_path, processing_status='error') directly in DB.
    This tests the retry endpoint contract in isolation from the transcription worker.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "RetryDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    # Create audio ref via REST (no file upload → no background worker)
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": doc_id,
            "title": "AudioRetryRef",
            "media_type": "audio", "is_reference": True,
            "content": "Failed transcription",
        },
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    # Set error state + file_path via DB (no REST endpoint for processing_status)
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'error', "
        "file_path = 'fake/path/audio.webm'",
        {"id": ref_id},
    )
    # Verify error state is visible through API
    resp = await client.get(
        f"/api/documents/{ref_id}/status",
        cookies={"lore_session": token},
    )
    assert resp.json()["processing_status"] == "error"
    # Retry
    resp = await client.post(
        f"/api/documents/{ref_id}/retry",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Verify status reset
    resp = await client.get(
        f"/api/documents/{ref_id}/status",
        cookies={"lore_session": token},
    )
    assert resp.json()["processing_status"] == "queued"
    assert resp.json()["content"] == ""


@pytest.mark.asyncio
async def test_retry_transcription_from_stuck_processing(client, admin_user, project_with_doc):
    """Audio ref stuck in 'processing' (worker died mid-run) → POST /retry re-queues it.

    Lets the user self-unstick without a server restart. See files.py retry guard.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "StuckDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "parent_id": doc_id, "title": "StuckAudioRef",
            "media_type": "audio", "is_reference": True, "content": "",
        },
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'processing', "
        "file_path = 'fake/path/audio.webm'",
        {"id": ref_id},
    )
    resp = await client.post(
        f"/api/documents/{ref_id}/retry",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/documents/{ref_id}/status",
        cookies={"lore_session": token},
    )
    assert resp.json()["processing_status"] == "queued"


@pytest.mark.asyncio
async def test_retry_transcription_emits_reference_updated(client, admin_user, project_with_doc):
    """POST /api/documents/{id}/retry emits reference_updated for multi-client sync."""
    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "RetryEmitDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": doc_id, "title": "AudioRef", "media_type": "audio", "is_reference": True},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'error', "
        "file_path = 'fake/path/audio.webm'",
        {"id": ref_id},
    )

    events = []
    async def capture(**kw):
        events.append(kw)

    on("reference_updated", capture)
    try:
        resp = await client.post(
            f"/api/documents/{ref_id}/retry",
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
        assert events[0]["project_id"] == pid
    finally:
        off("reference_updated", capture)


@pytest.mark.asyncio
async def test_retry_non_audio_rejected(client, admin_user, project_with_doc):
    """Markdown ref → retry → 400."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "NotAudioDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": doc_id, "title": "MdRef", "media_type": "markdown", "is_reference": True, "content": "text"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    resp = await client.post(
        f"/api/documents/{ref_id}/retry",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    # A plain markdown ref (no file, default status) is not in a retryable state.
    # Type-specific rejection (markdown WITH a non-docx file) is covered in
    # test_docx_import.py::test_retry_rejects_markdown_without_docx_file.


@pytest.mark.asyncio
async def test_retry_non_retryable_state(client, admin_user, project_with_doc):
    """Audio ref in 'queued' state → retry → 400."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "QueuedDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_audio(client, token, pid, doc_id)
    # Status is already 'queued' from upload
    resp = await client.post(
        f"/api/documents/{ref_id}/retry",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert "not in a retryable state" in resp.json()["detail"]


# ─── POST /api/documents/upload-and-transcribe ──────────────────────────────


async def _make_doc(client, token, pid, title="TestDoc"):
    """Create a document and return its ID."""
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


@pytest.mark.asyncio
async def test_upload_and_transcribe_success(client, admin_user, project_with_doc):
    """Sync transcription: upload audio → mock STT → 200 with text and reference_id."""
    from unittest.mock import AsyncMock, patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "TranscribeDoc")

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="Hello world"):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "Hello world"
    assert data["reference_id"]

    # Verify DB state
    from db import fetch_one
    ref = await fetch_one("documents", data["reference_id"])
    assert ref["processing_status"] == "ready"
    assert ref["content"] == "Hello world"
    assert ref["media_type"] == "audio"
    assert ref["file_path"] is not None
    assert ref["parent_id"] == doc_id
    assert ref["is_reference"] is True


@pytest.mark.asyncio
async def test_upload_and_transcribe_file_stored(client, admin_user, project_with_doc):
    """Sync transcription stores the audio file on disk."""
    from unittest.mock import AsyncMock, patch

    from config import STORAGE_PATH

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "FileStoreDoc")

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="ok"):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
            cookies={"lore_session": token},
        )
    ref_id = resp.json()["reference_id"]
    from db import fetch_one
    ref = await fetch_one("documents", ref_id)
    abs_path = STORAGE_PATH / ref["file_path"]
    assert abs_path.is_file()


@pytest.mark.asyncio
async def test_upload_and_transcribe_non_audio_rejected(client, admin_user, project_with_doc):
    """Sync transcription rejects non-audio files."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        data={"project_id": pid},
        files={"file": ("test.png", io.BytesIO(PNG_BYTES), "image/png")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert "Only audio" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_and_transcribe_failure_returns_502(client, admin_user, project_with_doc):
    """Transcription error → 502 with reference_id, ref status set to 'error'."""
    from unittest.mock import AsyncMock, patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "FailDoc")

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, side_effect=RuntimeError("STT down")):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 502
    data = resp.json()
    assert data["reference_id"]

    from db import fetch_one
    ref = await fetch_one("documents", data["reference_id"])
    assert ref["processing_status"] == "error"


@pytest.mark.asyncio
async def test_upload_and_transcribe_emits_reference_created(client, admin_user, project_with_doc):
    """Sync transcription emits reference_created event."""
    from unittest.mock import AsyncMock, patch

    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "EmitDoc")

    events = []
    async def capture(**kw):
        events.append(kw)

    on("reference_created", capture)
    try:
        with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="text"):
            resp = await client.post(
                "/api/documents/upload-and-transcribe",
                data={"project_id": pid, "document_id": doc_id},
                files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
                cookies={"lore_session": token},
            )
        assert resp.status_code == 200
        assert len(events) == 1
        assert events[0]["project_id"] == pid
        assert events[0]["reference_id"] == resp.json()["reference_id"]
        assert events[0]["document_id"] == doc_id
    finally:
        off("reference_created", capture)


# ─── Access control on upload endpoints ──────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_readonly_user_rejected(client, admin_user, regular_user, project_with_doc):
    """User with readonly access cannot upload files."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    # Give regular user readonly access
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid},
        files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upload_and_transcribe_readonly_rejected(client, admin_user, regular_user, project_with_doc):
    """User with readonly access cannot use sync transcription endpoint."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )

    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        data={"project_id": pid},
        files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upload_commentator_rejected(client, admin_user, regular_user, project_with_doc):
    """Commentator access cannot upload files."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "commentator"},
        cookies={"lore_session": admin_token},
    )

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid},
        files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


# ─── Upload with/without document binding ────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_audio_with_document_binding(client, admin_user, project_with_doc):
    """Upload audio with document_id → reference bound to that document."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "BoundDoc")

    ref_id, data = await _upload_audio(client, token, pid, doc_id)
    assert data["parent_id"] == doc_id


@pytest.mark.asyncio
async def test_upload_audio_without_document_binding(client, admin_user, project_with_doc):
    """Upload audio without document_id → project-level reference hosted on the index doc
    (parent_id = index_doc_id), never a silent no-host write."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid},
        files={"file": ("voice.webm", io.BytesIO(WEBM_BYTES), "audio/webm")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json().get("parent_id") == idx_id


# ─── Event bus from upload ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_emits_reference_created(client, admin_user, project_with_doc):
    """POST /upload emits reference_created with correct metadata."""
    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "EventDoc")

    events = []
    async def capture(**kw):
        events.append(kw)

    on("reference_created", capture)
    try:
        ref_id, _ = await _upload_audio(client, token, pid, doc_id)
        assert len(events) == 1
        assert events[0]["project_id"] == pid
        assert events[0]["reference_id"] == ref_id
        assert events[0]["document_id"] == doc_id
    finally:
        off("reference_created", capture)


# ─── Unit tests: save_base64_image ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_unit_save_base64_image_valid_png():
    from unittest.mock import AsyncMock, patch

    from files_service import save_base64_image

    b64 = base64.b64encode(PNG_BYTES).decode()
    with patch("files_service.save_upload", new_callable=AsyncMock) as mock_save:
        mock_save.return_value = ("ref-1", {"reference_id": "ref-1", "file_meta": {"width": 1, "height": 1}})
        ref_id, result = await save_base64_image(b64, "png", "proj-1", None, "image-1.png")
    assert ref_id == "ref-1"
    assert result["reference_id"] == "ref-1"


@pytest.mark.asyncio
async def test_unit_save_base64_image_unsupported_mime():
    from files_service import save_base64_image

    b64 = base64.b64encode(b"\x00").decode()
    with pytest.raises(ValueError, match="Unsupported image type"):
        await save_base64_image(b64, "bmp", "proj-1", None, "image-1.bmp")


@pytest.mark.asyncio
async def test_unit_save_base64_image_invalid_base64():
    from files_service import save_base64_image

    with pytest.raises(ValueError, match="Invalid base64"):
        await save_base64_image("!!!not-base64!!!", "png", "proj-1", None, "image-1.png")


@pytest.mark.asyncio
async def test_unit_save_base64_image_magic_mismatch():
    from files_service import save_base64_image

    b64 = base64.b64encode(WEBM_BYTES).decode()
    with pytest.raises(ValueError, match="does not match"):
        await save_base64_image(b64, "png", "proj-1", None, "image-1.png")


# ─── Unit tests: extract_and_replace_images ──────────────────────────────────


@pytest.mark.asyncio
async def test_unit_extract_inline_image():
    from unittest.mock import AsyncMock, patch

    from files_service import extract_and_replace_images

    b64 = base64.b64encode(PNG_BYTES).decode()
    md = f"![alt](data:image/png;base64,{b64})"

    with patch("files_service.save_base64_image", new_callable=AsyncMock) as mock_save:
        mock_save.return_value = ("ref-1", {"reference_id": "ref-1", "file_meta": {"width": 10, "height": 20}})
        content, records = await extract_and_replace_images(md, "proj-1", None)

    assert "data:image" not in content
    assert "ref:ref-1" in content
    assert len(records) == 1


@pytest.mark.asyncio
async def test_unit_extract_refdef_image():
    from unittest.mock import AsyncMock, patch

    from files_service import extract_and_replace_images

    b64 = base64.b64encode(PNG_BYTES).decode()
    md = f"[img1]: data:image/png;base64,{b64}\n\n![][img1]\n"

    with patch("files_service.save_base64_image", new_callable=AsyncMock) as mock_save:
        mock_save.return_value = ("ref-1", {"reference_id": "ref-1", "file_meta": {"width": 10, "height": 20}})
        content, records = await extract_and_replace_images(md, "proj-1", None)

    assert "data:image" not in content
    assert "ref:ref-1" in content
    assert "[img1]:" not in content
    assert len(records) == 1


@pytest.mark.asyncio
async def test_unit_extract_mixed_images():
    from unittest.mock import patch

    from files_service import extract_and_replace_images

    b64 = base64.b64encode(PNG_BYTES).decode()
    md = (
        f"[ref1]: data:image/png;base64,{b64}\n\n"
        f"![][ref1]\n\n"
        f"![inline](data:image/png;base64,{b64})\n"
    )

    call_count = 0

    async def mock_save(b64_data, mime_suffix, project_id, document_id, filename, **kwargs):
        nonlocal call_count
        call_count += 1
        return (f"ref-{call_count}", {"reference_id": f"ref-{call_count}", "file_meta": {"width": 10, "height": 20}})

    with patch("files_service.save_base64_image", side_effect=mock_save):
        content, records = await extract_and_replace_images(md, "proj-1", None)

    assert "data:image" not in content
    assert len(records) == 2


@pytest.mark.asyncio
async def test_unit_extract_no_images():
    from files_service import extract_and_replace_images

    md = "# Hello\n\nJust text.\n"
    content, records = await extract_and_replace_images(md, "proj-1", None)
    assert content == md
    assert records == []


@pytest.mark.asyncio
async def test_unit_extract_invalid_image_raises():
    from files_service import extract_and_replace_images

    md = "![alt](data:image/png;base64,!!!invalid!!!)"
    with pytest.raises(ValueError):
        await extract_and_replace_images(md, "proj-1", None)


# ─── Markdown upload helpers ─────────────────────────────────────────────────


def _md_with_inline_png(alt: str = "test") -> bytes:
    b64 = base64.b64encode(PNG_BYTES).decode()
    return f"# Title\n![{alt}](data:image/png;base64,{b64})\n".encode()


def _md_with_refdef_png(label: str = "img1") -> bytes:
    b64 = base64.b64encode(PNG_BYTES).decode()
    return f"[{label}]: data:image/png;base64,{b64}\n\n![][{label}]\n".encode()


# ─── POST /api/documents/upload-markdown ────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_markdown_reference_success(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    md_content = b"# My Document\n\nHello world.\n"
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reference"]["media_type"] == "markdown"
    assert data["reference"]["title"] == "doc"
    assert data["image_references"] == []


@pytest.mark.asyncio
async def test_upload_markdown_with_inline_base64_image(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    md_content = _md_with_inline_png("photo")
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["image_references"]) == 1
    assert data["image_references"][0]["media_type"] == "image"
    assert "data:image" not in data["reference"]["content"]
    assert "ref:" in data["reference"]["content"]


@pytest.mark.asyncio
async def test_upload_markdown_with_refdef_base64_image(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    md_content = _md_with_refdef_png("img1")
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["image_references"]) == 1
    assert "data:image" not in data["reference"]["content"]
    assert "ref:" in data["reference"]["content"]


@pytest.mark.asyncio
async def test_upload_markdown_non_md_extension_accepted(client, admin_user, project_with_doc):
    """Any UTF-8 text file is accepted by content, not extension (.txt → markdown ref)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.txt", io.BytesIO(b"hello"), "text/plain")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["reference"]["media_type"] == "markdown"


@pytest.mark.asyncio
async def test_upload_markdown_oversized_rejected(client, admin_user, project_with_doc):
    from unittest.mock import patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    big_md = b"x" * 101
    with patch("config.MAX_MARKDOWN_SIZE_MB", 0):
        resp = await client.post(
            "/api/documents/upload-markdown",
            data={"project_id": pid},
            files={"file": ("doc.md", io.BytesIO(big_md), "text/markdown")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_upload_markdown_invalid_base64_rejected(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    md_content = b"![alt](data:image/png;base64,!!!invalid!!!)\n"
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_markdown_image_too_large_rejected(client, admin_user, project_with_doc):
    from unittest.mock import patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    big_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
    md_content = f"![alt](data:image/png;base64,{big_b64})\n".encode()
    with patch("config.MAX_IMAGE_SIZE_MB", 0):
        resp = await client.post(
            "/api/documents/upload-markdown",
            data={"project_id": pid},
            files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_markdown_unsupported_image_type_rejected(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    md_content = b"![alt](data:image/bmp;base64,AAAA)\n"
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_markdown_emits_reference_created(client, admin_user, project_with_doc):
    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, token = admin_user
    md_content = _md_with_inline_png("photo")

    events = []

    async def capture(**kw):
        events.append(kw)

    on("reference_created", capture)
    try:
        resp = await client.post(
            "/api/documents/upload-markdown",
            data={"project_id": pid},
            files={"file": ("doc.md", io.BytesIO(md_content), "text/markdown")},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        assert len(events) == 2
        md_event = [e for e in events if e.get("reference_id") == resp.json()["reference"]["reference_id"]]
        assert len(md_event) == 1
        assert md_event[0]["project_id"] == pid
    finally:
        off("reference_created", capture)


@pytest.mark.asyncio
async def test_upload_markdown_readonly_user_rejected(client, admin_user, regular_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )

    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(b"hello"), "text/markdown")},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upload_markdown_no_document_binding(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(b"hello"), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["reference"].get("document_id") is None


@pytest.mark.asyncio
async def test_upload_markdown_null_filename_rejected(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents/upload-markdown",
        data={"project_id": pid},
        files={"file": (None, io.BytesIO(b"hello"), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (400, 422)


# ─── Streaming audio upload helpers (2.4) ────────────────────────────────────

class _FakeUpload:
    """Minimal UploadFile stub exposing an async chunked read()."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


@pytest.mark.asyncio
async def test_stream_upload_to_tmp_writes_and_returns_head(tmp_path, monkeypatch):
    import files_util

    monkeypatch.setattr(files_util, "STORAGE_PATH", tmp_path)
    payload = b"OggS" + b"\x00" * (3 * 1024 * 1024)  # 3 MB, spans multiple chunks
    path, head = await files_util.stream_upload_to_tmp(_FakeUpload(payload), 10 * 1024 * 1024, 10)

    assert head == payload[:16]
    assert path.read_bytes() == payload
    path.unlink()


@pytest.mark.asyncio
async def test_stream_upload_to_tmp_aborts_oversize(tmp_path, monkeypatch):
    import files_util
    from fastapi import HTTPException

    monkeypatch.setattr(files_util, "STORAGE_PATH", tmp_path)
    payload = b"\x00" * (2 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc:
        await files_util.stream_upload_to_tmp(_FakeUpload(payload), 1024 * 1024, 1)
    assert exc.value.status_code == 413
    # The temp file is unlinked on abort — no orphan left under tmp/.
    assert not list((tmp_path / "tmp").glob("*"))


@pytest.mark.asyncio
async def test_save_upload_moves_src_path(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch

    import files_util

    monkeypatch.setattr(files_util, "STORAGE_PATH", tmp_path)
    src = tmp_path / "src.bin"
    src.write_bytes(b"AUDIODATA")

    with patch.object(files_util, "create_reference_row", new=AsyncMock(return_value={})), \
         patch.object(files_util, "serialize_record", return_value={}), \
         EnqueueRecorder.active():
        ref_id, _ = await files_util.save_upload(
            None, "audio/mp4", "rec.m4a", "proj1", "proj1-host",
            title="rec", media_type="audio", processing_status="queued",
            src_path=src,
        )

    assert not src.exists()  # moved, not copied
    stored = tmp_path / "proj1" / ref_id / "rec.m4a"
    assert stored.read_bytes() == b"AUDIODATA"


@pytest.mark.asyncio
async def test_sweep_tmp_uploads_removes_only_old(tmp_path, monkeypatch):
    import os
    import time

    import files_util

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(files_util, "TMP_UPLOAD_DIR", tmp_dir)

    old = tmp_dir / "orphan.bin"
    old.write_bytes(b"stale")
    stale_mtime = time.time() - files_util._TMP_ORPHAN_AGE_SEC - 60
    os.utime(old, (stale_mtime, stale_mtime))

    fresh = tmp_dir / "in_flight.bin"
    fresh.write_bytes(b"live")

    await files_util.sweep_tmp_uploads()

    assert not old.exists()      # stale orphan removed
    assert fresh.exists()        # recent (possibly in-flight) upload untouched


@pytest.mark.asyncio
async def test_sweep_tmp_uploads_no_dir(tmp_path, monkeypatch):
    import files_util

    monkeypatch.setattr(files_util, "TMP_UPLOAD_DIR", tmp_path / "does_not_exist")
    # No tmp dir yet (fresh deploy) → no-op, no error.
    await files_util.sweep_tmp_uploads()


# ─── is_text_bytes content sniffing ────────────────────────────────────────

def test_is_text_bytes_plain_ascii():
    from files_util import is_text_bytes
    assert is_text_bytes(b"hello world\nsecond line\n") == "hello world\nsecond line\n"


def test_is_text_bytes_utf8_emoji():
    from files_util import is_text_bytes
    raw = "héllo 🌍 мир\n".encode("utf-8")
    assert is_text_bytes(raw) == "héllo 🌍 мир\n"


def test_is_text_bytes_rejects_nul():
    from files_util import is_text_bytes
    assert is_text_bytes(b"text\x00more") is None


def test_is_text_bytes_rejects_png_magic():
    from files_util import is_text_bytes
    assert is_text_bytes(PNG_BYTES) is None


def test_is_text_bytes_rejects_control_blob():
    from files_util import is_text_bytes
    assert is_text_bytes(bytes(range(1, 9)) * 20) is None


def test_is_text_bytes_empty():
    from files_util import is_text_bytes
    assert is_text_bytes(b"") == ""


# ─── POST /api/references/upload-markdown — arbitrary text files ────────────

async def _make_doc(client, token, pid, title):
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


@pytest.mark.asyncio
async def test_upload_plain_text_file(client, admin_user, project_with_doc):
    """A .txt uploads as a markdown reference with content stored verbatim."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "TxtDoc")
    body = "line one is fairly long prose that\ncontinues on the next line here\n"
    resp = await client.post(
        "/api/references/upload-markdown",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("notes.txt", io.BytesIO(body.encode("utf-8")), "text/plain")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    ref = resp.json()["reference"]
    assert ref["media_type"] == "markdown"
    assert ref["title"] == "notes"
    assert ref["content"] == body  # verbatim — NOT reflowed for non-markdown


@pytest.mark.asyncio
async def test_upload_markdown_file_is_normalized(client, admin_user, project_with_doc):
    """A .md with hard-wrapped prose is still reflowed (existing behavior preserved)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "MdNormDoc")
    body = ("This is a paragraph that has been hard wrapped at a fixed column\n"
            "and continues onto the following line and keeps going further\n"
            "and yet another wrapped line of prose to trip the reflow heuristic\n")
    resp = await client.post(
        "/api/references/upload-markdown",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("doc.md", io.BytesIO(body.encode("utf-8")), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    content = resp.json()["reference"]["content"]
    assert content != body  # reflowed
    assert "\n" not in content.strip()  # folded into one logical line


@pytest.mark.asyncio
async def test_upload_text_rejects_binary(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "BinDoc")
    resp = await client.post(
        "/api/references/upload-markdown",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("evil.txt", io.BytesIO(PNG_BYTES), "text/plain")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


# ─── Upload idempotency (idempotency_key replay — SYSTEM: recording-cache) ──


async def _ref_rows(pid):
    """All non-deleted reference rows of a project (direct SELECT, raw ids)."""
    from db import get_db

    db = await get_db()
    return await db.query(
        "SELECT * FROM documents WHERE project_id = $pid AND is_reference = true "
        "AND deleted_at = NONE",
        {"pid": pid},
    )


def _upload_files(name="recording.webm"):
    return {"file": (name, io.BytesIO(WEBM_BYTES), "audio/webm")}


@pytest.mark.asyncio
async def test_upload_same_key_twice_returns_same_reference_one_row(client, admin_user, project_with_doc):
    """A replay of the same idempotency_key is served the EXISTING reference —
    no second row, no second stored file."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "IdemDoc")
    data = {"project_id": pid, "document_id": doc_id, "idempotency_key": "take-111"}

    r1 = await client.post(
        "/api/documents/upload", data=data, files=_upload_files(),
        cookies={"lore_session": token},
    )
    r2 = await client.post(
        "/api/documents/upload", data=data, files=_upload_files(),
        cookies={"lore_session": token},
    )

    assert r1.status_code == 200
    assert r2.status_code == 200
    ref_id = r1.json()["reference_id"]
    assert r2.json()["reference_id"] == ref_id
    rows = await _ref_rows(pid)
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == "take-111"

    from config import STORAGE_PATH
    ref_dir = STORAGE_PATH / pid / ref_id
    assert ref_dir.is_dir()
    assert len(list(ref_dir.iterdir())) == 1  # one stored audio file, replay added none


@pytest.mark.asyncio
async def test_upload_keyless_twice_creates_two_distinct_refs(client, admin_user, project_with_doc):
    """No key = legacy behavior: every upload is a distinct reference."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "NoKeyDoc")
    data = {"project_id": pid, "document_id": doc_id}

    r1 = await client.post(
        "/api/documents/upload", data=data, files=_upload_files(),
        cookies={"lore_session": token},
    )
    r2 = await client.post(
        "/api/documents/upload", data=data, files=_upload_files(),
        cookies={"lore_session": token},
    )

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["reference_id"] != r2.json()["reference_id"]
    assert len(await _ref_rows(pid)) == 2


@pytest.mark.asyncio
async def test_upload_and_transcribe_replay_returns_stored_text_no_side_effect(client, admin_user, project_with_doc):
    """Replay of a READY ref returns the stored text + same reference_id with NO
    second transcription, row, or stored file."""
    from unittest.mock import AsyncMock, patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "IdemSyncDoc")
    data = {"project_id": pid, "document_id": doc_id, "idempotency_key": "take-222"}

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="Hello world") as stt:
        r1 = await client.post(
            "/api/documents/upload-and-transcribe", data=data, files=_upload_files(),
            cookies={"lore_session": token},
        )
        r2 = await client.post(
            "/api/documents/upload-and-transcribe", data=data, files=_upload_files(),
            cookies={"lore_session": token},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    ref_id = r1.json()["reference_id"]
    assert r2.json() == {"text": "Hello world", "reference_id": ref_id}
    assert stt.call_count == 1  # replay never re-transcribed
    assert len(await _ref_rows(pid)) == 1

    from config import STORAGE_PATH
    ref_dir = STORAGE_PATH / pid / ref_id
    assert ref_dir.is_dir()
    assert len(list(ref_dir.iterdir())) == 1  # replay added no second stored file


@pytest.mark.asyncio
async def test_upload_and_transcribe_replay_after_failure_retries_on_existing_ref(client, admin_user, project_with_doc):
    """A ref left non-ready by a failed transcription is REUSED on replay — the
    retry transcribes the existing row, never creates a second one."""
    from unittest.mock import AsyncMock, patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "IdemRetryDoc")
    data = {"project_id": pid, "document_id": doc_id, "idempotency_key": "take-333"}

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, side_effect=RuntimeError("STT down")):
        r1 = await client.post(
            "/api/documents/upload-and-transcribe", data=data, files=_upload_files(),
            cookies={"lore_session": token},
        )
    assert r1.status_code == 502
    ref_id = r1.json()["reference_id"]

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="Recovered"):
        r2 = await client.post(
            "/api/documents/upload-and-transcribe", data=data, files=_upload_files(),
            cookies={"lore_session": token},
        )

    assert r2.status_code == 200
    assert r2.json() == {"text": "Recovered", "reference_id": ref_id}
    assert len(await _ref_rows(pid)) == 1


@pytest.mark.asyncio
async def test_upload_loser_path_serves_replay_hit_not_500(client, admin_user, project_with_doc, monkeypatch):
    """Unique-index race loser: the create inside save_upload calls through (row
    lands) then raises — the catch re-selects by key and serves the created ref
    instead of a 500."""
    import files_util

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "LoserDoc")

    orig = files_util.create_reference_row
    calls = {"n": 0}

    async def call_through_then_raise(**kw):
        calls["n"] += 1
        record = await orig(**kw)
        if calls["n"] == 1:
            raise RuntimeError("simulated unique-index race loser")
        return record

    monkeypatch.setattr(files_util, "create_reference_row", call_through_then_raise)

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id, "idempotency_key": "take-444"},
        files=_upload_files(),
        cookies={"lore_session": token},
    )

    assert resp.status_code == 200
    assert resp.json()["reference_id"]
    assert len(await _ref_rows(pid)) == 1
