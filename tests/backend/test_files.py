"""Integration tests for file upload, download, delete, and status routes."""

import base64
import io

import pytest

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


# ─── DELETE /api/documents/{reference_id}/file ─────────────────────────────


@pytest.mark.asyncio
async def test_delete_file_without_content_deletes_the_reference(client, admin_user, project_with_doc):
    """Upload image with NO text → DELETE file → the whole reference is deleted.

    A reference left with neither file nor text is an empty shell nobody wants;
    deleting the picture from the reference view must not leave one behind."""
    from event_bus import off, on

    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "NoContentDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)
    # Whitespace-only text counts as empty.
    await client.patch(
        f"/api/documents/{ref_id}",
        json={"content": "  \n "},
        cookies={"lore_session": token},
    )

    deleted = []
    async def capture(**kw):
        deleted.append(kw)

    on("reference_deleted", capture)
    try:
        resp = await client.delete(
            f"/api/documents/{ref_id}/file",
            cookies={"lore_session": token},
        )
    finally:
        off("reference_deleted", capture)
    assert resp.status_code == 200
    assert resp.json()["reference_deleted"] is True
    assert deleted == [{"project_id": pid, "reference_id": ref_id}]
    resp = await client.get(
        f"/api/documents/{ref_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_file_reads_live_collab_text(client, admin_user, project_with_doc):
    """Text typed into the open editor but not yet flushed keeps the reference."""
    from collab.registry import _session_key, _sessions

    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "LiveDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    ref_id, _ = await _upload_image(client, token, pid, doc_id)

    class _Live:
        content = "typed just now"

    key = _session_key("doc", ref_id)
    _sessions[key] = _Live()
    try:
        resp = await client.delete(
            f"/api/documents/{ref_id}/file",
            cookies={"lore_session": token},
        )
    finally:
        _sessions.pop(key, None)
    assert resp.status_code == 200
    assert resp.json()["reference_deleted"] is False
    resp = await client.get(
        f"/api/documents/{ref_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["media_type"] == "markdown"


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
    await client.patch(
        f"/api/documents/{ref_id}",
        json={"content": "Caption"},
        cookies={"lore_session": token},
    )

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


    # A plain markdown ref (no file, default status) is not in a retryable state.
    # Type-specific rejection (markdown WITH a non-docx file) is covered in
    # test_docx_import.py::test_retry_rejects_markdown_without_docx_file.


@pytest.mark.asyncio
async def test_retry_transcription_from_stuck_queued(client, admin_user, project_with_doc):
    """Audio ref stuck in 'queued' (its job was refused before the first status write,
    e.g. by a stale-code worker) → POST /retry re-queues it instead of refusing."""
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
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/documents/{ref_id}/status", cookies={"lore_session": token})
    assert resp.json()["processing_status"] == "queued"


# ─── POST /api/documents/upload-and-transcribe ──────────────────────────────


async def _make_doc(client, token, pid, title="TestDoc"):
    """Create a document and return its ID."""
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


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


# ─── Markdown upload helpers ─────────────────────────────────────────────────


def _md_with_refdef_png(label: str = "img1") -> bytes:
    b64 = base64.b64encode(PNG_BYTES).decode()
    return f"[{label}]: data:image/png;base64,{b64}\n\n![][{label}]\n".encode()


# ─── POST /api/documents/upload-markdown ────────────────────────────────────


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


# ─── Streaming audio upload helpers (2.4) ────────────────────────────────────


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
