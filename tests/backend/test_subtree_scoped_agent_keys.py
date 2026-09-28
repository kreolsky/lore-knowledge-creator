"""Subtree-scoped agent keys — the MCP/Tool-API sandbox (plan
"subtree-scoped-agent-keys").

An agent key whose `document_id` (= scope_root) points at a subtree root
restricts every agent surface (Tool-API HTTP + MCP gateway) to that subtree
(root + descendants). Membership is re-walked per call via db.get_ancestor_ids;
enumeration uses db.get_descendant_ids. An internal (Pi) key with empty
scope_root keeps whole-project behavior.

Covers: membership, enforcement (read/write/search/structure/create),
out-of-scope transclusion, capability gating (a key works only on the surfaces
in its `capabilities` set; a combined key works on both), and the
proposal-apply re-check.
"""
import hashlib
import secrets

import pytest

# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_key(
    test_db, user_id: str, project_id: str, *, document_id: str = "",
    capabilities: list[str],
) -> str:
    """Insert an API key with the given capabilities, return the plaintext token.

    For the agent capability document_id is the subtree root ("" = whole
    project, internal-style); for the widget capability it is the bound doc.
    """
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"scope-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,
        "token_hash": token_hash,
        "label": "test key",
        "capabilities": capabilities,
    })
    return token


async def _make_agent_key(
    test_db, user_id: str, project_id: str, *, scope_root: str = "",
) -> str:
    """Agent-capability key. scope_root="" ⇒ whole project."""
    return await _make_key(
        test_db, user_id, project_id, document_id=scope_root,
        capabilities=["agent"],
    )


async def _make_widget_key(test_db, user_id: str, project_id: str, document_id: str) -> str:
    """Widget-capability key bound to a single document."""
    return await _make_key(
        test_db, user_id, project_id, document_id=document_id,
        capabilities=["widget"],
    )


async def _make_doc(
    client, token: str, project_id: str, title: str, content: str = "",
    *, parent_id: str | None = None,
) -> str:
    payload: dict = {"project_id": project_id, "title": title, "content": content}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=payload, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _build_tree(client, admin_token, project_id):
    """Build: subtree_root → child → grandchild, plus an outside sibling doc.

    Doc bodies are longer than the edit substrings the tests use, so edit_document
    stays pointwise (the full-rewrite guard rejects old_string == whole doc).

    Returns (subtree_root, child, grandchild, outside)."""
    root = await _make_doc(client, admin_token, project_id, "SubtreeRoot", "root body and more text")
    child = await _make_doc(
        client, admin_token, project_id, "Child", "child body and more text",
        parent_id=root,
    )
    grandchild = await _make_doc(
        client, admin_token, project_id, "Grandchild", "grandchild body and more text",
        parent_id=child,
    )
    outside = await _make_doc(client, admin_token, project_id, "Outside", "outside body and more text")
    return root, child, grandchild, outside


# ─── Membership helpers (unit) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_in_subtree_membership(test_db, client, admin_user, project_with_doc):
    """in_subtree: root/child/grandchild in; outside out; None = whole project."""
    from scope import in_subtree

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, outside = await _build_tree(client, admin_token, pid)

    assert await in_subtree(root, root) is True
    assert await in_subtree(root, child) is True
    assert await in_subtree(root, grandchild) is True
    assert await in_subtree(root, outside) is False
    # No scope_root ⇒ whole project ⇒ always True.
    assert await in_subtree("", outside) is True
    assert await in_subtree(None, outside) is True


@pytest.mark.asyncio
async def test_in_subtree_soft_deleted_ancestor_breaks_chain(
    test_db, client, admin_user, project_with_doc,
):
    """A soft-deleted ancestor breaks the walk: grandchild under a deleted child
    is no longer reachable from root (chain stops at the deleted node)."""
    from db import soft_delete
    from scope import in_subtree

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _outside = await _build_tree(client, admin_token, pid)

    assert await in_subtree(root, grandchild) is True
    await soft_delete("documents", child)
    assert await in_subtree(root, grandchild) is False


@pytest.mark.asyncio
async def test_subtree_doc_ids_enumeration(test_db, client, admin_user, project_with_doc):
    """subtree_doc_ids: [] when no scope (whole project); [root+descendants] when scoped."""
    from scope import subtree_doc_ids

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _outside = await _build_tree(client, admin_token, pid)

    # No scope ⇒ whole-project sentinel (empty list).
    assert await subtree_doc_ids("", pid) == []
    # Scoped ⇒ root + descendants, never outside docs.
    ids = set(await subtree_doc_ids(root, pid))
    assert ids == {root, child, grandchild}


