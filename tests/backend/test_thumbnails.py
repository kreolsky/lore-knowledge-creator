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
