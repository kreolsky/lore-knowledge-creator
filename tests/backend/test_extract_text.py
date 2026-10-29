"""Tests for POST /api/documents/extract-text — synchronous file-drop import.

The endpoint returns cleaned Markdown (text content or converted .docx) WITHOUT
creating a parent reference record (unlike upload-markdown / upload-docx). Only
image-reference records are created by extract_and_replace_images. This is the
backend half of the editor file-drop feature (drag a file into the document body
→ confirm → insert cleaned markdown below the drop position).
"""

import io
from unittest.mock import AsyncMock, patch

import pytest

# Minimal valid DOCX magic: a ZIP local-file-header signature.
DOCX_MAGIC = b"PK\x03\x04" + b"\x00" * 64

# 1x1 PNG, base64 — used to verify the converter→extract_and_replace_images path.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


async def _make_doc(client, token, pid, title="DropDoc"):
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


# ---- text files ----


# ---- image extraction ----

@pytest.mark.asyncio
async def test_extract_text_extracts_inline_image(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ImgDoc")
    md = f"![pic](data:image/png;base64,{_PNG_B64})\n"
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("doc.md", io.BytesIO(md.encode("utf-8")), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["image_references"]) == 1
    assert "data:image" not in data["markdown"]
    assert "ref:" in data["markdown"]


@pytest.mark.asyncio
async def test_extract_text_invalid_image_rejected(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "BadImgDoc")
    md = b"![alt](data:image/png;base64,!!!invalid!!!)\n"
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("doc.md", io.BytesIO(md), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


# ---- docx path ----


@pytest.mark.asyncio
async def test_extract_text_docx_with_image_extracts(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "DocxImgDoc")
    converter_md = f"![pic](data:image/png;base64,{_PNG_B64})\n"
    with patch("routes.files.post_docx_to_converter", new_callable=AsyncMock, return_value=converter_md):
        resp = await client.post(
            "/api/documents/extract-text",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("report.docx", io.BytesIO(DOCX_MAGIC),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["image_references"]) == 1
    assert "data:image" not in data["markdown"]


@pytest.mark.asyncio
async def test_extract_text_docx_converter_timeout_returns_502(client, admin_user, project_with_doc):
    """Converter timeout/timeout → 502 (explicit, no silent degradation)."""
    import asyncio as _asyncio

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "TimeoutDoc")

    async def _hang(*a, **kw):
        raise _asyncio.TimeoutError()

    with patch("routes.files.post_docx_to_converter", new=_hang):
        resp = await client.post(
            "/api/documents/extract-text",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("report.docx", io.BytesIO(DOCX_MAGIC),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 502


# ---- access control ----

@pytest.mark.asyncio
async def test_extract_text_readonly_user_rejected(client, admin_user, regular_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid},
        files={"file": ("doc.md", io.BytesIO(b"hello"), "text/markdown")},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403
