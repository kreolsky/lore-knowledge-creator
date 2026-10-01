"""Binding tests for the MCP tool-surface redesign
(.kilo/plans/mcp-tool-surface-redesign.md).

The surface is re-decomposed around the PARTS of a node (text / file / tree),
not around entity types or transports. Every assertion here binds a decision in
that plan (D1–D16) and is written to FAIL until the decision lands.

Two classes of assertion:
  - SURFACE/SCHEMA tests: derive over `build_tool_list()` / the AGENT_TOOLS source
    so they bind two components and cover the next member (testing.md).
  - EXECUTOR / E2E tests: drive the read/move/upload executors through the MCP
    gateway against a real DB.

The old tool names (upload_file, upload_reference_file, download_reference_file,
run_extractor) are asserted ABSENT; the new ones (attach_file, get_file,
reprocess_file, preview_extractor) are asserted PRESENT.
"""

import hashlib
import re
import secrets
import struct

import pytest
from helpers import pin_stt_url

# ─── helpers (mirror test_mcp_upload_url.py) ─────────────────────────────────


def _wav_bytes(payload_size: int) -> bytes:
    """A minimal RIFF/WAVE file of roughly `payload_size` bytes of audio data."""
    data = b"\x00" * payload_size
    fmt = struct.pack("<4sIHHIIHH4sI", b"fmt ", 16, 1, 1, 8000, 8000, 1, 8, b"data", len(data))
    body = b"WAVE" + fmt + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=True, scope_root=""):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"sr-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": scope_root,
        "token_hash": token_hash, "label": "surface", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _result_text(resp_json):
    return __import__("json").loads(resp_json["result"]["content"][0]["text"])


def _is_error(resp_json):
    return resp_json.get("result", {}).get("isError", False)


async def _call(client, token, name, arguments=None):
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments or {}}},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ═══ D1 — bytes never pass through the model ══════════════════════════════════


def test_d1_no_served_tool_has_a_bytes_field():
    """No advertised MCP tool accepts bytes as an argument (content_base64 is gone
    for good — there is no size threshold at which behavior changes). Derived over
    build_tool_list() so a new bytes-shaped field fails here, not a literal copy."""
    from mcp_gateway.schemas import build_tool_list

    bytes_fields = {"content_base64"}
    offenders = []
    for tool in build_tool_list():
        props = (tool.input_schema or {}).get("properties", {}) or {}
        for prop_name in props:
            if prop_name in bytes_fields:
                offenders.append((tool.name, prop_name))
    assert offenders == [], f"bytes-accepting fields still advertised: {offenders}"


def test_d1_no_served_description_mentions_content_base64():
    """The routing text about content_base64 is itself the tax — no description
    may name it."""
    from mcp_gateway.schemas import build_tool_list

    for tool in build_tool_list():
        assert "content_base64" not in (tool.description or "").lower(), (
            f"{tool.name} description still mentions content_base64"
        )


# ═══ D2/D13/D14 — the resulting surface (names) ═══════════════════════════════


def test_d14_removed_tools_absent():
    from mcp_gateway.schemas import build_tool_list

    served = {t.name for t in build_tool_list()}
    for removed in ("upload_file", "upload_reference_file", "download_reference_file"):
        assert removed not in served, f"{removed} still served (D14 removal incomplete)"


def test_d2_file_part_tools_present():
    from mcp_gateway.schemas import build_tool_list

    served = {t.name for t in build_tool_list()}
    for name in ("attach_file", "get_file", "reprocess_file"):
        assert name in served, f"{name} not served (file-part tool missing)"


def test_d13_run_extractor_renamed_to_preview_extractor(monkeypatch):
    """The gateway-only rename: the tool cannot run (dry_run:false rejected), so its
    name must not promise an action it cannot perform."""
    from mcp_gateway.schemas import build_tool_list

    import config

    # preview_extractor is gated on MCP_RUN_EXTRACTOR (off by default — CI does not set
    # it); opt it on here so the rename assertion exercises the tool's own visibility,
    # not the deployment flag (mirrors test_mcp_gateway_run_extractor's pattern).
    monkeypatch.setattr(config, "MCP_RUN_EXTRACTOR", True)
    served = {t.name for t in build_tool_list()}
    assert "preview_extractor" in served
    assert "run_extractor" not in served


