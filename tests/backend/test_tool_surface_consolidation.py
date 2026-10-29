"""Tool-surface consolidation tests (plan `.kilo/plans/tool-surface-consolidation.md`).

Per-step contract tests for the consolidation: derived instruction lists (Step 1),
list_references fold (2a), upload merge (2b), read_table fold (2c), run_extractor gate
(2d), and download_reference_file (Step 4). Each test binds the NEW contract derived
from the real served surface, not a hand-mirrored literal (testing.md).
"""

import hashlib
import json
import secrets

import pytest

# ─── Step 2d — run_extractor moved off the default MCP surface ────────────────


def test_run_extractor_not_advertised_by_default():
    """Step 2d: run_extractor is gated off the default MCP surface (~50s dev tool).
    It reappears when the gate flag is set (the CIR benchmark path is unaffected)."""
    import importlib

    import config as config_mod

    orig = getattr(config_mod, "MCP_RUN_EXTRACTOR", False)
    try:
        config_mod.MCP_RUN_EXTRACTOR = False
        import mcp_gateway.schemas as schemas_mod

        importlib.reload(schemas_mod)
        advertised = {t.name for t in schemas_mod.build_tool_list()}
        assert "preview_extractor" not in advertised, (
            "run_extractor must be off the default MCP surface"
        )

        config_mod.MCP_RUN_EXTRACTOR = True
        importlib.reload(schemas_mod)
        advertised = {t.name for t in schemas_mod.build_tool_list()}
        assert "preview_extractor" in advertised, (
            "run_extractor must reappear when the gate flag is set"
        )
    finally:
        config_mod.MCP_RUN_EXTRACTOR = orig
        importlib.reload(schemas_mod)


# ─── Step 4 — download_reference_file ────────────────────────────────────────


def test_resolve_reference_file_exists_as_shared_helper():
    """Step 4: resolve_reference_file is extracted as the shared helper so the
    security chain (fetch → is_reference → project_id → file_path → containment →
    exists) has ONE home for sandbox_fetch_reference, the serve route, and the new
    download tool. Its body tests live in the live MCP suite below."""
    from routes.files import resolve_reference_file  # noqa: F401

    assert callable(resolve_reference_file)


# ─── Step 4 — download_reference_file (live MCP e2e) ──────────────────────────
# Helpers mirror test_mcp_gateway_readonly. The plan's acceptance: tool call →
# signed URL → fetched bytes byte-identical to the stored file; an expired-token
# fetch returns 403; a ref_id from another project returns the uniform 404.


async def _dl_make_agent_key(test_db, user_id, project_id, *, auto_apply=True):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"dl-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "download", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _dl_hdr(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _dl_rpc(method, params=None, *, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _dl_result_text(resp_json):
    return json.loads(resp_json["result"]["content"][0]["text"])


def _dl_is_error(resp_json):
    return resp_json.get("result", {}).get("isError", False)