# ─── Enforcement: read_document ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_read_in_subtree_ok(client, test_db, admin_user, project_with_doc):
    """A scoped key CAN read docs inside its subtree (root, child, grandchild)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    for doc_id in (root, child, grandchild):
        resp = await client.post(
            "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(scoped),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["doc_id"] == doc_id


@pytest.mark.asyncio
async def test_scoped_read_outside_subtree_forbidden(
    client, test_db, admin_user, project_with_doc,
):
    """A scoped key CANNOT read a doc outside its subtree."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/read_document", json={"document_id": outside}, headers=_hdr(scoped),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_scoped_read_outside_via_mcp(client, mcp_running, test_db, admin_user, project_with_doc):
    """The MCP gateway surface enforces the same subtree wall."""
    import json

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "read_document", "arguments": {"document_id": outside}}},
        headers={"Authorization": f"Bearer {scoped}", "Accept": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("result", {}).get("isError") is True
    payload = json.loads(data["result"]["content"][0]["text"])
    assert payload["status_code"] == 403


# ─── Enforcement: edit_document (write) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_edit_outside_subtree_forbidden(
    client, test_db, admin_user, project_with_doc,
):
    """A scoped key CANNOT edit a doc outside its subtree (auto-apply mode)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)
    # auto_apply + apply=auto so it goes straight to the gated apply path.
    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": outside, "old_string": "outside", "new_string": "X",
              "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_scoped_edit_inside_subtree_ok(client, test_db, admin_user, project_with_doc):
    """A scoped key CAN edit a doc inside its subtree (auto-apply mode)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, _outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": root, "old_string": "root body", "new_string": "edited",
              "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "applied"


# ─── Enforcement: search_materials ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_search_returns_only_subtree(client, test_db, admin_user, project_with_doc):
    """search_materials returns only in-scope docs; an outside doc never leaks."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    # Make the outside doc match the query too.
    await client.patch(
        f"/api/documents/{outside}", json={"content": "root body echo"},
        cookies={"lore_session": admin_token},
    )
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/search_materials", json={"query": "body", "k": 20},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text
    hits = resp.json()["hits"]
    hit_ids = {h["doc_id"] for h in hits}
    assert outside not in hit_ids, "outside doc leaked into scoped search"


# ─── Enforcement: get_project_structure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_structure_returns_only_subtree(
    client, test_db, admin_user, project_with_doc,
):
    """get_project_structure is scoped to the subtree (root + descendants only)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text
    ids = {d["document_id"] for d in resp.json()["documents"]}
    assert ids == {root, child, grandchild}, f"outside leaked: {ids}"
    assert outside not in ids


# ─── Enforcement: create_document ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_create_outside_parent_forbidden(
    client, test_db, admin_user, project_with_doc,
):
    """create_document with parent_id outside the subtree is rejected."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "NewDoc", "content": "x", "parent_id": outside, "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_scoped_create_no_parent_defaults_to_scope_root(
    client, test_db, admin_user, project_with_doc,
):
    """create_document with no parent_id lands at the subtree root (inside scope)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, _outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "NewDoc", "content": "x", "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["doc_id"]
    # The new doc must be a child of the scope root (inside the wall).
    from db import fetch_one
    created = await fetch_one("documents", new_id)
    from db import extract_id
    assert extract_id(created["parent_id"]) == root


# ─── Enforcement: move_document ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_move_null_parent_lands_under_scope_root(
    client, test_db, admin_user, project_with_doc,
):
    """move_document with parent_id null lands under the SCOPE root, not the
    project root — and the doc stays visible to the same key afterwards (the
    pre-fix escape was silent until the next read 403'd)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, _grandchild, _outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/move_document",
        json={"document_id": child, "parent_id": None, "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_id"] == root

    # Still INSIDE the wall: the same key can read it after the move.
    resp = await client.post(
        "/api/tool/read_document", json={"document_id": child}, headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_scoped_move_out_of_scope_parent_forbidden(
    client, test_db, admin_user, project_with_doc,
):
    """move_document to a parent outside the subtree is rejected with the
    out-of-scope 403 naming the scope root."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/move_document",
        json={"document_id": child, "parent_id": outside, "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 403, resp.text
    assert root in resp.json()["detail"]


@pytest.mark.asyncio
async def test_scoped_reference_move_null_hosts_on_scope_root(
    client, test_db, admin_user, project_with_doc,
):
    """A scoped reference move with parent_id null hosts on the scope root
    (200) instead of the reference-at-root 400 — null never means the project
    root under a scoped key."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, _grandchild, _outside = await _build_tree(client, admin_token, pid)
    ref_resp = await client.post(
        "/api/documents", json={
            "project_id": pid, "title": "Ref", "content": "r",
            "is_reference": True, "media_type": "markdown", "parent_id": child,
        }, cookies={"lore_session": admin_token},
    )
    assert ref_resp.status_code == 200, ref_resp.text
    ref_id = ref_resp.json()["document_id"]
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/move_document",
        json={"document_id": ref_id, "parent_id": None, "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_id"] == root


@pytest.mark.asyncio
async def test_unscoped_move_null_parent_still_project_root(
    client, test_db, admin_user, project_with_doc,
):
    """An UNSCOPED key's null move keeps today's meaning: the project root."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, child, _grandchild, _outside = await _build_tree(client, admin_token, pid)
    unscoped = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/move_document",
        json={"document_id": child, "parent_id": None, "apply": "auto"},
        headers=_hdr(unscoped),
    )
    assert resp.status_code == 200, resp.text
    from db import extract_id, fetch_one

    row = await fetch_one("documents", child)
    assert not (row.get("parent_id") and extract_id(row.get("parent_id")))