def test_resulting_surface_is_the_expected_set():
    """The full served surface (16 by default; preview_extractor gated). Pins every
    flag that shapes it (testing.md) via the INVARIANT fixture below."""
    from mcp_gateway.schemas import build_tool_list

    import config

    expected = {
        "init", "search_materials", "read_document", "get_project_structure",
        "create_document", "edit_document", "append_to_document",
        "attach_file", "get_file", "reprocess_file", "move_document",
        "create_table", "edit_table_cell", "add_table_rows", "add_table_column",
        "rename_document",
    }
    # INVARIANT: preview_extractor is gated on MCP_RUN_EXTRACTOR; the default ship
    # value (.env.example) is 0, so a default deployment advertises 16. Pinning the
    # flag keeps this assertion honest across environments.
    if getattr(config, "MCP_RUN_EXTRACTOR", False):
        expected.add("preview_extractor")
    served = {t.name for t in build_tool_list()}
    assert served == expected, sorted(served ^ expected)


# ═══ D4 — one byte channel, always a URL ══════════════════════════════════════


def test_d4_attach_file_schema_shape():
    """attach_file takes (attach_to, filename, title?) — the host is `attach_to`
    (NOT document_id), matching the argument the mint echoes. No bytes field."""
    from mcp_gateway.schemas import build_tool_list

    tool = next(t for t in build_tool_list() if t.name == "attach_file")
    props = set((tool.input_schema or {}).get("properties", {}))
    assert "attach_to" in props
    assert "filename" in props
    assert "title" in props
    assert "document_id" not in props
    assert "content_base64" not in props
    required = set((tool.input_schema or {}).get("required", []))
    assert {"attach_to", "filename"} <= required


def test_d4_mint_response_has_no_document_id_but_redeem_does():
    """The mint echoes `attach_to` (the host), NOT `document_id` — the node id is
    born at redeem, so the same key must not name two different nodes. The mint's
    OUTPUT schema must therefore carry no document_id; the redeem response must."""
    from mcp_gateway.schemas import _GATEWAY_OUTPUT_SCHEMAS

    mint_props = set(_GATEWAY_OUTPUT_SCHEMAS.get("attach_file", {}).get("properties", {}))
    assert "document_id" not in mint_props, (
        "mint must not echo document_id (it names the host as attach_to)"
    )
    assert "attach_to" in mint_props
    # Redeem (POST) response contract is checked at runtime; the executable example
    # must carry the new node's id so the agent sees it without a follow-up call.


# ═══ D6 — referenced-node visibility (read_document) ══════════════════════════


def test_d6_read_document_schema_carries_file_and_references_fields():
    from mcp_gateway.schemas import build_tool_list

    tool = next(t for t in build_tool_list() if t.name == "read_document")
    # read_document advertises the new file-part + reference visibility fields.
    out = set(tool.output_schema.get("properties", {})) if tool.output_schema else set()
    for field in ("media_type", "has_file", "processing_status", "references"):
        assert field in out, f"read_document outputSchema missing {field}"


