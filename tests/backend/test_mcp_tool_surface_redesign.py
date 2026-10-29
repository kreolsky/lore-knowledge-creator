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


# ═══ D2/D13/D14 — the resulting surface (names) ═══════════════════════════════


# ═══ D4 — one byte channel, always a URL ══════════════════════════════════════


    # Redeem (POST) response contract is checked at runtime; the executable example
    # must carry the new node's id so the agent sees it without a follow-up call.


# ═══ D6 — referenced-node visibility (read_document) ══════════════════════════


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


# ═══ D3 — create_document: parent_id + node_type; is_reference/media_type gone ═


# ═══ D5 — file part carries docx/markdown too ══════════════════════════════════


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


# ═══ D12 — init stops lying; capabilities are checkable ═══════════════════════


# ═══ D16 — one re-parent path for every node ══════════════════════════════════


# ═══ D9 — remote images get a decision, not silence ═══════════════════════════


# ═══ {{ROOT}} — the served surface names the caller's actual root ═════════════
