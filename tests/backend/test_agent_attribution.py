"""S1 (plan 1786740208300) — agent attribution on agent-surface writes.

# INVARIANT (attribution axes): `created_by` stays the key's user_id (the RIGHTS
# axis — RBAC, ownership and agent-rights ≤ owner-rights all read it) while
# `created_by_name` carries the LABEL of the api_keys row that made the write
# (the DISPLAY axis). The two must not be conflated: an MCP write is made on the
# owning user's authority and that stays literally true — only the byline
# changes. Internal keys (the Pi driver's own, label "agent") and blank labels
# carry NO key label, so those writes keep the owner's name byte-identically.
# The label is denormalized onto the row at WRITE time: a later key deletion
# must not change what already-authored rows show.
"""

import hashlib
import json
import secrets

import pytest

pytestmark = pytest.mark.asyncio


# ─── helpers (mirror test_mcp_upload_url) ─────────────────────────────────────


async def _make_agent_key(
    test_db, user_id, project_id, *,
    label="upload", internal=False, auto_apply=True, scope_root="",
):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"attr-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": scope_root,
        "token_hash": token_hash, "label": label, "capabilities": ["agent"],
        "internal": internal,
        "auto_apply": auto_apply,
    })
    return token, key_id


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


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56


# ─── ctx resolution (api_key_auth) ────────────────────────────────────────────


async def test_resolve_api_key_carries_key_label_for_user_minted_key(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A user-minted agent key's label rides the ctx — the DISPLAY axis source
    every agent-surface write site reads."""
    from api_key_auth import resolve_api_key

    pid, _idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="Research Agent", internal=False,
    )
    ctx = await resolve_api_key(token, require="agent")
    assert ctx["key_label"] == "Research Agent"
    # The RIGHTS axis is untouched: created_by sites keep reading user_id.
    assert ctx["user_id"] == uid


async def test_resolve_api_key_strips_label_whitespace(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    from api_key_auth import resolve_api_key

    pid, _idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="  Archiver  ", internal=False,
    )
    ctx = await resolve_api_key(token, require="agent")
    assert ctx["key_label"] == "Archiver"


async def test_internal_key_carries_no_key_label(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """PIN: the internal Pi-driver key (internal=true, label 'agent') must NOT
    surface as an author — its label is plumbing, not identity. Write sites fall
    back to the owning user's name, byte-identical to pre-S1 behavior."""
    from api_key_auth import resolve_api_key

    pid, _idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="agent", internal=True,
    )
    ctx = await resolve_api_key(token, require="agent")
    assert ctx["key_label"] is None


async def test_blank_label_carries_no_key_label(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """PIN: a whitespace-only label is no identity — None, not an empty byline."""
    from api_key_auth import resolve_api_key

    pid, _idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="   ", internal=False,
    )
    ctx = await resolve_api_key(token, require="agent")
    assert ctx["key_label"] is None


# ─── MCP write attribution (e2e) ──────────────────────────────────────────────


async def test_mcp_create_reference_shows_key_label_as_author(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """THE S1 acceptance: an MCP write through a minted read-write key renders
    the agent's label as the author, while created_by stays the owner."""
    from db import fetch_one

    pid, idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="Research Agent",
    )
    data = await _call(client, token, "create_document", {
        "title": "Deposited by agent", "content": "body", "parent_id": idx,
        "node_type": "reference",
    })
    assert not _is_error(data), data
    rid = _result_text(data)["doc_id"]

    row = await fetch_one("documents", rid)
    assert row["is_reference"] is True
    # RIGHTS axis unchanged.
    assert row["created_by"] == uid
    # DISPLAY axis = the making key's label.
    assert row["created_by_name"] == "Research Agent"


