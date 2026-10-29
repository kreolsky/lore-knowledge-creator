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
