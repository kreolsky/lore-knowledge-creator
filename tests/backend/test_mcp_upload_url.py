"""MCP upload_reference_file — signed-URL transport for LARGE binary uploads.

Symmetry with download_reference_file (which already solved the mirror problem):
bytes could come OUT over a signed URL but could only go IN as `content_base64`
inlined in the JSON-RPC args — capped at the 1MB `_JSON_MAX_BYTES` body limit that
also governs /mcp. A 14MB audio chunk was therefore unuploadable over MCP at all,
while the browser widget streams the same file at MAX_AUDIO_SIZE_MB (500MB).

The tool mints a short-lived token binding (project_id, document_id, filename,
title, user_id, scope_root); an UNAUTHENTICATED multipart route redeems it into the
SAME widget save path (save_audio_upload / save_upload + enqueue_transcription).

Acceptance bound here:
  - a >1MB multipart body succeeds (the regression the whole change exists for);
  - the created reference is byte-identical to what was POSTed and transcription is
    enqueued, exactly as the widget route does;
  - the token is not a session: expired → 403, and it cannot be widened.
"""

import hashlib
import json
import secrets
import struct

import pytest
from helpers import pin_stt_url

# ─── helpers (mirror test_tool_surface_consolidation's download slice) ────────


def _wav_bytes(payload_size: int) -> bytes:
    """A minimal RIFF/WAVE file of roughly `payload_size` bytes of audio data.

    RIFF magic is what validate_magic checks for audio/wav; the rest only has to be
    structurally plausible (nothing decodes it in these tests).
    """
    data = b"\x00" * payload_size
    fmt = struct.pack("<4sIHHIIHH4sI", b"fmt ", 16, 1, 1, 8000, 8000, 1, 8, b"data", len(data))
    body = b"WAVE" + fmt + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=True, scope_root=""):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"ul-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": scope_root,
        "token_hash": token_hash,
        # S1 (plan 1786740208300): a LABELED external key takes the byline on its
        # writes (tests/backend/test_agent_attribution.py). These tests bind
        # "attributed to the MINTING USER", so the fixture key carries NO label —
        # the byline falls back to the user's name, as before S1.
        "label": "", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _result_text(resp_json):
    return json.loads(resp_json["result"]["content"][0]["text"])


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


async def _make_doc(client, token, project_id, title, content=""):
    """Create a document via the REST API (session cookie) and return its id."""
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


# ─── surface ─────────────────────────────────────────────────────────────────


def test_upload_reference_file_is_advertised():
    """The mint tool is on the MCP surface — the symmetric counterpart of
    download_reference_file."""
    from mcp_gateway.schemas import build_tool_list

    advertised = {t.name for t in build_tool_list()}
    assert "attach_file" in advertised


def test_upload_redeem_route_is_exempt_from_the_json_body_limit():
    """INVARIANT: the redeem route MUST be in _UPLOAD_PATHS.

    Asserted over the DERIVED source (main._UPLOAD_PATHS), not a literal copy of the
    path: without the exemption the 1MB `_JSON_MAX_BYTES` middleware rejects the very
    large upload this feature exists to enable, and it would fail as a 413 with no
    hint of which layer refused.
    """
    from mcp_gateway.upload import MCP_UPLOAD_ROUTE_PREFIX

    import main

    assert any(MCP_UPLOAD_ROUTE_PREFIX.startswith(p) for p in main._UPLOAD_PATHS)


