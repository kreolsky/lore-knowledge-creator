"""Tests for the import_file agent/MCP tool (plan tool-surface-consolidation Step 2b).

ONE tool (import_file) replaces the former upload_document + upload_reference,
discriminated by `node_type`. text/markdown/.docx via the import pipeline
(markdown normalize for .md, image extraction); binary image/audio via the widget's
save path (thumbnail/transcription). Binary requires node_type="reference".

D2: plain `content` (authored text) is NOT an input — authored text goes to
create_document (which may propose). The .md normalize capability moved to
create_document's `normalize` flag (D3); those tests live here too. Auto-only on
BOTH surfaces (no ProposalKind) — the Pi driver sees status:"applied" and shows no
proposal card.

These pin the import-pipeline funnel (is_text_bytes gate, conditional
normalize_markdown, sync docx converter, extract_and_replace_images), the
record-factory branch (create_document_via_collab / create_reference_via_collab),
the binary format dispatch, the dual-surface invariant, and the access gates.
"""

import base64
import hashlib
import secrets
from unittest.mock import AsyncMock, patch

import pytest
from helpers import pinned_stt_url

# Minimal valid DOCX magic: a ZIP local-file-header signature.
DOCX_MAGIC = b"PK\x03\x04" + b"\x00" * 64

# 1x1 PNG bytes (magic-valid) — for the binary + image-extraction paths.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ─── Shared helpers (mirror test_append_move_tools / test_mcp_gateway_mutating) ─


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"up-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _mcp_hdr(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _make_doc(client, token, project_id, title, content="", parent_id=None):
    body = {"project_id": project_id, "title": title, "content": content}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _read_content(client, token, doc_id):
    resp = await client.get(f"/api/documents/{doc_id}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json().get("content") or ""


async def _read_doc(client, token, doc_id):
    """Read a document/reference row directly (references are NOT in the project
    tree — /api/projects/{id} filters is_reference=false)."""
    resp = await client.get(f"/api/documents/{doc_id}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rpc(method, params=None, *, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _result_text(resp_json):
    import json
    return json.loads(resp_json["result"]["content"][0]["text"])


def _is_error(resp_json):
    return resp_json.get("result", {}).get("isError", False)


async def _mcp_call(client, token, name, arguments=None):
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_mcp_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── sandbox_path test seams (D5: content_base64 left the Pi surface, so the
# binary pipeline is exercised through sandbox_path — the byte channel the agent
# actually has. Mirrors test_sandbox_file_bridge's sftp_seams pattern.) ──────


async def _make_internal_key(test_db, user_id, project_id):
    """An INTERNAL whole-project key — the kind the Pi driver mints for itself and
    the only kind the sandbox_path console gate accepts."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"up-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": True, "internal": True,
    })
    return token


@pytest.fixture
def sandbox_configured(monkeypatch):
    """Pretend a sandbox is configured (config.SANDBOX_ENABLED is imported BY VALUE
    at module import, so patch the consumer's own module attribute)."""
    from helpers import pin_tool_gates
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    pin_tool_gates(monkeypatch, sandbox=True)


@pytest.fixture
def sftp_seams(monkeypatch, sandbox_configured):
    """Replace the SFTP read seam with a mutable data knob so a test can feed
    arbitrary bytes (PNG / DOCX / MP3) as if read from the workspace."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    state = {"realpath": None, "data": PNG_BYTES}

    async def _fake_read_checked(ws_path, max_bytes):
        return state["realpath"] or ws_path, state["data"]

    monkeypatch.setattr(sandbox.files, "_sftp_read_checked", _fake_read_checked)
    return state


# ════════════════════════════════════════════════════════════════════════════
# create_document — normalize flag (D3: the .md normalize capability moved here
# from the removed upload `content` path)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_document_normalize_false_default_preserves_authored_text(
    client, test_db, admin_user, project_with_doc,
):
    """D3: normalize defaults to False — authored markdown must NOT be reflowed (it
    would rewrite text the user is about to review). Hard-wrapped prose survives."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    wrapped = (
        "This is a paragraph that has been hard wrapped at a fixed column\n"
        "and continues onto the following line and keeps going further\n"
    )
    resp = await client.post("/api/tool/create_document", json={
        "title": "Authored", "content": wrapped, "parent_id": None, "apply": "auto",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["doc_id"]
    content = await _read_content(client, token, doc_id)
    # Default False: the hard-wrapped newlines survive (no reflow).
    assert "hard wrapped at a fixed column\n" in content


@pytest.mark.asyncio
async def test_create_document_normalize_true_reflows_md_text(
    client, test_db, admin_user, project_with_doc,
):
    """D3: normalize=True applies normalize_markdown — hard-wrapped prose is
    reflowed (the capability that left the upload surface with `content` in D2)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    wrapped = (
        "This is a paragraph that has been hard wrapped at a fixed column\n"
        "and continues onto the following line and keeps going further\n"
        "and yet another wrapped line of prose to trip the reflow heuristic\n"
    )
    resp = await client.post("/api/tool/create_document", json={
        "title": "Imported", "content": wrapped, "normalize": True,
        "parent_id": None, "apply": "auto",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["doc_id"]
    content = await _read_content(client, token, doc_id)
    # Reflowed: the soft-wrapped newlines are folded into one logical line.
    assert "\n" not in content.strip()


# ════════════════════════════════════════════════════════════════════════════
# import_file — docx (sandbox_path + ZIP magic → sync converter) → document or
# reference. D5: content_base64 left the Pi surface; the binary pipeline is fed
# through sandbox_path (the byte channel the agent actually has).
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_docx_creates_document(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """A workspace docx (ZIP magic) → sync converter (mocked) → normalize → extract
    images → a NEW document is created with the converted markdown (is_reference=false)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = DOCX_MAGIC

    converted_md = "# Converted\n\nHello from the docx.\n"
    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock, return_value=converted_md):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "report.docx",
            "sandbox_path": "outputs/report.docx",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    content = await _read_content(client, token, data["doc_id"])
    assert "Hello from the docx" in content


@pytest.mark.asyncio
async def test_import_file_docx_oversize_rejected(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = DOCX_MAGIC

    with patch("config.MAX_DOCX_SIZE_MB", 0):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "big.docx", "sandbox_path": "outputs/big.docx",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_import_file_docx_converter_failure_is_502(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = DOCX_MAGIC

    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock, side_effect=RuntimeError("pandoc died")):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "bad.docx", "sandbox_path": "outputs/bad.docx",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 502, resp.text


@pytest.mark.asyncio
async def test_import_file_docx_reference_under_host(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """docx → import pipeline → a markdown reference attached to document_id (host)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "Host")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = DOCX_MAGIC

    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock, return_value="# Docx\n\nBody.\n"):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "r.docx", "node_type": "reference", "parent_id": host,
            "sandbox_path": "outputs/r.docx",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    assert data["reference_id"] == data["doc_id"]
    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["parent_id"] == host


# ════════════════════════════════════════════════════════════════════════════
# import_file — binary image/audio → reference (node_type="reference")
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_image_binary_creates_reference(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """binary image (sandbox_path) → import_file(node_type="reference") ACCEPTED
    (widget save path: magic validation + thumbnail enqueued)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "Host2")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = PNG_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": host,
        "sandbox_path": "outputs/pic.png",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["parent_id"] == host
    assert ref["media_type"] == "image"


async def _assert_image_reference_thumbnail_contract(
    client, token, resp, *, host,
):
    """Shared contract: an agent-created image reference must carry file_path +
    dimensions, enqueue thumbnail_task bound to the ref, and serve /thumb.

    Mirrors the spies/asserts in test_thumbnails.test_upload_image_enqueues_thumbnail
    + test_serve_thumbnail_on_demand, applied to the AGENT upload path so a
    regression here can no longer pass CI silently. Uses a Pillow-decodable PNG
    (the module-level PNG_BYTES has a broken data stream and would skip the
    dimension read silently).
    """
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    ref_id = data["reference_id"]

    ref = await _read_doc(client, token, ref_id)
    assert ref["is_reference"] is True
    assert ref["parent_id"] == host
    assert ref["media_type"] == "image"
    # file_path + dimensions are written by save_upload's image branch — the SAME
    # branch the widget hits. Missing file_path ⇒ the agent path diverged.
    assert ref.get("file_path"), "agent image reference must have a file_path"
    meta = ref.get("file_meta") or {}
    assert isinstance(meta.get("width"), int) and meta["width"] > 0, \
        "file_meta.width must be read from the decoded image"
    assert isinstance(meta.get("height"), int) and meta["height"] > 0, \
        "file_meta.height must be read from the decoded image"

    # GET /thumb serves 200 image/webp (on-demand generation when the worker
    # hasn't produced _thumb.webp yet — the fallback the gallery relies on).
    thumb_resp = await client.get(
        f"/api/files/{ref_id}/thumb",
        cookies={"lore_session": token},
    )
    assert thumb_resp.status_code == 200
    assert thumb_resp.headers["content-type"] == "image/webp"
    return ref_id


@pytest.mark.asyncio
async def test_import_file_image_binary_enqueues_thumbnail_and_serves(
    client, test_db, admin_user, project_with_doc, sftp_seams, enqueue_recorder,
):
    """Agent image-upload via sandbox_path MUST flow through the SAME save path as
    the widget: file_path + dimensions written, thumbnail_task enqueued with
    job_id thumb:{ref_id}, and GET /thumb serves 200. Pins the contract the bug
    report claimed was missing (verify-first: RED ⇒ agent path diverged)."""
    from test_thumbnails import _make_valid_png

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "ImgThumbHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    valid_png = _make_valid_png(4, 4)
    sftp_seams["data"] = valid_png

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": host,
        "sandbox_path": "outputs/pic.png",
    }, headers=_hdr(agent_tok))

    ref_id = await _assert_image_reference_thumbnail_contract(
        client, token, resp, host=host,
    )

    # save_upload must enqueue exactly one thumbnail_task, bound to this ref.
    thumb_calls = [(n, a, k) for n, a, k in enqueue_recorder.calls if n == "thumbnail_task"]
    assert len(thumb_calls) == 1, "image upload must enqueue one thumbnail_task"
    assert thumb_calls[0][2].get("job_id") == f"thumb:{ref_id}"


@pytest.mark.asyncio
async def test_import_file_audio_binary_enqueues_transcription(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """binary audio (sandbox_path) → import_file(node_type="reference") ACCEPTED
    (widget save path: magic validation + transcription enqueued when STT is
    configured)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "AudioHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)

    # Minimal MP3 frame (magic-valid: 0xFF 0xFB MPEG sync).
    mp3 = b"\xff\xfb\x90\x00" + b"\x00" * 400
    sftp_seams["data"] = mp3
    with pinned_stt_url(), \
            patch("transcription.enqueue_transcription", new_callable=AsyncMock) as enc:
        resp = await client.post("/api/tool/import_file", json={
            "filename": "clip.mp3", "node_type": "reference", "parent_id": host,
            "sandbox_path": "outputs/clip.mp3",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["media_type"] == "audio"
    assert enc.await_count == 1, "transcription must be enqueued for audio"


# ════════════════════════════════════════════════════════════════════════════
# import_file — .zip archive (sandbox_path) → media_type "file" reference
# (agent zip sharing; plan agent-zip-reference). Extension-first dispatch: a
# `.zip` NEVER reaches the docx converter even though both share the PK\x03\x04
# magic. The archive is never opened — only its magic is validated.
# ════════════════════════════════════════════════════════════════════════════


# Minimal zip payload: ZIP local-file-header magic + padding (magic-valid).
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 128


@pytest.mark.asyncio
async def test_import_file_zip_creates_file_reference(
    client, test_db, admin_user, project_with_doc, sftp_seams, enqueue_recorder,
):
    """A workspace .zip (sandbox_path) → a `media_type: "file"` reference: stored
    bytes served verbatim by the generic files route, mime application/zip, no
    processing_status, and NOTHING derived (no thumbnail, no transcription)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "ZipHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = ZIP_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "page.zip", "node_type": "reference", "parent_id": host,
        "sandbox_path": "outputs/page.zip",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["parent_id"] == host
    assert ref["media_type"] == "file"
    # None (or key-absent — the API drops NONE fields): a file ref derives nothing.
    assert not ref.get("processing_status")
    meta = ref.get("file_meta") or {}
    assert meta.get("mime_type") == "application/zip"
    assert meta.get("original_name") == "page.zip"
    assert meta.get("file_size") == len(ZIP_BYTES)

    # The generic authed serve route returns the stored bytes verbatim.
    serve = await client.get(
        f"/api/files/{data['reference_id']}/{meta['original_name']}",
        cookies={"lore_session": token},
    )
    assert serve.status_code == 200
    assert serve.content == ZIP_BYTES
    assert serve.headers["content-type"].startswith("application/zip")

    # A file reference derives nothing — no thumbnail_task, no other job.
    assert [n for n, _a, _k in enqueue_recorder.calls if n == "thumbnail_task"] == []


@pytest.mark.asyncio
async def test_import_file_zip_magic_mismatch_rejected(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """Non-zip bytes named .zip → 400 (magic must confirm the extension's claim)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "ZipMagicHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = b"definitely not a zip archive"

    resp = await client.post("/api/tool/import_file", json={
        "filename": "fake.zip", "node_type": "reference", "parent_id": host,
        "sandbox_path": "outputs/fake.zip",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_import_file_zip_oversize_rejected(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """A .zip above MAX_ARCHIVE_SIZE_MB → 413 (env-tunable archive cap)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "ZipBigHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = ZIP_BYTES

    with patch("config.MAX_ARCHIVE_SIZE_MB", 0):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "big.zip", "node_type": "reference", "parent_id": host,
            "sandbox_path": "outputs/big.zip",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_import_file_zip_with_docx_bytes_stays_archive(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """Zip/docx magic collision: DOCX bytes named `.zip` stay an ARCHIVE — the
    extension carries the intent, the docx converter is never invoked."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "ZipDocxHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = DOCX_MAGIC

    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock) as conv:
        resp = await client.post("/api/tool/import_file", json={
            "filename": "page.zip", "node_type": "reference", "parent_id": host,
            "sandbox_path": "outputs/page.zip",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    assert conv.await_count == 0, "a .zip must never reach the docx converter"
    ref = await _read_doc(client, token, resp.json()["reference_id"])
    assert ref["media_type"] == "file"


@pytest.mark.asyncio
async def test_import_file_zip_rejected_for_document_names_fix(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """An archive is never a document — default node_type rejects it (400) and the
    error names `node_type: "reference"` as the fix (mirrors image/audio)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = ZIP_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "page.zip",
        "sandbox_path": "outputs/page.zip",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 400, resp.text
    assert "node_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_import_file_binary_unsupported_extension_rejected(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """A binary whose extension is neither image nor audio is rejected (400), not
    silently stored."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "BinHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = b"\x00\x01\x02"

    resp = await client.post("/api/tool/import_file", json={
        "filename": "data.bin", "node_type": "reference", "parent_id": host,
        "sandbox_path": "outputs/data.bin",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_import_file_reference_without_document_id_rejected(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """A reference must attach to a host (parent_id). Mirrors create_document's
    'a reference requires parent_id' invariant — the two surfaces agree."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = PNG_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference",
        "sandbox_path": "outputs/pic.png",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 422, resp.text
    assert "parent_id" in resp.text


# ════════════════════════════════════════════════════════════════════════════
# binary REJECTED for a document (default node_type) — names node_type
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_binary_rejected_for_document_names_fix(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    """An image/audio binary is never a document — a default-node_type import_file
    rejects it (400) and the error names `node_type: "reference"` as the fix."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = PNG_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png",
        "sandbox_path": "outputs/pic.png",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 400, resp.text
    assert "node_type" in resp.json()["detail"]


# ════════════════════════════════════════════════════════════════════════════
# attachment_index — backend resolves an attached chat image (in-chat agent only)
# ════════════════════════════════════════════════════════════════════════════


async def _seed_user_message_with_images(
    test_db, session_id, msg_id, images, *, user_id, project_id, content="add this",
):
    """Insert an OWNED chat session row plus a user message row carrying `images`
    (data-URI list), mirroring how a chat turn persists attachments (chat_id =
    session_id, role='user'). The session row is required: attachment resolution
    validates X-Agent-Session-Id against chat_sessions before reading messages."""
    from db import create_record

    await create_record("chat_sessions", session_id, {
        "user_id": user_id, "project_id": project_id, "title": "",
    })
    await test_db.query("DELETE type::record('messages', $id)", {"id": msg_id})
    await create_record("messages", msg_id, {
        "chat_id": session_id, "role": "user", "content": content, "images": images,
    })


@pytest.mark.asyncio
async def test_import_file_attachment_index_resolves_image(
    client, test_db, admin_user, project_with_doc,
):
    """attachment_index (in-chat agent) → backend resolves the attached image from
    the latest user message's `images` (via X-Agent-Session-Id) and runs the SAME
    binary image path as content_base64 → an `image` reference under document_id."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "AttachHost")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    session_id = "test-att-session-0"
    data_uri = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-0", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": host,
        "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["parent_id"] == host
    assert ref["media_type"] == "image"


@pytest.mark.asyncio
async def test_import_file_attachment_index_enqueues_thumbnail_and_serves(
    client, test_db, admin_user, project_with_doc, enqueue_recorder,
):
    """attachment_index (in-chat agent) resolves an attached chat image and runs
    the SAME binary image path as content_base64 → the SAME thumbnail contract
    (file_path + dimensions + thumbnail_task + /thumb 200). The bug report's
    repro is exactly this flow, so it must be pinned end-to-end."""
    from test_thumbnails import _make_valid_png

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "AttachThumbHost")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    valid_png = _make_valid_png(4, 4)
    session_id = "test-att-session-thumb"
    data_uri = f"data:image/png;base64,{base64.b64encode(valid_png).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-thumb", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": host,
        "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})

    ref_id = await _assert_image_reference_thumbnail_contract(
        client, token, resp, host=host,
    )

    thumb_calls = [(n, a, k) for n, a, k in enqueue_recorder.calls if n == "thumbnail_task"]
    assert len(thumb_calls) == 1, "attachment image must enqueue one thumbnail_task"
    assert thumb_calls[0][2].get("job_id") == f"thumb:{ref_id}"


@pytest.mark.asyncio
async def test_import_file_attachment_index_as_document_rejected(
    client, test_db, admin_user, project_with_doc,
):
    """attachment_index resolves an attached IMAGE; an image is never a document, so
    node_type="document" is rejected (400) naming node_type as the fix —
    the same gate the content_base64 binary path hits, reached via the attachment
    resolution branch."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    session_id = "test-att-session-doc"
    data_uri = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-doc", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png",
        "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 400, resp.text
    assert "node_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_import_file_attachment_index_requires_session(
    client, test_db, admin_user, project_with_doc,
):
    """attachment_index without an in-chat session (no X-Agent-Session-Id, e.g. an
    external/MCP caller that shouldn't be able to use it) → 400, not a confusing 422."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": "host", "attachment_index": 0,
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 400, resp.text
    assert "chat session" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_attachment_index_refuses_a_foreign_session(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """attachment_index resolves images via X-Agent-Session-Id: a session id
    the caller does not OWN (foreign user's real session, or no session row at
    all) must be refused before any message is read — the messages query used
    to run on the raw header value, silently importing another user's
    attachment (cross-user image read). Refused with the SAME 400 the absent
    header gets: "not a resolvable chat session of yours" is one condition
    with several spellings, and none of them may leak what the other session
    holds."""
    from db import create_record

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    other_uid, _ = regular_user
    await create_record("project_members", "test-pm-att-foreign", {
        "project_id": pid, "user_id": other_uid, "access_level": "full",
    })
    host = await _make_doc(client, token, pid, "AttForeignHost")
    other_tok = await _make_agent_key(test_db, other_uid, pid, auto_apply=True)

    session_id = "test-att-session-foreign"
    data_uri = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"
    await create_record("chat_sessions", session_id, {
        "user_id": admin_uid, "project_id": pid, "title": "",
    })
    await create_record("messages", "test-att-msg-foreign", {
        "chat_id": session_id, "role": "user", "content": "x", "images": [data_uri],
    })

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": host,
        "attachment_index": 0,
    }, headers={**_hdr(other_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 400, resp.text
    assert "chat session" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_import_file_attachment_index_out_of_range(
    client, test_db, admin_user, project_with_doc,
):
    """An out-of-range index → actionable 400 naming the valid range."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    session_id = "test-att-session-1"
    data_uri = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-1", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": "host", "attachment_index": 5,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 400, resp.text
    assert "out of range" in resp.json()["detail"]


# ════════════════════════════════════════════════════════════════════════════
# D1 (plan internal-agent-surface-reconciliation): the BYTES decide the type on
# the attachment_index path — the model cannot see the bytes, so its filename
# guess may never override the mime the backend already holds (the data-URI
# header). The model's filename contributes its STEM only.
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_attachment_index_ignores_wrong_extension(
    client, test_db, admin_user, project_with_doc,
):
    """D1: a PNG attachment uploaded with a WRONG filename extension
    (character_art.jpg) still creates an image reference. The backend derives the
    extension from the data-URI mime (png), NOT from the model's filename — a wrong
    guess used to win and fail magic validation (400)."""
    from test_thumbnails import _make_valid_png

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "WrongExtHost")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    valid_png = _make_valid_png(4, 4)
    session_id = "test-att-wrongext"
    data_uri = f"data:image/png;base64,{base64.b64encode(valid_png).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-wrongext", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "character_art.jpg", "node_type": "reference", "parent_id": host,
        "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["media_type"] == "image"


@pytest.mark.asyncio
async def test_import_file_attachment_index_webp_creates_reference(
    client, test_db, admin_user, project_with_doc,
):
    """D1+D2: a webp attachment (the format the frontend emits for chat images)
    resolves to an image reference. Before D2 the .webp extension died as
    'Unsupported binary type' (stdlib mimetypes has no webp); before D1 the model's
    filename guess overrode the known mime → magic mismatch → 400."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "WebpHost")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # Minimal RIFF….WEBP — validate_magic keys off the RIFF magic + image/webp.
    webp = b"RIFF\x1a\x00\x00\x00WEBP" + b"\x00" * 32
    session_id = "test-att-webp"
    data_uri = f"data:image/webp;base64,{base64.b64encode(webp).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-webp", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "shot.webp", "node_type": "reference", "parent_id": host,
        "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["media_type"] == "image"


@pytest.mark.asyncio
async def test_import_file_attachment_index_normalizes_jpeg_to_jpg(
    client, test_db, admin_user, project_with_doc,
):
    """D1: a jpeg attachment keeps the project's jpg-not-jpeg naming convention
    (IMAGE_EXT_BY_MIME → image/jpeg maps to "jpg"). The model passes a wrong .png
    extension; the stored file lands as .jpg, never .jpeg."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "JpgHost")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # Minimal JPEG magic (FF D8 FF) — validate_magic keys off it for image/jpeg.
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    session_id = "test-att-jpg"
    data_uri = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-jpg", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "filename": "character_art.png", "node_type": "reference", "parent_id": host,
        "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 200, resp.text
    ref = await _read_doc(client, token, resp.json()["reference_id"])
    assert ref["media_type"] == "image"
    # The stored file follows the jpg-not-jpeg convention (plan D1 example).
    assert ref.get("file_path", "").endswith(".jpg"), ref.get("file_path")


@pytest.mark.asyncio
async def test_import_file_attachment_index_no_filename_uses_mime_ext(
    client, test_db, admin_user, project_with_doc,
):
    """D1: with no filename at all the extension comes from the attachment's mime
    (png → attachment-0.png) and the reference is created — the model need not guess
    a filename it cannot derive from bytes it cannot see."""
    from test_thumbnails import _make_valid_png

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "NoNameHost")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    valid_png = _make_valid_png(4, 4)
    session_id = "test-att-noname"
    data_uri = f"data:image/png;base64,{base64.b64encode(valid_png).decode()}"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-noname", [data_uri],
        user_id=admin_uid, project_id=pid,
    )

    resp = await client.post("/api/tool/import_file", json={
        "node_type": "reference", "parent_id": host, "attachment_index": 0,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["media_type"] == "image"


# ════════════════════════════════════════════════════════════════════════════
# exactly-one-input-form XOR (sandbox_path / attachment_index)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_requires_exactly_one_input_form(
    client, test_db, admin_user, project_with_doc,
):
    """Providing neither (or both) of sandbox_path / attachment_index is a 422
    (pydantic model_validator). D5: content_base64 is no longer an input form;
    D2: authored `content` is not an input form either."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # neither
    resp = await client.post("/api/tool/import_file", json={
        "filename": "x.md",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 422, resp.text
    # both sandbox_path + attachment_index
    resp = await client.post("/api/tool/import_file", json={
        "filename": "x.png", "sandbox_path": "ws/x.png",
        "attachment_index": 0,
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 422, resp.text

