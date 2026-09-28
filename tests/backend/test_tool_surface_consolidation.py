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

# ─── Step 1 — derive the tool-name lists in the agent instructions ────────────


async def test_init_instructions_list_every_served_tool_name():
    """Step 1: the rendered `init` instructions text must NAME every tool the
    gateway advertises via build_tool_list(), split into the read vs mutating lists.

    This is the test that would have failed on the live drift bug
    (bootstrap.py hand-copied a 3-tool mutating list while the gateway served 10).
    Asserts over build_tool_list() (the derived source), so it covers the next tool
    added, not a literal.
    """
    from agent.tools import MUTATING_TOOLS
    from mcp_gateway.bootstrap import _build_instructions
    from mcp_gateway.schemas import build_tool_list

    text = await _build_instructions(scope_root="", root_title="")
    advertised = {t.name for t in build_tool_list()}
    # `init` is prose-only (call-once) and IS named in the instructions; every other
    # advertised tool must appear by name somewhere in the rendered instructions.
    missing = [name for name in advertised if name not in text]
    assert not missing, (
        f"tools served via tools/list but absent from the init instructions: {missing}"
    )
    # The mutating list specifically: every MUTATING tool THIS GATEWAY SERVES (the
    # AGENT_TOOLS intersection, not the Pi-only sandbox superset) must be in the text.
    from agent.tools import AGENT_TOOLS
    served_mutating = MUTATING_TOOLS & {s["function"]["name"] for s in AGENT_TOOLS}
    missing_mut = sorted(m for m in served_mutating if m not in text)
    assert not missing_mut, (
        f"mutating tools missing from the init instructions: {missing_mut}"
    )


# ─── Step 2a — list_references folded into get_project_structure ──────────────


def test_list_references_not_advertised_after_fold():
    """Step 2a: list_references is removed from the MCP surface (folded into
    get_project_structure)."""
    from mcp_gateway.schemas import build_tool_list

    advertised = {t.name for t in build_tool_list()}
    assert "list_references" not in advertised, (
        "list_references should be folded into get_project_structure"
    )


def test_get_project_structure_advertises_no_new_required_fields():
    """Step 2a: get_project_structure's advertised inputSchema is unchanged (the
    fold is in the OUTPUT shape, not the input)."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    schema = tools["get_project_structure"].input_schema
    # No new required input; start_id/depth stay optional.
    assert not schema.get("required")


def test_get_project_structure_output_schema_has_reference_fields():
    """Step 2a: the advertised outputSchema for get_project_structure now carries
    media_type + source_url on the documents item (reference rows)."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    item = tools["get_project_structure"].output_schema["properties"]["documents"]["items"]
    props = item["properties"]
    assert "media_type" in props
    assert "source_url" in props


# ─── Step 2b — upload_document + upload_reference → import_file(is_reference) ─


async def test_import_file_is_agent_only_attach_file_is_the_mcp_byte_channel():
    """D14 (plan mcp-tool-surface-redesign): import_file left the MCP surface (agent-only
    now — the driver reads sandbox_path / resolves attachment_index, so bytes never
    pass through the model). The MCP byte channel is attach_file (signed-URL
    transport). The old pre-merge names stay gone."""
    from agent.tools import agent_toolset
    from agent_tools.specs.media import IMPORT_FILE_TOOL
    from mcp_gateway.schemas import build_tool_list

    advertised = {t.name for t in build_tool_list()}
    assert "import_file" not in advertised, "import_file must be off the MCP surface (Pi-only)"
    assert "attach_file" in advertised
    assert "upload_document" not in advertised
    assert "upload_reference" not in advertised
    # Pi keeps it.
    assert "import_file" in {t["function"]["name"] for t in await agent_toolset()}
    assert IMPORT_FILE_TOOL["function"]["name"] == "import_file"


def test_import_file_carries_node_type_and_no_content():
    """Step 2b (D2): the Pi-only import_file has a `node_type` enum and NO plain
    `content` field (text authored by the model goes to create_document). D5 (plan
    internal-agent-surface-reconciliation): `content_base64` is also removed — the
    agent cannot author bytes; the byte channels are sandbox_path (produced) and
    attachment_index (persisted). Plan agent-document-placement-and-node-type:
    is_reference/document_id are gone — node_type names the kind, parent_id both
    placements (tree parent / host)."""
    from agent_tools.specs.media import IMPORT_FILE_TOOL

    props = IMPORT_FILE_TOOL["function"]["parameters"]["properties"]
    assert props["node_type"]["enum"] == ["document", "reference"]
    assert "is_reference" not in props
    assert "document_id" not in props
    assert "parent_id" in props
    # D2: plain `content` is removed from the upload surface.
    assert "content" not in props
    # D5: content_base64 is removed from the Pi surface (the model cannot see bytes).
    assert "content_base64" not in props
    assert "sandbox_path" in props
    assert "attachment_index" in props


def test_create_document_carries_normalize_flag():
    """Step 2b (D3): create_document gains a `normalize` boolean (the .md
    normalize capability moved out of the upload text path after D2)."""
    from agent.tools import AGENT_TOOLS

    tool = next(t for t in AGENT_TOOLS if t["function"]["name"] == "create_document")
    props = tool["function"]["parameters"]["properties"]
    assert "normalize" in props
    assert props["normalize"]["type"] == "boolean"
    assert props["normalize"]["default"] is False


# ─── Step 2c — read_table folded into read_document ───────────────────────────


def test_read_table_not_advertised_after_fold():
    """Step 2c: read_table is removed from the MCP surface (folded into read_document)."""
    from mcp_gateway.schemas import build_tool_list

    advertised = {t.name for t in build_tool_list()}
    assert "read_table" not in advertised


def test_read_document_carries_table_params():
    """Step 2c: read_document gains `tables` (index|inline|none, default index) and
    an optional `table_id` (the existing read_table semantics moved)."""
    from agent.tools import AGENT_TOOLS

    tool = next(t for t in AGENT_TOOLS if t["function"]["name"] == "read_document")
    props = tool["function"]["parameters"]["properties"]
    assert "tables" in props
    assert props["tables"]["default"] == "index"
    assert set(props["tables"]["enum"]) == {"index", "inline", "none"}
    assert "table_id" in props


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


def test_download_reference_file_is_advertised():
    """Step 4: download_reference_file is on the MCP surface (symmetry with import_file)."""
    from mcp_gateway.schemas import build_tool_list

    advertised = {t.name for t in build_tool_list()}
    assert "get_file" in advertised


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