# ─── e2e ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_audio_over_signed_url_creates_reference_and_enqueues(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """Acceptance: mint → multipart POST → a reference exists with the exact bytes
    on disk, and transcription is enqueued (the widget's behavior, reached by an
    agent key)."""
    from config import STORAGE_PATH

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)

    enqueued: list[tuple] = []

    async def _fake_enqueue(user_id, ref_id):
        enqueued.append((user_id, ref_id))

    monkeypatch.setattr("routes.files_mcp_upload.enqueue_transcription", _fake_enqueue)
    pin_stt_url(monkeypatch, "http://stt.test")

    data = await _call(client, key, "attach_file", {
        "filename": "chunk_001.wav", "attach_to": doc_id, "title": "Chunk 1",
    })
    assert not _is_error(data), data
    payload = _result_text(data)
    assert "/api/mcp/upload/" in payload["url"]
    assert payload["expires_at"]

    wav = _wav_bytes(2048)
    resp = await client.post(payload["url"], files={"file": ("chunk_001.wav", wav, "audio/wav")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ref_id = body["document_id"]

    from db import fetch_one

    row = await fetch_one("documents", ref_id)
    assert row["is_reference"] is True
    assert row["media_type"] == "audio"
    assert row["project_id"] == pid
    assert row["parent_id"] == doc_id or row.get("parent_id") == doc_id
    # The redeem is attributed to the MINTING user (review fix R4) — the same
    # human transcription is enqueued for below.
    assert row["created_by"] == admin_uid
    assert row["created_by_name"] == "testadmin"
    stored = STORAGE_PATH / row["file_path"]
    assert stored.read_bytes() == wav
    assert enqueued == [(admin_uid, ref_id)]


@pytest.mark.asyncio
async def test_upload_over_one_megabyte_succeeds(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """THE regression: a body larger than _JSON_MAX_BYTES (1MB) must go through.

    This is what `content_base64` over /mcp could never do — a 14MB audio chunk was
    rejected at the middleware before reaching any upload logic.
    """
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    pin_stt_url(monkeypatch, "")

    payload = _result_text(await _call(client, key, "attach_file", {
        "filename": "big.wav", "attach_to": doc_id,
    }))
    big = _wav_bytes(1_500_000)
    assert len(big) > 1024 * 1024

    resp = await client.post(payload["url"], files={"file": ("big.wav", big, "audio/wav")})
    assert resp.status_code == 200, resp.text

    from config import STORAGE_PATH
    from db import fetch_one

    row = await fetch_one("documents", resp.json()["document_id"])
    assert (STORAGE_PATH / row["file_path"]).stat().st_size == len(big)


@pytest.mark.asyncio
async def test_upload_image_over_signed_url(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Parity with the widget's other binary branch: an image lands via save_upload
    (no transcription), so the tool is not audio-only."""
    import base64

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    payload = _result_text(await _call(client, key, "attach_file", {
        "filename": "shot.png", "attach_to": doc_id,
    }))
    resp = await client.post(payload["url"], files={"file": ("shot.png", png, "image/png")})
    assert resp.status_code == 200, resp.text

    from db import fetch_one

    row = await fetch_one("documents", resp.json()["document_id"])
    assert row["media_type"] == "image"
    assert row["created_by"] == admin_uid
    assert row["created_by_name"] == "testadmin"