async def _dl_call(client, token, name, arguments=None):
    resp = await client.post(
        "/mcp", json=_dl_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_dl_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed_ref_with_file(test_db, project_id, ref_id, data, mime="image/png",
                               media_type="image", safe_name="chart.png"):
    """Persist a reference row with stored bytes on disk (mirrors save_upload)."""
    from config import STORAGE_PATH
    from db import create_record

    rel = f"{project_id}/{ref_id}/{safe_name}"
    disk = STORAGE_PATH / project_id / ref_id / safe_name
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(data)
    # Reference-host invariant: a reference needs a real host (parent_id). Create a host
    # doc once per project (idempotent) and attach the ref — mirrors production shape.
    host_id = f"{project_id}-host"
    try:
        await create_record("documents", host_id, {
            "project_id": project_id, "parent_id": None, "title": "Host",
            "content": "", "path": f"{host_id}.md", "is_index": False,
        })
    except RuntimeError:
        pass
    await create_record("documents", ref_id, {
        "project_id": project_id, "parent_id": host_id, "title": safe_name,
        "content": "", "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": media_type, "file_path": rel,
        "file_meta": {"mime_type": mime, "file_size": len(data),
                      "original_name": safe_name},
    })
    return disk


@pytest.mark.asyncio
async def test_download_reference_file_e2e_bytes_identical(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Step 4 acceptance: tool call → signed URL → fetched bytes are byte-identical
    to the stored file."""
    import base64

    pid, _, admin_uid = project_with_doc
    ref_id = f"dl-ref-{secrets.token_hex(4)}"
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    disk = await _seed_ref_with_file(test_db, pid, ref_id, png)
    token = await _dl_make_agent_key(test_db, admin_uid, pid)

    data = await _dl_call(client, token, "get_file", {"document_id": ref_id})
    assert not _dl_is_error(data), data
    payload = _dl_result_text(data)
    assert "/api/mcp/download/" in payload["url"]
    assert payload["filename"]

    # Fetch the bytes over the unauthenticated route.
    resp = await client.get(payload["url"])
    assert resp.status_code == 200, resp.text
    assert resp.content == disk.read_bytes() == png


@pytest.mark.asyncio
async def test_download_reference_file_expired_token_is_403(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """Step 4 acceptance: an expired token fetch returns 403 (the TTL + exp
    requirement are what stop the URL from becoming a general file oracle)."""
    pid, _, admin_uid = project_with_doc
    ref_id = f"dl-ref-{secrets.token_hex(4)}"
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    await _seed_ref_with_file(test_db, pid, ref_id, png)
    token = await _dl_make_agent_key(test_db, admin_uid, pid)

    # Mint with a zero TTL so the token is already expired.
    import config
    monkeypatch.setattr(config, "MCP_DOWNLOAD_TOKEN_TTL_S", 0)
    data = await _dl_call(client, token, "get_file", {"document_id": ref_id})
    url = _dl_result_text(data)["url"]
    # A 0-TTL token is in the past by the time it is decoded → 403.
    resp = await client.get(url)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_download_reference_file_cross_project_is_uniform_404(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Step 4 acceptance: a ref_id from ANOTHER project yields a uniform 404 (no
    existence oracle) at tool-call time — the cross-project IDOR guard."""
    pid, _, admin_uid = project_with_doc
    other_pid = f"dl-other-{secrets.token_hex(4)}"
    await test_db.query(
        "CREATE type::record('projects', $id) SET name='Other', status='active', "
        "project_context='', owner_id=$uid",
        {"id": other_pid, "uid": admin_uid},
    )
    foreign_ref = f"dl-foreign-{secrets.token_hex(4)}"
    await _seed_ref_with_file(test_db, other_pid, foreign_ref, b"\x00" * 16)
    token = await _dl_make_agent_key(test_db, admin_uid, pid)

    data = await _dl_call(client, token, "get_file",
                          {"document_id": foreign_ref})
    assert _dl_is_error(data)
    payload = _dl_result_text(data)
    assert payload["status_code"] == 404


@pytest.mark.asyncio
async def test_download_reference_file_subtree_scoped_key_rejects_out_of_scope(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A subtree-scoped agent key (document_id = the subtree root) must NOT be able
    to download a reference binary attached to a doc OUTSIDE its subtree. The text
    read path (read_document) and run_extractor both enforce scope; the download
    path must too (the binary is strictly more sensitive). Regression guard for the
    scope_root check added to _dispatch_get_file."""
    from db import create_record

    pid, scope_root, admin_uid = project_with_doc
    # A sibling doc NOT under the scoped subtree (scope_root).
    sibling = f"dl-sibling-{secrets.token_hex(4)}"
    await test_db.query(
        "CREATE type::record('documents', $id) SET project_id=$pid, parent_id=NONE, "
        "title='Sibling', content='', path='', is_reference=false, is_index=false, "
        "deleted_at=NONE",
        {"id": sibling, "pid": pid},
    )
    foreign_ref = f"dl-oos-{secrets.token_hex(4)}"
    await _seed_ref_with_file(test_db, pid, foreign_ref, b"\x00" * 16,
                              safe_name="oos.png")
    # Attach the reference to the out-of-scope sibling (parent_id = sibling, not scope_root).
    await test_db.query(
        "UPDATE type::record('documents', $id) SET parent_id=$sib",
        {"id": foreign_ref, "sib": sibling},
    )
    # Scoped key: document_id = scope_root ⇒ subtree = scope_root + descendants.
    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await create_record("api_keys", f"dl-scoped-{secrets.token_hex(4)}", {
        "user_id": admin_uid, "project_id": pid, "document_id": scope_root,
        "token_hash": token_hash, "label": "scoped-dl", "capabilities": ["agent"],
        "auto_apply": True,
    })

    data = await _dl_call(client, token, "get_file",
                          {"document_id": foreign_ref})
    assert _dl_is_error(data), data
    assert _dl_result_text(data)["status_code"] == 403
