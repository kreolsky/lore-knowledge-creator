"""Tests for the arq thumbnail_task and the upload-trigger thumbnail enqueue.

Phase D migration: thumbnail generation no longer runs inline during upload.
save_upload extracts dimensions via PIL header read, then enqueues thumbnail_task
via arq. The serve endpoint (/api/files/{ref_id}/thumb) still generates on demand
if the worker has not run yet.
"""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_thumbnail_task_generates_file():
    """thumbnail_task generates a thumbnail file on disk."""
    from uuid import uuid4

    from PIL import Image
    from thumbnails import get_thumb_path

    from config import STORAGE_PATH
    from jobs.tasks import thumbnail_task

    # TEST-WHY: stale patch target — thumbnail_task resolves its source through
    # files_util.resolve_stored_file (rot-epic step 2), bound to the REAL
    # STORAGE_PATH at import; the former config/thumbnails.STORAGE_PATH patches
    # stopped reaching it, so the source must be a REAL contained file.
    rel = f"thumb-arq-{uuid4().hex[:8]}/test.png"
    src = STORAGE_PATH / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 300), color=(255, 0, 0)).save(src, "PNG")

    await thumbnail_task({}, "proj-thumb-arq", "ref-thumb-arq", rel)

    thumb = get_thumb_path("proj-thumb-arq", "ref-thumb-arq")
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