# ─── security ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_upload_token_is_403(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """INVARIANT(security): the token expires. A 0-TTL mint is already dead on
    redeem — the TTL is what stops a leaked URL from being a standing write grant."""
    import config

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    monkeypatch.setattr(config, "MCP_UPLOAD_TOKEN_TTL_S", 0)

    payload = _result_text(await _call(client, key, "attach_file", {
        "filename": "late.wav", "attach_to": doc_id,
    }))
    resp = await client.post(
        payload["url"], files={"file": ("late.wav", _wav_bytes(64), "audio/wav")},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_tampered_upload_token_is_403(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A forged/garbage token is a uniform 403 — the signature is the authorization."""
    resp = await client.post(
        "/api/mcp/upload/not-a-real-token",
        files={"file": ("x.wav", _wav_bytes(64), "audio/wav")},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_readonly_key_cannot_mint_an_upload_url(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A read-only MCP key is refused at MINT. Testing the principal that must be
    REFUSED: minting is the write — the redeem route has no key to check, so a
    read-only key that could mint would hold an unauthenticated write grant."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)

    data = await _call(client, key, "attach_file", {
        "filename": "nope.wav", "attach_to": doc_id,
    })
    assert _is_error(data), data
    assert _result_text(data)["status_code"] == 403


@pytest.mark.asyncio
async def test_out_of_scope_mint_403_names_the_scope_root(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """An upload mint against a document_id OUTSIDE the key's subtree is a 403 whose
    detail names the scope root — the recovery the agent needs to re-target an
    in-scope host document instead of concluding 'uploading is impossible'."""
    pid, root_doc, admin_uid = project_with_doc
    _, admin_token = admin_user
    # Host document OUTSIDE the key's subtree (top-level, not under root_doc).
    out_doc = await _make_doc(client, admin_token, pid, "Outside", "x")
    # Read-write key scoped to root_doc's subtree.
    key = await _make_agent_key(test_db, admin_uid, pid, scope_root=root_doc)

    data = await _call(client, key, "attach_file", {
        "filename": "x.wav", "attach_to": out_doc,
    })
    assert _is_error(data), data
    payload = _result_text(data)
    assert payload["status_code"] == 403, payload
    # The detail names the scope root so the agent can re-target in-scope.
    assert root_doc in payload["error"]


@pytest.mark.asyncio
async def test_host_document_from_another_project_is_uniform_404(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Cross-project host document → the uniform 404 (no existence oracle), checked
    at mint time so a dead URL is never handed out."""
    pid, _doc_id, admin_uid = project_with_doc
    other_pid = f"ul-other-{secrets.token_hex(4)}"
    await test_db.query(
        "CREATE type::record('projects', $id) SET name='Other', status='active', "
        "project_context='', owner_id=$uid",
        {"id": other_pid, "uid": admin_uid},
    )
    foreign_doc = f"ul-foreign-{secrets.token_hex(4)}"
    from db import create_record

    await create_record("documents", foreign_doc, {
        "project_id": other_pid, "parent_id": None, "title": "Foreign",
        "content": "", "path": "foreign.md", "is_index": False, "is_reference": False,
    })
    key = await _make_agent_key(test_db, admin_uid, pid)

    data = await _call(client, key, "attach_file", {
        "filename": "x.wav", "attach_to": foreign_doc,
    })
    assert _is_error(data), data
    assert _result_text(data)["status_code"] == 404


@pytest.mark.asyncio
async def test_unsupported_extension_is_rejected_at_mint(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A non audio/image extension 400s at MINT, not after the client streamed
    500MB — the extension picks the media branch, so it is knowable up front."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)

    data = await _call(client, key, "attach_file", {
        "filename": "notes.pdf", "attach_to": doc_id,
    })
    assert _is_error(data), data
    assert _result_text(data)["status_code"] == 400


@pytest.mark.asyncio
async def test_magic_mismatch_is_rejected_at_redeem(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The declared type is bound in the TOKEN (not taken from the multipart part),
    and the bytes must match it — same validate_magic gate the widget passes."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)

    payload = _result_text(await _call(client, key, "attach_file", {
        "filename": "liar.wav", "attach_to": doc_id,
    }))
    resp = await client.post(
        payload["url"], files={"file": ("liar.wav", b"this is plain text", "audio/wav")},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_token_type_cannot_be_swapped_with_a_download_token(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """INVARIANT(security): an upload token and a download token are both signed
    with SECRET_KEY, so they MUST carry a distinguishing claim — otherwise a
    read-only key's download token would redeem as a write grant."""
    from fastapi import HTTPException
    from mcp_gateway.download import mint_download_token
    from mcp_gateway.upload import verify_upload_token

    dl_token, _exp = await mint_download_token("some-ref-id")
    with pytest.raises(HTTPException) as exc:
        verify_upload_token(dl_token)
    assert exc.value.status_code == 403
