"""Tests for thumbnail generation and GET /api/files/{ref_id}/thumb endpoint."""

import io

import pytest
from test_files import _make_doc


def _make_valid_png(width=4, height=4):
    """Generate valid PNG bytes via Pillow (the existing PNG_BYTES has a broken data stream)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


VALID_PNG = _make_valid_png(4, 4)


async def _upload_valid_image(client, token, pid, doc_id, title="test.png"):
    """Upload a valid PNG and return (reference_id, upload_data)."""
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id, "title": title},
        files={"file": (title, io.BytesIO(VALID_PNG), "image/png")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    return data["reference_id"], data


@pytest.mark.asyncio
async def test_upload_image_enqueues_thumbnail(client, admin_user, project_with_doc, enqueue_recorder):
    """Uploading an image should enqueue thumbnail_task (not generate inline)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ThumbGenDoc")

    ref_id, _ = await _upload_valid_image(client, token, pid, doc_id)

    thumb_calls = [(n, a, k) for n, a, k in enqueue_recorder.calls if n == "thumbnail_task"]
    assert len(thumb_calls) == 1
    assert thumb_calls[0][2].get("job_id") == f"thumb:{ref_id}"


@pytest.mark.asyncio
async def test_upload_image_stores_dimensions(client, admin_user, project_with_doc):
    """Uploading an image should store width/height in file_meta."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "DimDoc")
    ref_id, data = await _upload_valid_image(client, token, pid, doc_id)

    meta = data.get("file_meta", {})
    assert "width" in meta, "file_meta should contain 'width'"
    assert "height" in meta, "file_meta should contain 'height'"
    assert isinstance(meta["width"], int)
    assert isinstance(meta["height"], int)


@pytest.mark.asyncio
async def test_serve_thumbnail_endpoint(client, admin_user, project_with_doc):
    """GET /api/files/{ref_id}/thumb returns WebP thumbnail."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ThumbServeDoc")
    ref_id, _ = await _upload_valid_image(client, token, pid, doc_id)

    resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


@pytest.mark.asyncio
async def test_serve_thumbnail_cache_control(client, admin_user, project_with_doc):
    """GET /api/files/{ref_id}/thumb 200 response carries immutable Cache-Control."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ThumbCacheDoc")
    ref_id, _ = await _upload_valid_image(client, token, pid, doc_id)

    resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_serve_thumbnail_404_no_cache_control(client, admin_user):
    """GET /api/files/{ref_id}/thumb 404 response carries no Cache-Control header."""
    _, token = admin_user
    resp = await client.get(
        "/api/files/nonexistent-thumb-cache-ref/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404
    assert "cache-control" not in resp.headers


@pytest.mark.asyncio
async def test_serve_thumbnail_on_demand(client, admin_user, project_with_doc):
    """GET /api/files/{ref_id}/thumb generates thumbnail if missing (old images)."""
    from config import STORAGE_PATH

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "OnDemandDoc")
    ref_id, _ = await _upload_valid_image(client, token, pid, doc_id)

    thumb_path = STORAGE_PATH / pid / ref_id / "_thumb.webp"
    assert not thumb_path.exists(), "No inline thumbnail after upload"

    resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert thumb_path.is_file(), "Thumbnail should be generated on demand"

    thumb_path.unlink()
    assert not thumb_path.exists()

    resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert thumb_path.is_file(), "Thumbnail should be regenerated and cached"


@pytest.mark.asyncio
async def test_serve_thumbnail_non_image_rejected(client, admin_user, project_with_doc):
    """GET /api/files/{ref_id}/thumb returns 400 for non-image references."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "NotImageThumbDoc")
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": doc_id,
            "title": "TextRef",
            "media_type": "markdown",
            "is_reference": True,
            "content": "not an image",
        },
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]

    resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_serve_thumbnail_ref_not_found(client, admin_user):
    """GET /api/files/{ref_id}/thumb returns 404 for missing reference."""
    _, token = admin_user
    resp = await client.get(
        "/api/files/nonexistent-thumb-ref/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_file_also_deletes_thumbnail(client, admin_user, project_with_doc):
    """Deleting a reference file should also remove _thumb.webp."""
    from config import STORAGE_PATH

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ThumbDelDoc")
    ref_id, _ = await _upload_valid_image(client, token, pid, doc_id)

    resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    thumb_path = STORAGE_PATH / pid / ref_id / "_thumb.webp"
    assert thumb_path.is_file()

    resp = await client.delete(
        f"/api/documents/{ref_id}/file",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert not thumb_path.exists(), "Thumbnail should be deleted alongside original"


@pytest.mark.asyncio
async def test_upload_audio_no_thumbnail(client, admin_user, project_with_doc):
    """Audio uploads should NOT generate a thumbnail."""
    from test_files import _upload_audio

    from config import STORAGE_PATH

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "AudioNoThumbDoc")
    ref_id, _ = await _upload_audio(client, token, pid, doc_id)

    thumb_path = STORAGE_PATH / pid / ref_id / "_thumb.webp"
    assert not thumb_path.exists(), "Audio uploads should not generate thumbnails"


@pytest.mark.asyncio
async def test_unit_generate_thumbnail():
    """Unit test: generate_thumbnail produces correct WebP output."""
    import tempfile
    from pathlib import Path

    from PIL import Image
    from thumbnails import generate_thumbnail

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "test.png"
        dst = Path(tmp) / "_thumb.webp"

        img = Image.new("RGB", (400, 300), color=(255, 0, 0))
        img.save(src, "PNG")

        orig_w, orig_h = generate_thumbnail(src, dst)

        assert orig_w == 400
        assert orig_h == 300
        assert dst.is_file()

        thumb = Image.open(dst)
        assert thumb.format == "WEBP"
        assert thumb.size == (200, 200)


@pytest.mark.asyncio
async def test_unit_generate_thumbnail_small_image():
    """Unit test: images smaller than THUMB_SIZE are not upscaled."""
    import tempfile
    from pathlib import Path

    from PIL import Image
    from thumbnails import THUMB_SIZE, generate_thumbnail

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "small.png"
        dst = Path(tmp) / "_thumb.webp"

        img = Image.new("RGB", (50, 30), color=(0, 255, 0))
        img.save(src, "PNG")

        orig_w, orig_h = generate_thumbnail(src, dst)

        assert orig_w == 50
        assert orig_h == 30
        assert dst.is_file()

        thumb = Image.open(dst)
        assert thumb.format == "WEBP"
        assert thumb.size[0] <= THUMB_SIZE
        assert thumb.size[1] <= THUMB_SIZE
        assert thumb.size[0] == thumb.size[1], "Should still be square"


@pytest.mark.asyncio
async def test_unit_get_thumb_path():
    """Unit test: get_thumb_path returns correct path."""
    from thumbnails import THUMB_FILENAME, get_thumb_path

    path = get_thumb_path("proj-1", "ref-1")
    assert path.name == THUMB_FILENAME
    assert "proj-1" in str(path)
    assert "ref-1" in str(path)