async def test_mcp_write_with_internal_key_attributed_to_owner(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Internal-key writes keep the owner's name — user-path attribution
    byte-identical (the plan's boundary case #1)."""
    from db import fetch_one

    pid, idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="agent", internal=True,
    )
    data = await _call(client, token, "create_document", {
        "title": "Pi deposit", "content": "body", "parent_id": idx,
        "node_type": "reference",
    })
    assert not _is_error(data), data
    rid = _result_text(data)["doc_id"]

    row = await fetch_one("documents", rid)
    assert row["created_by"] == uid
    assert row["created_by_name"] == "testadmin"


async def test_deleted_key_label_persists_on_authored_rows(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Denormalization is the point: soft-delete the key after the write and the
    authored row KEEPS its label (the label is never resolved at read time), even
    though the key itself no longer authenticates."""
    from api_key_auth import resolve_api_key
    from fastapi import HTTPException

    from db import fetch_one

    pid, idx, uid = project_with_doc
    token, key_id = await _make_agent_key(
        test_db, uid, pid, label="Retired Agent",
    )
    data = await _call(client, token, "create_document", {
        "title": "Legacy deposit", "content": "body", "parent_id": idx,
        "node_type": "reference",
    })
    assert not _is_error(data), data
    rid = _result_text(data)["doc_id"]

    await test_db.query(
        "UPDATE type::record('api_keys', $id) SET deleted_at = time::now()",
        {"id": key_id},
    )
    row = await fetch_one("documents", rid)
    assert row["created_by_name"] == "Retired Agent"
    with pytest.raises(HTTPException) as exc:
        await resolve_api_key(token, require="agent")
    assert exc.value.status_code == 401


# ─── attach_file (signed-URL upload redeem) ───────────────────────────────────


async def test_attach_file_redeem_attributes_to_key_label(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The redeem route has no key — the label must ride the token's claims
    (authorization is bound at mint, same principle as the scope checks)."""
    from db import fetch_one

    pid, idx, uid = project_with_doc
    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="Field Recorder",
    )
    payload = _result_text(await _call(client, token, "attach_file", {
        "filename": "note.md", "attach_to": idx, "title": "Note",
    }))
    resp = await client.post(
        payload["url"], files={"file": ("note.md", b"# hello", "text/markdown")},
    )
    assert resp.status_code == 200, resp.text

    row = await fetch_one("documents", resp.json()["document_id"])
    assert row["created_by"] == uid
    assert row["created_by_name"] == "Field Recorder"


async def test_upload_token_carries_agent_label_claim(test_db):
    """Unit: the mint freezes the making key's label into the claims."""
    from mcp_gateway.upload import mint_upload_token, verify_upload_token

    token, _exp, _jti = await mint_upload_token(
        project_id="p", document_id="d", filename="a.md", title="a",
        mime="text/markdown", user_id="u", agent_label="Research Agent",
    )
    claims = verify_upload_token(token)
    assert claims["agent_label"] == "Research Agent"


async def test_author_kwargs_without_label_claim_falls_back_to_user(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """PIN (back-compat): a token minted without a label claim (pre-S1 mints in
    flight during a deploy) attributes to the minting user's name."""
    from mcp_gateway.upload import mint_upload_token, verify_upload_token
    from routes.files_mcp_upload import _author_kwargs

    pid, _idx, uid = project_with_doc
    token, _exp, _jti = await mint_upload_token(
        project_id=pid, document_id="d", filename="a.md", title="a",
        mime="text/markdown", user_id=uid,
    )
    claims = verify_upload_token(token)
    assert not claims.get("agent_label")
    author = await _author_kwargs(claims)
    assert author == {"created_by": uid, "created_by_name": "testadmin"}


# ─── import_file thread-through (Tool-API HTTP surface) ──────────────────────


async def test_import_document_threads_author_name(monkeypatch):
    """`_import_document` threads the resolved author name to BOTH attribution
    sites: extracted images and the created reference."""
    from routes.tool_api.imports import _import_document

    captured: dict = {}

    async def _fake_extract(content, project_id, parent_id, *,
                            created_by=None, created_by_name=None):
        captured["extract"] = (created_by, created_by_name)
        return content, []

    async def _fake_create_reference_via_collab(*, document_id, title, content,
                                                media_type, source_url,
                                                project_id, user,
                                                scope_root=None,
                                                author_name=None):
        captured["ref"] = author_name
        return {"doc_id": "ref-1"}

    monkeypatch.setattr(
        "routes.tool_api.imports._extract_images", _fake_extract,
    )
    monkeypatch.setattr(
        "agent.collab_writes.create_reference_via_collab",
        _fake_create_reference_via_collab,
    )

    result = await _import_document(
        is_reference=True, binary_allowed=False, filename="notes.md",
        content="hello", content_base64=None, title="Notes", parent_id=None,
        document_id="host-1",
        project_id="p1", user={"user_id": "u1", "name": "testadmin"},
        scope_root=None, author_name="Research Agent",
    )
    assert result["status"] == "applied"
    assert captured["extract"] == ("u1", "Research Agent")
    assert captured["ref"] == "Research Agent"


async def test_import_document_defaults_to_user_name(monkeypatch):
    """PIN: callers that pass no author_name (non-agent entry points reuse the
    core) keep the user's name — the pre-S1 default."""
    from routes.tool_api.imports import _import_document

    captured: dict = {}

    async def _fake_extract(content, project_id, parent_id, *,
                            created_by=None, created_by_name=None):
        captured["extract"] = (created_by, created_by_name)
        return content, []

    async def _fake_create_reference_via_collab(*, document_id, title, content,
                                                media_type, source_url,
                                                project_id, user,
                                                scope_root=None,
                                                author_name=None):
        captured["ref"] = author_name
        return {"doc_id": "ref-1"}

    monkeypatch.setattr(
        "routes.tool_api.imports._extract_images", _fake_extract,
    )
    monkeypatch.setattr(
        "agent.collab_writes.create_reference_via_collab",
        _fake_create_reference_via_collab,
    )

    await _import_document(
        is_reference=True, binary_allowed=False, filename="notes.md",
        content="hello", content_base64=None, title="Notes", parent_id=None,
        document_id="host-1",
        project_id="p1", user={"user_id": "u1", "name": "testadmin"},
        scope_root=None,
    )
    assert captured["extract"] == ("u1", "testadmin")
    assert captured["ref"] is None


async def test_create_binary_reference_threads_author_name(monkeypatch):
    """The binary save_upload sites (image/audio) attribute to the key label."""
    from routes.tool_api.imports import _create_binary_reference

    saved: dict = {}

    async def _fake_save_upload(data, mime, original_name, project_id,
                                document_id, *, title, media_type,
                                processing_status,
                                created_by=None, created_by_name=None):
        saved.update(created_by=created_by, created_by_name=created_by_name)
        return "ref-x", {"reference_id": "ref-x"}

    monkeypatch.setattr("routes.tool_api.imports.save_upload", _fake_save_upload)

    result = await _create_binary_reference(
        _PNG, "shot.png", "Shot", "host-1", "p1",
        {"user_id": "u1", "name": "testadmin"}, author_name="Research Agent",
    )
    assert result["status"] == "applied"
    assert saved["created_by"] == "u1"
    assert saved["created_by_name"] == "Research Agent"

    # PIN: no author_name → the user's name (pre-S1 default).
    await _create_binary_reference(
        _PNG, "shot.png", "Shot", "host-1", "p1",
        {"user_id": "u1", "name": "testadmin"},
    )
    assert saved["created_by_name"] == "testadmin"


# ─── generate_image (web launcher + worker rebuild + persist) ─────────────────


async def test_generate_image_enqueue_payload_carries_key_label(
    client, test_db, project_with_doc, monkeypatch,
):
    """The launcher freezes key_label into the arq payload — the worker has no
    key context to re-read."""
    from helpers import pin_comfy_config
    from routes.tool_api import image_gen

    pid, idx, uid = project_with_doc

    async def _async_return(value):
        return value

    import config

    monkeypatch.setattr(config, "COMFYUI_URL", "http://comfy-test")
    pin_comfy_config(monkeypatch)
    monkeypatch.setattr(
        image_gen.tool, "fetch_one",
        lambda *a, **k: _async_return({"project_id": pid, "deleted_at": None}),
    )
    enqueued: dict = {}

    async def _fake_enqueue(fn_name, payload, **kw):
        enqueued["fn_name"] = fn_name
        enqueued["payload"] = payload

    monkeypatch.setattr("jobs.pool.enqueue", _fake_enqueue)

    token, _key_id = await _make_agent_key(
        test_db, uid, pid, label="Art Bot", internal=False,
    )
    resp = await client.post(
        "/api/tool/generate_image",
        json={"prompt": "a cat", "document_id": idx},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert enqueued["fn_name"] == "generate_image_task"
    assert enqueued["payload"]["key_label"] == "Art Bot"


async def test_generate_image_task_rebuilt_ctx_carries_key_label(monkeypatch):
    """The worker's minimal ctx rebuild threads key_label through to
    run_generation."""
    import image_generation.run

    from jobs import tasks

    called: dict = {}

    async def _spy(ctx, run_id, target_doc_id, body, prompt_template, wf, size):
        called["ctx"] = ctx

    monkeypatch.setattr(image_generation.run, "run_generation", _spy)
    payload = {
        "run_id": "run-xyz", "project_id": "p1", "user_id": "u1",
        "user_name": "Alice", "key_label": "Art Bot",
        "session_id": "sess-9", "message_id": None, "target_doc_id": "doc-1",
        "prompt": "a tower", "orientation": "landscape", "count": 1,
        "prompt_template": "TPL", "workflow": {"6": {"inputs": {}}},
        "size": [1216, 832],
    }
    await tasks.generate_image_task({"redis": None}, payload)
    assert called["ctx"]["key_label"] == "Art Bot"


async def test_persist_and_announce_prefers_key_label(monkeypatch):
    """The persist site prefers the key label over the user's name — the image
    reference's byline is the agent that generated it."""
    import image_generation.run
    from image_generation import image_refine

    saved_names: list = []

    async def _fake_save_upload(data, mime, name, project_id, document_id, *,
                                title, media_type, processing_status,
                                created_by=None, created_by_name=None):
        saved_names.append((created_by, created_by_name))
        rid = f"ref-{len(saved_names)}"
        return rid, {"reference_id": rid}

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(image_generation.persist, "save_upload", _fake_save_upload)
    monkeypatch.setattr(image_generation.persist, "_push_gen_settled", _noop)

    ctx = {
        "project_id": "p1", "user_id": "u1", "key_label": "Art Bot",
        "user_name": "Alice", "user": {"id": "u1", "name": "Alice"},
    }
    await image_generation.persist._persist_and_announce(
        ctx, "run-1", "doc-1", "A cat", None,
        image_refine._RefineResult(prompt="x", ok=True, error=None),
        [(_PNG, "image/png")],
    )
    assert saved_names == [("u1", "Art Bot")]

    # PIN: no key label (internal key / web test path) → user name, unchanged.
    ctx_no_label = {
        "project_id": "p1", "user_id": "u1",
        "user_name": "Alice", "user": {"id": "u1", "name": "Alice"},
    }
    await image_generation.persist._persist_and_announce(
        ctx_no_label, "run-2", "doc-1", "A cat", None,
        image_refine._RefineResult(prompt="x", ok=True, error=None),
        [(_PNG, "image/png")],
    )
    assert saved_names[-1] == ("u1", "Alice")
