"""Integration tests for POST /api/documents/upload-and-transcribe."""

import io

import pytest


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
async def test_upload_and_transcribe_requires_auth(client):
    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        files={"file": ("voice.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
        data={"project_id": "any"},
    )
    assert resp.status_code == 401
