"""Integration tests for POST /api/documents/upload-and-transcribe."""

import io
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_upload_and_transcribe_success(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="hello world"):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            files={"file": ("voice.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
            data={"project_id": pid},
            cookies={"lore_session": admin_token},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "hello world"
    assert data["reference_id"]

    # Verify reference was created with correct state
    ref_resp = await client.get(
        f"/api/documents/{data['reference_id']}/status",
        cookies={"lore_session": admin_token},
    )
    assert ref_resp.status_code == 200
    assert ref_resp.json()["processing_status"] == "ready"
    assert ref_resp.json()["content"] == "hello world"


@pytest.mark.asyncio
async def test_upload_and_transcribe_with_document_id(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="doc text"):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            files={"file": ("voice.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
            data={"project_id": pid, "document_id": doc_id},
            cookies={"lore_session": admin_token},
        )

    assert resp.status_code == 200
    assert resp.json()["text"] == "doc text"


@pytest.mark.asyncio
async def test_upload_and_transcribe_rejects_non_audio(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        files={"file": ("photo.png", io.BytesIO(b"\x89PNG" + b"\x00" * 100), "image/png")},
        data={"project_id": pid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400
    assert "audio" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_and_transcribe_rejects_oversized(client, admin_user, project_with_doc, monkeypatch):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    # Patch limit to 1MB so we don't allocate hundreds of MB in tests. The audio path
    # now streams through files_util.save_audio_upload, which reads MAX_AUDIO_SIZE_MB
    # via settings.get — the config attr is its no-override fallback leg.
    monkeypatch.setattr("config.MAX_AUDIO_SIZE_MB", 1)

    big_data = b"\x1aE\xdf\xa3" + b"\x00" * (2 * 1024 * 1024 - 4)  # 2MB > 1MB limit
    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        files={"file": ("big.webm", io.BytesIO(big_data), "audio/webm")},
        data={"project_id": pid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_upload_and_transcribe_stt_failure(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, side_effect=RuntimeError("STT down")):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            files={"file": ("voice.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
            data={"project_id": pid},
            cookies={"lore_session": admin_token},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["reference_id"]

    # Verify reference was saved with error status (audio preserved for retry)
    ref_resp = await client.get(
        f"/api/documents/{data['reference_id']}/status",
        cookies={"lore_session": admin_token},
    )
    assert ref_resp.json()["processing_status"] == "error"


@pytest.mark.asyncio
async def test_upload_and_transcribe_requires_auth(client):
    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        files={"file": ("voice.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
        data={"project_id": "any"},
    )
    assert resp.status_code == 401