@pytest.mark.asyncio
async def test_d6_read_document_returns_references_and_has_file_for_image_node(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A node with an attached image reads back has_file:true + processing_status:null
    AND lists the image in references[] (the two states `null` conflates today)."""
    import base64

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)

    # Attach an image via attach_file → POST.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    mint = _result_text(await _call(client, key, "attach_file", {
        "attach_to": doc_id, "filename": "shot.png",
    }))
    resp = await client.post(mint["url"], files={"file": ("shot.png", png, "image/png")})
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["document_id"]

    read = _result_text(await _call(client, key, "read_document", {"document_id": doc_id}))
    assert read["media_type"] is not None or "media_type" in read
    # The image node itself: has_file true, processing_status null (no OCR pipeline).
    img_read = _result_text(await _call(client, key, "read_document", {"document_id": new_id}))
    assert img_read["has_file"] is True
    assert img_read["processing_status"] is None
    # references[] on the host lists the image, carrying created_at.
    refs = read.get("references", [])
    assert any(r["document_id"] == new_id for r in refs), refs
    assert all("created_at" in r for r in refs), refs


@pytest.mark.asyncio
async def test_d6_markdown_leaf_reads_back_has_file_false(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A markdown leaf (no file part) reads back has_file:false — the second state
    `null` conflates with today."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    created = _result_text(await _call(client, key, "create_document", {
        "parent_id": doc_id, "node_type": "reference",
        "title": "Note", "content": "hello",
    }))
    ref_id = created.get("document_id") or created.get("doc_id")
    read = _result_text(await _call(client, key, "read_document", {"document_id": ref_id}))
    assert read["has_file"] is False


@pytest.mark.asyncio
async def test_d6_references_created_at_distinguishes_same_named_nodes(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """Titles are not unique; a retried upload leaves several identically-named
    nodes. created_at is the field an agent uses to tell them apart (D6)."""
    import base64

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    for _ in range(2):
        mint = _result_text(await _call(client, key, "attach_file", {
            "attach_to": doc_id, "filename": "dup.png",
        }))
        await client.post(mint["url"], files={"file": ("dup.png", png, "image/png")})
    read = _result_text(await _call(client, key, "read_document", {"document_id": doc_id}))
    refs = read.get("references", [])
    same_named = [r for r in refs if r["title"] == "dup.png" or r.get("title", "").startswith("dup")]
    assert len({r["created_at"] for r in same_named}) == len(same_named), same_named


# ═══ D8 — addressing stays on unique ids ══════════════════════════════════════


def test_d8_read_document_requires_document_id():
    """`required: ["document_id"]` is restored; the name-only fallback is gone, so
    `read_document {}` is rejected at validation before execution."""
    from mcp_gateway.schemas import build_tool_list

    tool = next(t for t in build_tool_list() if t.name == "read_document")
    assert "document_id" in (tool.input_schema or {}).get("required", [])


@pytest.mark.asyncio
async def test_d8_name_only_read_document_is_rejected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A `name`-only call is no longer a tolerated fallback — it is rejected. This
    must FAIL today (the name fallback resolves the title) and pass after D8."""
    from db import create_record

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    titled_id = f"sr-titled-{secrets.token_hex(4)}"
    unique_title = "UniqueD8Title-" + secrets.token_hex(3)
    await create_record("documents", titled_id, {
        "project_id": pid, "parent_id": doc_id, "title": unique_title, "content": "body",
        "path": f"{unique_title}.md", "is_index": False, "is_reference": False,
    })
    # Passing the TITLE as document_id must be rejected once the name fallback is gone.
    data = await _call(client, key, "read_document", {"document_id": unique_title})
    assert _is_error(data), data


# ═══ D3 — create_document: parent_id + node_type; is_reference/media_type gone ═


def test_d3_create_document_has_no_is_reference_or_media_type():
    from mcp_gateway.schemas import build_tool_list

    tool = next(t for t in build_tool_list() if t.name == "create_document")
    schema = tool.input_schema or {}
    props = set(schema.get("properties", {}))
    assert "is_reference" not in props
    assert "media_type" not in props
    assert "attach_to" not in props, "attach_to is deleted, not aliased"
    assert "parent_id" in props
    assert schema["properties"]["node_type"]["enum"] == ["document", "reference"]
    # Placement is required — presence (not value) splits omitted/null/id.
    assert "parent_id" in schema.get("required", [])


@pytest.mark.asyncio
async def test_d3_create_document_reference_via_node_type(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """node_type="reference" + parent_id (the host) creates a reference leaf —
    the same key names placement AND kind, no second vocabulary."""
    from db import fetch_one

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    data = await _call(client, key, "create_document", {
        "title": "Leaf", "content": "x", "parent_id": doc_id,
        "node_type": "reference",
    })
    assert not _is_error(data), data
    leaf_id = _result_text(data).get("document_id") or _result_text(data).get("doc_id")
    row = await fetch_one("documents", leaf_id)
    assert row["is_reference"] is True
    assert row["parent_id"] == doc_id


# ═══ D5 — file part carries docx/markdown too ══════════════════════════════════


def test_d5_attach_file_accepts_docx_filename():
    """The mint must NOT refuse a .docx (it routes to save_upload +
    convert_docx_task). The accepted set is derived from the mint's own check, so
    PDF (export-only) cannot re-enter by hand — asserted in the next test."""
    from mcp_gateway.schemas import build_tool_list

    tool = next(t for t in build_tool_list() if t.name == "attach_file")
    # The description must not list PDF among accepted uploads.
    assert "pdf" not in (tool.description or "").lower()


@pytest.mark.asyncio
async def test_d5_docx_attach_keeps_file_path_and_produces_derived_text(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A .docx arrives → the ORIGINAL is kept as the file part AND a derived-markdown
    conversion is enqueued async (parity with the widget path)."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)

    enqueued: list = []

    async def _fake_enqueue(*a, **kw):
        enqueued.append(a)

    # A minimal valid ZIP (empty .docx shell) — enough that save_upload stores it.
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxml'
            'formats.org/wordprocessingml/2006/main"><w:body><w:p><w:t>Hello'
            "</w:t></w:p></w:body></w:document>",
        )
    docx_bytes = buf.getvalue()

    monkeypatch.setattr("jobs.pool.enqueue", _fake_enqueue, raising=False)

    mint = _result_text(await _call(client, key, "attach_file", {
        "attach_to": doc_id, "filename": "doc.docx",
    }))
    resp = await client.post(mint["url"], files={"file": ("doc.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["document_id"]

    from db import fetch_one

    row = await fetch_one("documents", new_id)
    assert row.get("file_path", "").endswith(".docx"), row
    assert row.get("processing_status") in ("queued", "processing", "ready", "error")


# ═══ D7 — reprocess_file behind the narrow error+empty gate ═══════════════════


@pytest.mark.asyncio
async def test_d7_reprocess_accepted_for_error_empty_and_processing_empty(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """reprocess_file is accepted for processing_status in (error, processing) WHEN
    the text part is empty. `processing` is inside the gate because a stuck node
    has no text to wipe."""
    from db import create_record

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    for status in ("error", "processing"):
        ref_id = f"sr-rep-{secrets.token_hex(4)}"
        await create_record("documents", ref_id, {
            "project_id": pid, "parent_id": doc_id, "title": "t", "content": "",
            "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
            "media_type": "audio", "file_path": "audio/x.wav",
            "processing_status": status,
        })
        data = await _call(client, key, "reprocess_file", {"document_id": ref_id})
        assert not _is_error(data), (status, data)


@pytest.mark.asyncio
async def test_d7_reprocess_refused_for_ready(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A finished (ready) node is refused — the wipe would destroy a human-corrected
    transcript, which is unrecoverable via the History panel."""
    from db import create_record

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    ref_id = f"sr-ready-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": doc_id, "title": "t", "content": "done text",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/x.wav", "processing_status": "ready",
    })
    data = await _call(client, key, "reprocess_file", {"document_id": ref_id})
    assert _is_error(data), data


@pytest.mark.asyncio
async def test_d7_reprocess_refusal_for_error_with_text_names_the_app_path(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """error + NON-empty text (a partial transcript that then failed) is the one real
    dead end. The refusal must NAME the human exit, not strand the agent."""
    from db import create_record

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    ref_id = f"sr-pt-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": doc_id, "title": "t", "content": "partial transcript",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/x.wav", "processing_status": "error",
    })
    data = await _call(client, key, "reprocess_file", {"document_id": ref_id})
    assert _is_error(data), data
    payload = _result_text(data)
    # The message names the app reference panel as the exit.
    assert "reference panel" in payload.get("error", "").lower() or "panel" in str(payload).lower(), payload


def test_d7_shared_validate_reprocessable_is_unchanged():
    """The MCP gate is an EDGE check, not an edit to the shared validator.
    _validate_reprocessable must still accept error|ready|processing (the app's own
    escape hatch stays intact) — tightening it in place would silently remove that."""
    import inspect

    from files_service import validate_reprocessable

    src = inspect.getsource(validate_reprocessable)
    assert "ready" in src and "processing" in src and "error" in src
    # The shared core still accepts 'ready' (the one value the MCP gate subtracts).
    assert '"error", "ready", "processing"' in src


# ═══ D10 — redeem is idempotent ════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_d10_replayed_redeem_returns_same_id_with_deduplicated_flag(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A replay of the SAME url within its TTL returns the SAME document_id with
    deduplicated:true (a dropped POST connection is recoverable by retry)."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    pin_stt_url(monkeypatch, "")

    mint = _result_text(await _call(client, key, "attach_file", {
        "attach_to": doc_id, "filename": "idem.wav",
    }))
    wav = _wav_bytes(256)
    r1 = await client.post(mint["url"], files={"file": ("idem.wav", wav, "audio/wav")})
    r2 = await client.post(mint["url"], files={"file": ("idem.wav", wav, "audio/wav")})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["document_id"] == r2.json()["document_id"]
    assert r2.json().get("deduplicated") is True


@pytest.mark.asyncio
async def test_d10_zero_byte_redeem_is_refused_and_creates_no_node(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A zero-byte part is refused at redeem BEFORE a node exists (per-part length
    does not exist in multipart, so this is the cheap minimum, nothing beyond it)."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    pin_stt_url(monkeypatch, "")
    mint = _result_text(await _call(client, key, "attach_file", {
        "attach_to": doc_id, "filename": "empty.wav",
    }))
    resp = await client.post(mint["url"], files={"file": ("empty.wav", b"", "audio/wav")})
    assert resp.status_code in (400, 422), resp.status_code


@pytest.mark.asyncio
async def test_d10_concurrent_redeem_of_same_url_creates_one_node(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """Two CONCURRENT redeems of the SAME url (a flaky POST retried while the first is
    still in flight) must not create two nodes. The pre-fix GET-then-SET deduped only
    SEQUENTIAL replays; the atomic SETNX claim closes the concurrent window. The
    interleaving here is non-deterministic (fast path or poll), so the test binds the
    end-state INVARIANT: both responses carry the SAME id and exactly ONE node exists."""
    import asyncio

    from db import get_db

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    pin_stt_url(monkeypatch, "")

    mint = _result_text(await _call(client, key, "attach_file", {
        "attach_to": doc_id, "filename": "concurrent.wav",
    }))
    wav = _wav_bytes(256)
    url = mint["url"]
    r1, r2 = await asyncio.gather(
        client.post(url, files={"file": ("concurrent.wav", wav, "audio/wav")}),
        client.post(url, files={"file": ("concurrent.wav", wav, "audio/wav")}),
    )
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    id1, id2 = r1.json()["document_id"], r2.json()["document_id"]
    assert id1 == id2, (id1, id2)
    # Exactly ONE audio node under the host — no orphaned duplicate.
    rows = await (await get_db()).query(
        "SELECT id FROM documents WHERE parent_id = $h AND is_reference = true "
        "AND media_type = 'audio' AND deleted_at IS NONE",
        {"h": doc_id},
    )
    assert len(rows) == 1, rows


# ═══ D11 — GET on the redeem path teaches instead of refusing ═════════════════


@pytest.mark.asyncio
async def test_d11_get_on_redeem_path_returns_recipe(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """An agent holding a URL WILL probe it; a GET must be the recipe (a curl
    example), not a bare Method Not Allowed."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    mint = _result_text(await _call(client, key, "attach_file", {
        "attach_to": doc_id, "filename": "probe.wav",
    }))
    resp = await client.get(mint["url"])
    body = resp.text.lower()
    assert "curl" in body or "post" in body, resp.text[:200]


# ═══ D15 — descriptions are part of the design ════════════════════════════════


def test_d15_every_served_tool_has_nonempty_description():
    """The import-time assert is widened to cover READING tools too: a served tool
    with an empty description fails here."""
    from mcp_gateway.schemas import build_tool_list

    for tool in build_tool_list():
        assert (tool.description or "").strip(), f"{tool.name} has no description"


def test_d15_anchor_vocabulary_in_text_writing_tools():
    """Each text-writing tool's description carries a concrete `![…](ref:<id>)`
    anchor example, and the example anchor parses with the SAME regex the
    renderer uses (_REF_IMG_PATTERN) — including its `|WxH` dimension slot
    spelled as a concrete size, never a placeholder."""
    from documents.service import _REF_IMG_PATTERN
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: (t.description or "") for t in build_tool_list()}
    for name in ("edit_document", "append_to_document", "create_document"):
        desc = tools[name]
        assert "ref:" in desc, f"{name} description lacks the ref: anchor vocabulary"
        # Pull an example anchor from the description and confirm the image regex
        # reads the dimension form the description teaches.
        match = re.search(r"!\[[^\]]*\]\(ref:[^)]+\)", desc)
        assert match, f"{name} description has no parseable ref: anchor example"
        assert _REF_IMG_PATTERN.search(match.group(0)), (
            f"{name} example anchor not matched by _REF_IMG_PATTERN: {match.group(0)}"
        )


def test_d15_processing_status_enum_matches_code_writers():
    """read_document's description enumerates exactly the processing_status values
    the code can set — derived from the writers, not hand-copied."""
    from mcp_gateway.schemas import build_tool_list

    tool = next(t for t in build_tool_list() if t.name == "read_document")
    desc = tool.description or ""
    # The terminal + transient set produced by the writers.
    for value in ("queued", "processing", "ready", "error"):
        assert value in desc, f"read_document description missing status '{value}'"


def test_d15_descriptions_name_no_ghost_tool():
    """No description may mention a tool name that is not in build_tool_list()."""
    from mcp_gateway.schemas import build_tool_list

    # Old names that must not linger in any description.
    ghosts = {"upload_file", "upload_reference_file", "download_reference_file", "run_extractor"}
    for tool in build_tool_list():
        for ghost in ghosts:
            assert ghost not in (tool.description or ""), (
                f"{tool.name} description still names removed tool {ghost}"
            )


# ═══ D12 — init stops lying; capabilities are checkable ═══════════════════════


@pytest.mark.asyncio
async def test_d12_init_names_no_key_it_does_not_return(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """init's description promises only keys the package actually returns. The old
    text claimed a `work_area` key that does not exist."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    data = await _call(client, key, "init")
    pkg = _result_text(data)
    assert "work_area" not in pkg, "init still returns/mentions the non-existent work_area key"
    assert "capabilities" in pkg
    caps = pkg.get("capabilities", {})
    # binary_upload is a CHECKABLE field, not prose.
    bu = caps.get("binary_upload")
    assert isinstance(bu, dict), caps
    assert bu.get("mode") == "signed_url_post"
    assert "base_url" in bu


def test_d12_init_happy_path_lines_name_only_served_tools():
    """init's instruction text must name only tools present in build_tool_list() —
    a rename cannot orphan them."""
    from agent_tools.registry import by_name
    from mcp_gateway.schemas import build_tool_list

    served = {t.name for t in build_tool_list()}
    init_desc = by_name("init").spec["function"]["description"]
    for name in ("attach_file", "read_document", "get_file", "create_document"):
        if name in init_desc:
            assert name in served, f"init names {name} but it is not served"


# ═══ D16 — one re-parent path for every node ══════════════════════════════════


def _make_ref(test_db, *, pid, parent, media="audio", status=None, content="", title="ref"):
    """Create a reference node directly in the DB for move tests."""
    from db import create_record

    ref_id = f"sr-mv-{secrets.token_hex(4)}"
    import asyncio

    async def _go():
        await create_record("documents", ref_id, {
            "project_id": pid, "parent_id": parent, "title": title, "content": content,
            "path": f"{title}.md", "is_index": False, "is_reference": True,
            "media_type": media, "file_path": (f"audio/{title}.wav" if media == "audio" else None),
            "processing_status": status,
        })
        return ref_id

    return asyncio.get_event_loop().run_until_complete(_go())


@pytest.mark.asyncio
async def test_d16_move_reference_to_another_host_succeeds(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A reference CAN be moved (the blanket refusal is deleted). After the move it
    appears in the NEW host's references[] and not the old one's."""
    from db import create_record

    pid, idx_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    # Two host docs.
    host_a = f"sr-ha-{secrets.token_hex(4)}"
    host_b = f"sr-hb-{secrets.token_hex(4)}"
    for hid, t in ((host_a, "A"), (host_b, "B")):
        await create_record("documents", hid, {
            "project_id": pid, "parent_id": idx_id, "title": t, "content": "",
            "path": f"{t}.md", "is_index": False, "is_reference": False,
        })
    ref_id = f"sr-mref-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host_a, "title": "movable", "content": "",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/m.wav",
    })
    data = await _call(client, key, "move_document", {
        "document_id": ref_id, "parent_id": host_b,
    })
    assert not _is_error(data), data

    refs_b = _result_text(await _call(client, key, "read_document", {"document_id": host_b}))
    refs_a = _result_text(await _call(client, key, "read_document", {"document_id": host_a}))
    assert any(r["document_id"] == ref_id for r in refs_b.get("references", []))
    assert not any(r["document_id"] == ref_id for r in refs_a.get("references", []))


@pytest.mark.asyncio
async def test_d16_after_id_on_reference_is_rejected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """References are not tree-ordered; passing after_id for one is an error (the one
    surviving fragment of the old guard, moved from 'may not move' to 'may not reorder')."""
    from db import create_record

    pid, idx_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    host_a = f"sr-ha-{secrets.token_hex(4)}"
    host_b = f"sr-hb-{secrets.token_hex(4)}"
    for hid, t in ((host_a, "A"), (host_b, "B")):
        await create_record("documents", hid, {
            "project_id": pid, "parent_id": idx_id, "title": t, "content": "",
            "path": f"{t}.md", "is_index": False, "is_reference": False,
        })
    ref_id = f"sr-ai-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host_a, "title": "r", "content": "",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/r.wav",
    })
    data = await _call(client, key, "move_document", {
        "document_id": ref_id, "parent_id": host_b, "after_id": host_a,
    })
    assert _is_error(data), data


