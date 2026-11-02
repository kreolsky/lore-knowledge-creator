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


# TEST-WHY: cross-process-contract — the replay path resolves a stored file_path on the shared storage volume; the pin holds the containment refusal (uniform 404, nothing transcribed) for a file_path that escapes STORAGE_PATH, which the route happily transcribed before the files_util.resolve_stored_file join (rot step 5).
@pytest.mark.asyncio
async def test_upload_and_transcribe_replay_refuses_outside_storage_path(
    client, admin_user, project_with_doc, test_db,
):
    # rot-consolidation step 5. Pre-step-5 code handed STORAGE_PATH / file_path
    # to transcribe_audio unchecked — proven red on gray: 200 with a stubbed
    # transcriber reading any path the row named.
    from db import create_record

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    key = "replay-traversal-key"

    await create_record("documents", "replay-traversal-ref", {
        "project_id": pid, "parent_id": "some-doc", "title": "voice.webm",
        "media_type": "audio", "content": "", "processing_status": "processing",
        "path": "_ref/replay-traversal.webm", "is_reference": True,
        "idempotency_key": key, "file_path": "../../etc/passwd",
    })

    resp = await client.post(
        "/api/documents/upload-and-transcribe",
        files={"file": ("voice.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
        data={"project_id": pid, "idempotency_key": key},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Reference file is missing on disk"
