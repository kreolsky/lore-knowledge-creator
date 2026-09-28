"""Tests for the arq thumbnail_task and the upload-trigger thumbnail enqueue.

Phase D migration: thumbnail generation no longer runs inline during upload.
save_upload extracts dimensions via PIL header read, then enqueues thumbnail_task
via arq. The serve endpoint (/api/files/{ref_id}/thumb) still generates on demand
if the worker has not run yet.
"""

from unittest.mock import patch

import pytest
from enqueue_recorder import EnqueueRecorder


@pytest.mark.asyncio
async def test_upload_image_enqueues_thumbnail_task(client, admin_user, project_with_doc, enqueue_recorder):
    """Uploading an image enqueues thumbnail_task with correct args and job_id."""
    import io

    from test_files import _make_doc

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ThumbEnqDoc")

    from test_thumbnails import VALID_PNG

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id, "title": "enq.png"},
        files={"file": ("enq.png", io.BytesIO(VALID_PNG), "image/png")},
        cookies={"lore_session": token},
    )

    assert resp.status_code == 200
    ref_id = resp.json()["reference_id"]

    thumb_calls = enqueue_recorder.of("thumbnail_task")
    assert len(thumb_calls) == 1
    name, args, kwargs = thumb_calls[0]
    assert args[0] == pid
    assert args[1] == ref_id
    assert kwargs.get("job_id") == f"thumb:{ref_id}"


@pytest.mark.asyncio
async def test_upload_image_stores_dimensions_without_inline_thumb(client, admin_user, project_with_doc):
    """Dimensions are stored in file_meta even without inline thumbnail generation."""
    import io

    from test_files import _make_doc

    from config import STORAGE_PATH

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "DimNoThumbDoc")

    from test_thumbnails import VALID_PNG

    with EnqueueRecorder.active():
        resp = await client.post(
            "/api/documents/upload",
            data={"project_id": pid, "document_id": doc_id, "title": "dim.png"},
            files={"file": ("dim.png", io.BytesIO(VALID_PNG), "image/png")},
            cookies={"lore_session": token},
        )

    assert resp.status_code == 200
    data = resp.json()
    meta = data.get("file_meta", {})
    assert "width" in meta
    assert "height" in meta
    assert isinstance(meta["width"], int)
    assert isinstance(meta["height"], int)

    thumb_path = STORAGE_PATH / pid / data["reference_id"] / "_thumb.webp"
    assert not thumb_path.exists(), "Thumbnail should NOT be generated inline"


@pytest.mark.asyncio
async def test_upload_audio_no_thumbnail_enqueue(client, admin_user, project_with_doc, enqueue_recorder):
    """Audio uploads should NOT enqueue thumbnail_task."""
    from test_files import _make_doc, _upload_audio

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "AudioNoThumbEnqDoc")

    await _upload_audio(client, token, pid, doc_id)

    assert "thumbnail_task" not in enqueue_recorder.names()


@pytest.mark.asyncio
async def test_thumbnail_task_generates_file():
    """thumbnail_task generates a thumbnail file on disk."""
    import tempfile
    from pathlib import Path

    from PIL import Image

    from jobs.tasks import thumbnail_task

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "test.png"
        img = Image.new("RGB", (400, 300), color=(255, 0, 0))
        img.save(src, "PNG")

        with patch("config.STORAGE_PATH", Path(tmp)):
            with patch("thumbnails.STORAGE_PATH", Path(tmp)):
                with patch("thumbnails.get_thumb_path", return_value=Path(tmp) / "_thumb.webp"):
                    await thumbnail_task({}, "proj", "ref", "test.png")

        thumb = Path(tmp) / "_thumb.webp"
        assert thumb.is_file()
        result = Image.open(thumb)
        assert result.format == "WEBP"


@pytest.mark.asyncio
async def test_thumbnail_task_skips_missing_file():
    """thumbnail_task silently skips if source file is missing."""
    from pathlib import Path

    from jobs.tasks import thumbnail_task

    with patch("config.STORAGE_PATH", Path("/nonexistent")):
        await thumbnail_task({}, "proj", "ref", "nonexistent.png")