@pytest.mark.asyncio
async def test_scoped_move_scope_root_itself_null_parent_refused(
    client, test_db, admin_user, project_with_doc,
):
    """Moving the SCOPE ROOT itself with parent_id null is refused (400 own
    parent), not parked at the project root. Why pinned: pre-fix, a null root
    move skipped destination validation entirely, so the root-itself move was
    the same one-way escape; post-fix the resolution yields
    new_parent == document_id and the cycle guard catches it."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    # The scope root sits under a HOST doc, so "did not move" is a real check
    # (_build_tree's roots are parentless — a parentless refusal would be
    # indistinguishable from a successful root move on the row).
    host = await _make_doc(client, admin_token, pid, "Host")
    root = await _make_doc(
        client, admin_token, pid, "ScopedRoot", "root body and more text",
        parent_id=host,
    )
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    resp = await client.post(
        "/api/tool/move_document",
        json={"document_id": root, "parent_id": None, "apply": "auto"},
        headers=_hdr(scoped),
    )
    assert resp.status_code == 400, resp.text
    assert "own parent" in resp.json()["detail"]

    # Refused BEFORE the UPDATE: the root is still hosted, not root-less.
    from db import extract_id, fetch_one

    row = await fetch_one("documents", root)
    assert extract_id(row.get("parent_id")) == host


# ─── Out-of-scope transclusion ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_out_of_scope_transclusion_is_opaque_anchor(
    client, test_db, admin_user, project_with_doc,
):
    """A host in scope returns raw content with an opaque `![alt](doc:X)` anchor;
    read_document(X) is 403; X's body never appears."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    root, _child, _grandchild, outside = await _build_tree(client, admin_token, pid)
    # Put a secret marker in the outside doc, and an embed anchor in the host.
    await client.patch(
        f"/api/documents/{outside}", json={"content": "SECRET_OUTSIDE_BODY"},
        cookies={"lore_session": admin_token},
    )
    host = await _make_doc(
        client, admin_token, pid, "Host", f"see ![outside](doc:{outside})",
        parent_id=root,
    )
    scoped = await _make_agent_key(test_db, admin_uid, pid, scope_root=root)

    # Read the host: raw anchor present, secret body absent.
    resp = await client.post(
        "/api/tool/read_document", json={"document_id": host}, headers=_hdr(scoped),
    )
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert f"![outside](doc:{outside})" in content
    assert "SECRET_OUTSIDE_BODY" not in content

    # Reading the transclusion target directly is 403.
    resp2 = await client.post(
        "/api/tool/read_document", json={"document_id": outside}, headers=_hdr(scoped),
    )
    assert resp2.status_code == 403


# ─── Capability gating ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_widget_only_key_rejected_on_tool_api(client, test_db, admin_user, project_with_doc):
    """A key without the agent capability cannot be used on /api/tool/*."""
    pid, _idx, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(client, admin_token, pid, "WDoc", "x")
    widget = await _make_widget_key(test_db, admin_uid, pid, doc)

    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc}, headers=_hdr(widget),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_agent_only_key_rejected_on_widget(client, test_db, admin_user, project_with_doc):
    """A key without the widget capability cannot be used on /api/widget/*."""
    pid, _, admin_uid = project_with_doc
    agent = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.get("/api/widget/info", headers=_hdr(agent))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_combined_key_accepted_on_both_surfaces(
    client, test_db, admin_user, project_with_doc,
):
    """ONE key with both capabilities works on the widget surface AND the agent
    surface (document_id = bound doc = subtree root)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(client, admin_token, pid, "CombinedDoc", "combined body text")
    combined = await _make_key(
        test_db, admin_uid, pid, document_id=doc,
        capabilities=["widget", "agent"],
    )

    resp_w = await client.get("/api/widget/info", headers=_hdr(combined))
    assert resp_w.status_code == 200, resp_w.text

    resp_a = await client.post(
        "/api/tool/read_document", json={"document_id": doc}, headers=_hdr(combined),
    )
    assert resp_a.status_code == 200, resp_a.text


