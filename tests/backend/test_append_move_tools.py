"""Tests for the append_to_document + move_document agent/MCP tools (plan
mcp-editor-tools-part1).

Both tools are mutating and surface on BOTH the internal Tool-API/Pi path and the
MCP gateway. append reuses the live-CRDT edit core (synthesizes a unique tail-
anchor edit, or a content-set for an empty doc); move is a structural executor
over the existing reparent + fractional sort_key logic.

Pure unit tests cover the append section/anchor resolution (the off-by-one risk);
HTTP + MCP integration tests cover the dual-surface invariant + access gates.
"""

import hashlib
import secrets

import pytest

# ─── Shared helpers (mirror test_tool_api / test_mcp_gateway_mutating) ─────────


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False, scope_root=""):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"am-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": scope_root,
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


# ════════════════════════════════════════════════════════════════════════════
# append — pure unit tests for the edit synthesis (the off-by-one risk)
# ════════════════════════════════════════════════════════════════════════════


def _apply_edit(content, edit):
    """Apply one synthesized edit to content (first occurrence) — mirrors what the
    edit core does, so the unit tests assert the INTENT of the synthesized edit."""
    return content.replace(edit["old_string"], edit["new_string"], 1)


@pytest.mark.asyncio
async def test_append_to_empty_doc_unwraps_placeholder_bracket_link_destinations(monkeypatch):
    """A `(<id>)` link the model copied from a prompt placeholder is stored
    unwrapped — append into an EMPTY document is the one append branch that
    bypasses the edit executor and normalizes on its own."""
    from agent import collab_writes as cw
    from routes.tool_api.edits import _apply_append_to_empty_doc

    captured: dict = {}

    async def fake_route(*, doc_id, new_content, project_id):
        captured["content"] = new_content
        return True

    async def fake_presence(_doc_id, _uid):
        return None

    monkeypatch.setattr("agent.doc_state.route_document_content", fake_route)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    await _apply_append_to_empty_doc(
        doc_id="d1", append_text="[text](<abc-def>) and ![a|800x600](<ref:xyz_1>)",
        project_id="p1", user={"user_id": "u1"},
    )
    assert captured["content"] == "[text](abc-def) and ![a|800x600](ref:xyz_1)"


@pytest.mark.asyncio
async def test_append_section_inserts_before_next_same_or_higher_heading():
    from routes.tool_api.edits import _build_append_edit

    content = (
        "# Top\n"
        "intro\n"
        "## Sub1\n"
        "sub1 body\n"
        "## Sub2\n"
        "sub2 body\n"
    )
    edit = await _build_append_edit(content, "appended", section="Sub1")
    assert edit is not None
    result = _apply_edit(content, edit)
    # The appended text lands at the END of Sub1's section, BEFORE "## Sub2".
    sub1_end = result.index("## Sub2")
    assert "appended" in result[:sub1_end]
    assert result.index("appended") > result.index("sub1 body")
    # Sub2's heading and body remain intact and AFTER the appended text.
    assert "## Sub2\nsub2 body" in result[result.index("appended"):]


# ════════════════════════════════════════════════════════════════════════════
# append — Tool-API HTTP (auto + confirm) + MCP
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_append_empty_doc_auto_applies_content_set(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "Empty", "")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await client.post("/api/tool/append_to_document", json={
        "document_id": doc_id, "content": "seed content", "apply": "auto",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"

    content = await _read_content(client, token, doc_id)
    assert content.strip() == "seed content"


@pytest.mark.asyncio
async def test_append_mcp_readwrite_applies(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "McpAppend", "hello")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "append_to_document", {
        "document_id": doc_id, "content": "world",
    })
    assert not _is_error(data), data
    assert _result_text(data)["status"] == "applied"
    content = await _read_content(client, token, doc_id)
    assert "hello" in content and content.rstrip().endswith("world")


@pytest.mark.asyncio
async def test_append_mcp_readonly_key_rejected(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)
    data = await _mcp_call(client, agent_tok, "append_to_document", {
        "document_id": doc_id, "content": "x",
    })
    assert _is_error(data)
    assert _result_text(data)["status_code"] == 403


# ════════════════════════════════════════════════════════════════════════════
# move — Tool-API HTTP (auto) + MCP + guards
# ════════════════════════════════════════════════════════════════════════════


async def _move(client, token, agent_tok, doc_id, parent_id=None, after_id=None, apply="auto"):
    body = {"document_id": doc_id, "apply": apply}
    if parent_id is not None:
        body["parent_id"] = parent_id
    if after_id is not None:
        body["after_id"] = after_id
    return await client.post("/api/tool/move_document", json=body, headers=_hdr(agent_tok))


@pytest.mark.asyncio
async def test_move_cross_project_uniform_404(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    resp = await _move(client, admin_user[1], agent_tok, "doc-not-in-project", parent_id=None)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_move_mcp_readonly_key_rejected(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)
    data = await _mcp_call(client, agent_tok, "move_document", {"document_id": doc_id})
    assert _is_error(data)
    assert _result_text(data)["status_code"] == 403


@pytest.mark.asyncio
async def test_scoped_move_onto_project_index_outside_wall_refused(
    client, test_db, admin_user, project_with_doc,
):
    """Scope-wall fence for index→root normalisation: a scoped key moving a doc
    onto the project index (OUTSIDE its subtree) is refused 403 as today. The
    normalisation to root must run only AFTER the destination checks —
    resolving to None before them once let a scoped key park its target
    outside the wall (INVARIANT at documents/move.py move_document_command)."""
    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    inner = await _make_doc(client, token, pid, "WallInner")
    child = await _make_doc(client, token, pid, "WallChild", parent_id=inner)
    agent_tok = await _make_agent_key(
        test_db, admin_uid, pid, auto_apply=True, scope_root=inner,
    )

    resp = await _move(client, token, agent_tok, child, parent_id=idx_id)
    assert resp.status_code == 403, resp.text
    assert "subtree scope" in resp.json().get("detail", "")

    rows = await test_db.query(
        "SELECT parent_id FROM type::record('documents', $id)", {"id": child},
    )
    assert rows[0].get("parent_id") == inner, "refused move must write nothing"
