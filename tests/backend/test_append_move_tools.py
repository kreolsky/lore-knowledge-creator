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


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"am-key-{secrets.token_hex(4)}"
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


async def _sibling_order(client, cookies, pid, parent_id=None):
    resp = await client.get(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    return [
        d["document_id"] for d in docs
        if not d.get("is_index") and (d.get("parent_id") == parent_id)
    ]


# ════════════════════════════════════════════════════════════════════════════
# append — pure unit tests for the edit synthesis (the off-by-one risk)
# ════════════════════════════════════════════════════════════════════════════


def _apply_edit(content, edit):
    """Apply one synthesized edit to content (first occurrence) — mirrors what the
    edit core does, so the unit tests assert the INTENT of the synthesized edit."""
    return content.replace(edit["old_string"], edit["new_string"], 1)


@pytest.mark.asyncio
async def test_append_end_of_doc_builds_tail_anchor_edit():
    from routes.tool_api.edits import _build_append_edit

    content = "alpha beta gamma"
    edit = await _build_append_edit(content, "delta")
    assert edit is not None
    result = _apply_edit(content, edit)
    assert result.rstrip().endswith("delta")
    # The original text is preserved verbatim before the appended block.
    assert "alpha beta gamma" in result


@pytest.mark.asyncio
async def test_append_empty_doc_returns_none_content_set_path():
    from routes.tool_api.edits import _build_append_edit

    # An empty document has no anchor for a str_replace → None signals the
    # content-set path (route_document_content), NOT the edit executor.
    assert await _build_append_edit("", "first content") is None
    assert await _build_append_edit("   \n  \n", "first content") is None


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


@pytest.mark.asyncio
async def test_append_nested_section_stops_at_higher_level_heading():
    from routes.tool_api.edits import _build_append_edit

    content = (
        "# Top\n"
        "top body\n"
        "## Sub\n"
        "sub body\n"
        "### Deep\n"
        "deep body\n"
        "# Other\n"  # h1 terminates the "Deep" (h3) section too
        "other body\n"
    )
    edit = await _build_append_edit(content, "more", section="Deep")
    result = _apply_edit(content, edit)
    assert "more" in result
    assert result.index("deep body") < result.index("more") < result.index("# Other")


def test_append_section_not_found_returns_none_or_errors():
    """A section name that does not match any heading cannot be located."""
    from textmatch import _section_end_offset

    content = "# Top\nbody\n"
    # No matching heading → the section resolver signals not-found (raises).
    with pytest.raises(LookupError):
        _section_end_offset(content, "Nonexistent")


# ════════════════════════════════════════════════════════════════════════════
# append — Tool-API HTTP (auto + confirm) + MCP
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_append_auto_applies_via_tool_api(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "AppendMe", "one two three")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await client.post("/api/tool/append_to_document", json={
        "document_id": doc_id, "content": "four", "apply": "auto",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"

    content = await _read_content(client, token, doc_id)
    assert "one two three" in content
    assert content.rstrip().endswith("four")


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
async def test_move_reparents_under_new_parent(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent = await _make_doc(client, token, pid, "Parent")
    mover = await _make_doc(client, token, pid, "Mover")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await _move(client, token, agent_tok, mover, parent_id=parent)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    assert mover in await _sibling_order(client, cookies, pid, parent_id=parent)


@pytest.mark.asyncio
async def test_move_to_root_via_null_parent(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent = await _make_doc(client, token, pid, "Parent2")
    child = await _make_doc(client, token, pid, "Child2", parent_id=parent)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # parent_id null = project root (Variant A).
    resp = await client.post("/api/tool/move_document", json={
        "document_id": child, "apply": "auto",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    assert child in await _sibling_order(client, cookies, pid, parent_id=None)


@pytest.mark.asyncio
async def test_move_rejects_reference_parent(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "Host")
    # Create a reference under host.
    ref_resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "Ref", "content": "r",
        "is_reference": True, "media_type": "markdown", "parent_id": host,
    }, cookies={"lore_session": token})
    ref_id = ref_resp.json()["document_id"]
    mover = await _make_doc(client, token, pid, "MoverRef")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await _move(client, token, agent_tok, mover, parent_id=ref_id)
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_move_rejects_cycle_into_descendant(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    a = await _make_doc(client, token, pid, "A")
    b = await _make_doc(client, token, pid, "B", parent_id=a)
    c = await _make_doc(client, token, pid, "C", parent_id=b)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # Moving A under its own descendant C must be rejected (cycle).
    resp = await _move(client, token, agent_tok, a, parent_id=c)
    assert resp.status_code in (400, 409), resp.text


@pytest.mark.asyncio
async def test_move_reorder_after_sibling(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent = await _make_doc(client, token, pid, "ReorderParent")
    c1 = await _make_doc(client, token, pid, "C1", parent_id=parent)
    c2 = await _make_doc(client, token, pid, "C2", parent_id=parent)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # Move c1 after c2 within the same parent (parent_id = current parent).
    resp = await _move(client, token, agent_tok, c1, parent_id=parent, after_id=c2)
    assert resp.status_code == 200, resp.text
    order = await _sibling_order(client, cookies, pid, parent_id=parent)
    assert order == [c2, c1]


@pytest.mark.asyncio
async def test_move_rejects_after_id_from_wrong_parent(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    parent_a = await _make_doc(client, token, pid, "PA")
    parent_b = await _make_doc(client, token, pid, "PB")
    child_a = await _make_doc(client, token, pid, "ChildA", parent_id=parent_a)
    child_b = await _make_doc(client, token, pid, "ChildB", parent_id=parent_b)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    # after_id points at a doc under a DIFFERENT parent → rejected.
    resp = await _move(client, token, agent_tok, child_a, parent_id=parent_a, after_id=child_b)
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_move_cross_project_uniform_404(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    resp = await _move(client, admin_user[1], agent_tok, "doc-not-in-project", parent_id=None)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_move_mcp_readwrite_applies(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent = await _make_doc(client, token, pid, "McpParent")
    mover = await _make_doc(client, token, pid, "McpMover")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "move_document", {
        "document_id": mover, "parent_id": parent,
    })
    assert not _is_error(data), data
    assert _result_text(data)["status"] == "applied"
    assert mover in await _sibling_order(client, cookies, pid, parent_id=parent)


@pytest.mark.asyncio
async def test_move_mcp_readonly_key_rejected(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)
    data = await _mcp_call(client, agent_tok, "move_document", {"document_id": doc_id})
    assert _is_error(data)
    assert _result_text(data)["status_code"] == 403