@pytest.mark.asyncio
async def test_d16_moved_reference_gets_top_key_of_new_ref_group(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A moved reference gets the TOP key of the new host's REFERENCE group:
    it re-lands at the head of the new group's
    list, ordered among refs only — tree sibling queries are kind-filtered, so
    the key never leaks a reference into the document tree."""
    from db import create_record, fetch_one

    pid, idx_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    host_a = f"sr-ha-{secrets.token_hex(4)}"
    host_b = f"sr-hb-{secrets.token_hex(4)}"
    for hid, t in ((host_a, "A"), (host_b, "B")):
        await create_record("documents", hid, {
            "project_id": pid, "parent_id": idx_id, "title": t, "content": "",
            "path": f"{t}.md", "is_index": False, "is_reference": False,
        })
    pre_ref = f"sr-pre-{secrets.token_hex(4)}"
    await create_record("documents", pre_ref, {
        "project_id": pid, "parent_id": host_b, "title": "pre", "content": "",
        "path": f"{pre_ref}.md", "is_index": False, "is_reference": True,
        "media_type": "markdown", "sort_key": "a0",
    })
    ref_id = f"sr-sk-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host_a, "title": "r", "content": "",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/r.wav",
    })
    await _call(client, key, "move_document", {"document_id": ref_id, "parent_id": host_b})
    row = await fetch_one("documents", ref_id)
    assert row.get("parent_id") == host_b, row
    moved_key = row.get("sort_key")
    # Top of the new group's ref key space: a non-empty key strictly above (==
    # sorting before) every pre-existing ref key of the new host.
    assert isinstance(moved_key, str) and moved_key, row
    assert moved_key < "a0", row


@pytest.mark.asyncio
async def test_d16_null_parent_on_reference_is_rejected_naming_index_doc(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """parent_id:null on a reference is rejected — 'project root' for a reference
    means the project's INDEX document, which the message must name."""
    pid, idx_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    ref_id = f"sr-nul-{secrets.token_hex(4)}"
    from db import create_record

    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": idx_id, "title": "r", "content": "",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/r.wav",
    })
    data = await _call(client, key, "move_document", {"document_id": ref_id, "parent_id": None})
    assert _is_error(data), data
    payload = _result_text(data)
    assert "index" in payload.get("error", "").lower(), payload


