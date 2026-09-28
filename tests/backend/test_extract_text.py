"""Tests for POST /api/documents/extract-text — synchronous file-drop import.

The endpoint returns cleaned Markdown (text content or converted .docx) WITHOUT
creating a parent reference record (unlike upload-markdown / upload-docx). Only
image-reference records are created by extract_and_replace_images. This is the
backend half of the editor file-drop feature (drag a file into the document body
→ confirm → insert cleaned markdown below the drop position).
"""

import base64
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
PNG_BYTES = base64.b64decode(_PNG_B64)


async def _make_doc(client, token, pid, title="DropDoc"):
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


# ---- text files ----

@pytest.mark.asyncio
async def test_extract_text_markdown_returns_content(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid)
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("doc.md", io.BytesIO(b"# Title\n\nHello world.\n"), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Title" in data["markdown"]
    assert data["image_references"] == []


@pytest.mark.asyncio
async def test_extract_text_md_is_normalized(client, admin_user, project_with_doc):
    """A .md with hard-wrapped prose is reflowed (same choke point as upload-markdown)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "NormDoc")
    body = ("This is a paragraph that has been hard wrapped at a fixed column\n"
            "and continues onto the following line and keeps going further\n"
            "and yet another wrapped line of prose to trip the reflow heuristic\n")
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("doc.md", io.BytesIO(body.encode("utf-8")), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    content = resp.json()["markdown"]
    assert "\n" not in content.strip()  # folded into one logical line


@pytest.mark.asyncio
async def test_extract_text_txt_is_verbatim(client, admin_user, project_with_doc):
    """Non-markdown text is NOT reflowed (INVARIANT: do not reflow arbitrary .txt/code)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "TxtDoc")
    body = "line one is fairly long prose that\ncontinues on the next line here\n"
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("notes.txt", io.BytesIO(body.encode("utf-8")), "text/plain")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["markdown"] == body


@pytest.mark.asyncio
async def test_extract_text_json_accepted_as_text(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "JsonDoc")
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("data.json", io.BytesIO(b'{"a": 1}'), "application/json")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["markdown"] == '{"a": 1}'


@pytest.mark.asyncio
async def test_extract_text_rejects_binary(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "BinDoc")
    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("img.png", io.BytesIO(PNG_BYTES), "image/png")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_extract_text_creates_no_parent_reference(client, admin_user, project_with_doc):
    """Unlike upload-markdown, extract-text creates NO parent reference record."""
    from db import get_db

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "NoRefDoc")
    db = await get_db()
    before = await db.query(
        "SELECT count() AS n FROM documents WHERE project_id = $pid AND is_reference = true",
        {"pid": pid},
    )
    n_before = before[0]["n"] if before else 0

    resp = await client.post(
        "/api/documents/extract-text",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("doc.md", io.BytesIO(b"# plain text\n"), "text/markdown")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    after = await db.query(
        "SELECT count() AS n FROM documents WHERE project_id = $pid AND is_reference = true",
        {"pid": pid},
    )
    n_after = after[0]["n"] if after else 0
    assert n_after == n_before  # no new reference document


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
async def test_extract_text_docx_returns_cleaned_markdown(client, admin_user, project_with_doc):
    """A .docx → converter (mocked) → normalize → extract images → markdown returned."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "DocxDoc")
    converter_md = "# Report\n\nSome prose text here.\n"
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
    assert "Report" in data["markdown"]
    assert data["image_references"] == []


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


@pytest.mark.asyncio
async def test_extract_text_docx_converter_error_returns_502(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ErrDoc")
    with patch("routes.files.post_docx_to_converter", new_callable=AsyncMock,
               side_effect=RuntimeError("pandoc failed")):
        resp = await client.post(
            "/api/documents/extract-text",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("report.docx", io.BytesIO(DOCX_MAGIC),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_extract_text_docx_oversize_rejected(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "BigDoc")
    with patch("config.MAX_DOCX_SIZE_MB", 0):
        resp = await client.post(
            "/api/documents/extract-text",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("report.docx", io.BytesIO(DOCX_MAGIC),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 413


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