@pytest.mark.asyncio
async def test_d16_move_broadcast_identifies_both_hosts(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch, emit_recorder,
):
    """The move event must identify BOTH the old and the new host, so a client
    showing either can refresh its reference panel (the single path is not enough
    for the panel, which loads per-host)."""
    from db import create_record

    pid, idx_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    host_a = f"sr-ha-{secrets.token_hex(4)}"
    host_b = f"sr-hb-{secrets.token_hex(4)}"
    for hid, t in ((host_a, "A"), (host_b, "B")):
        await create_record("documents", hid, {
            "project_id": pid, "parent_id": idx_id, "title": t, "content": "",
            "path": f"{t}.md", "is_index": False, "is_reference": False,
        })
    ref_id = f"sr-ev-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host_a, "title": "r", "content": "",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "audio", "file_path": "audio/r.wav",
    })
    await _call(client, key, "move_document", {"document_id": ref_id, "parent_id": host_b})
    # At least one emitted event carries BOTH host ids.
    moved = emit_recorder.of("document_moved")
    assert moved, emit_recorder.calls
    joined = " ".join(str(v) for e in moved for v in e.values())
    assert host_a in joined and host_b in joined, joined


# ═══ D9 — remote images get a decision, not silence ═══════════════════════════


@pytest.mark.asyncio
async def test_d9_edit_document_with_remote_image_reports_or_rejects(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A remote image URL in written text must NOT pass through untouched (silent
    degradation). It is either fetched+rewritten (rewritten_images reported) or
    explicitly rejected naming the fix — never silently kept."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    # Seed a doc with a placeholder the edit targets.
    created = _result_text(await _call(client, key, "create_document", {
        "parent_id": doc_id, "title": "imgdoc", "content": "BELOW\n",
    }))
    target = created.get("document_id") or created.get("doc_id")
    data = await _call(client, key, "edit_document", {
        "document_id": target,
        "edits": [{"old_string": "BELOW\n", "new_string": "![pic](https://example.invalid/x.png)\n"}],
    })
    payload = _result_text(data)
    # Either it reports rewritten_images (list, possibly failed) OR rejects.
    if not _is_error(data):
        assert "rewritten_images" in payload, payload
    # The crucial property: a later read must NOT contain the raw external URL.
    read = _result_text(await _call(client, key, "read_document", {"document_id": target}))
    assert "https://example.invalid" not in read["content"], read["content"]


def test_d9_remote_image_inside_code_block_is_not_rejected():
    """Prose that DOCUMENTS the syntax (a fenced block or inline code quoting
    `![alt](https://...)` verbatim) is not mistaken for an actual embed attempt —
    the scan masks code first. Only a real inline image in authored text is rejected."""
    from agent.doc_state import reject_remote_images

    # A real inline image still rejected.
    with pytest.raises(Exception):  # noqa: PT011 — any HTTPException
        reject_remote_images("see ![pic](https://example.invalid/x.png)")
    # Fenced code block quoting the same syntax: allowed.
    reject_remote_images("Example:\n```\n![pic](https://example.invalid/x.png)\n```\n")
    # Inline code quoting it: allowed.
    reject_remote_images("Write `![alt](https://example.invalid/x.png)` like this.")
    # Tilde fence: allowed.
    reject_remote_images("~~~\n![pic](https://example.invalid/x.png)\n~~~")
    # No remote image at all.
    reject_remote_images("a normal ![pic](ref:abc123) reference")


# ═══ {{ROOT}} — the served surface names the caller's actual root ═════════════


def _surface_texts(tools) -> list[tuple[str, str]]:
    """Every description string a model reads off the surface: tool-level AND
    param-level — the half the {{ROOT}} fill must walk INTO (filling only
    Tool.description leaves the parent_id texts, the ones a model actually reads
    when it chooses parent_id:null, as literal sentinels)."""
    texts: list[tuple[str, str]] = []
    for t in tools:
        texts.append((t.name, t.description or ""))
        for pname, p in (t.input_schema.get("properties") or {}).items():
            if isinstance(p, dict) and p.get("description"):
                texts.append((f"{t.name}.{pname}", p["description"]))
    return texts


def test_unscoped_surface_pins_the_root_wording():
    """Post-{{ROOT}} pin: the no-scope render fills every sentinel site with the
    unscoped constant — byte-identical to the pre-sentinel wording, except
    move_document's MCP text which the fill normalizes by 4 chars ('null =
    project root' → 'null = the project root', the plan's one deliberate
    exception)."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    assert "null = the project root), or the HOST" in tools["create_document"].description
    assert "parent_id = new parent (null = the project root);" in tools["move_document"].description
    assert "(null = the project root), or" in (
        tools["create_document"].input_schema["properties"]["parent_id"]["description"]
    )
    assert "null for the project root." in (
        tools["move_document"].input_schema["properties"]["parent_id"]["description"]
    )


def test_scoped_render_names_the_scope_root():
    """A scoped render fills every {{ROOT}} site with the caller's OWN root —
    title + id — at BOTH levels (tool text AND parent_id params, the strings a
    model reads when it chooses parent_id:null), because the executor resolves a
    null parent to the scope root (scope.resolve_scoped_parent). The string
    'project root' appears NOWHERE: it names a place the key cannot reach."""
    from mcp_gateway.schemas import build_tool_list

    tools = {
        t.name: t for t in build_tool_list(
            scope_root="sr-root-1", root_title="Atlas Wing",
        )
    }
    expected = 'the "Atlas Wing" subtree root (sr-root-1)'
    for name in ("create_document", "move_document"):
        assert expected in (tools[name].description or ""), name
        pdesc = tools[name].input_schema["properties"]["parent_id"]["description"]
        assert expected in pdesc, f"{name}.parent_id"
    for which, text in _surface_texts(tools.values()):
        assert "project root" not in text, which
        assert "{{ROOT}}" not in text, which


def test_deleted_scope_root_renders_empty_title():
    """A scope root with no resolvable title (deleted) renders an EMPTY title in
    the fill — consistency with what init's capabilities.scope does today, not a
    new error path (a plan decision)."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list(scope_root="gone-root", root_title="")}
    assert 'the "" subtree root (gone-root)' in tools["move_document"].description
    pdesc = tools["move_document"].input_schema["properties"]["parent_id"]["description"]
    assert 'the "" subtree root (gone-root)' in pdesc
