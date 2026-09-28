"""Integration tests for the Tool-API (plan stage-1, §3.2 / §3.8).

The Tool-API is the stable HTTP/JSON surface the home agent (Pi, stage-1 phase 2)
and future external brains (stage 2) call. It authenticates an agent-capable key
('agent' in capabilities), resolves the OWNING user, and enforces RBAC
per target document. Writes funnel through the live CRDT path + a pre-edit checkpoint.
Confirmation mode holds the call for mid-turn approval; AS mode applies directly.

# ARCH: contract tests WITHOUT Pi (plan §5 test strategy) — the spine is provable
# on its own.
"""

import hashlib
import secrets

import pytest

# ─── Test helpers ─────────────────────────────────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str, label: str = "agent") -> str:
    """Insert a project-scoped agent API key and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",  # project-scoped (GAP-5): no per-doc binding
        "token_hash": token_hash,
        "label": label,
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _add_member(test_db, project_id: str, user_id: str, level: str) -> None:
    pm_id = f"pm-{project_id}-{user_id}"
    await test_db.query(
        "DELETE type::record('project_members', $id)", {"id": pm_id},
    )
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = $lvl",
        {"id": pm_id, "pid": project_id, "uid": user_id, "lvl": level},
    )


async def _make_doc(client, token: str, project_id: str, title: str, content: str = "") -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


# ─── Auth ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_no_auth_rejected(client):
    resp = await client.post("/api/tool/read_document", json={"document_id": "x"})
    assert resp.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_tool_invalid_key_rejected(client):
    resp = await client.post(
        "/api/tool/read_document",
        json={"document_id": "x"},
        headers=_hdr("lore_deadbeef"),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_tool_keys_route_deleted(client, admin_user, project_with_doc):
    """/api/tool/keys is gone — issuance is unified under POST /api/api-keys
    (plan "glimmering-knitting-pebble", covered in test_unified_key_issuance)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/tool/keys", json={"project_id": pid},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (404, 405)


# ─── Read tools ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_document(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "ReadMe", "# Hello world")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["doc_id"] == doc_id
    assert "Hello world" in data["content"]
    # Minimal-tool-set plan Task 1: metadata fields present.
    for field in ("created_at", "updated_at", "is_index", "is_reference", "is_system"):
        assert field in data, f"read_document must return {field}"
    assert data["is_index"] is False
    assert data["is_reference"] is False
    assert data["is_system"] is False


@pytest.mark.asyncio
async def test_search_materials(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    await _make_doc(client, token, pid, "Dragon Lore", "The wyrm sleeps beneath the mountain.")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials", json={"query": "dragon"}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    hits = resp.json()["hits"]
    assert any("Dragon" in h.get("title", "") for h in hits)


@pytest.mark.asyncio
async def test_get_project_structure(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    await _make_doc(client, token, pid, "StructChild", "body")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    titles = [d["title"] for d in docs]
    assert "StructChild" in titles


@pytest.mark.asyncio
async def test_references_visible_via_project_structure(client, test_db, admin_user, project_with_doc):
    """Plan tool-surface-consolidation Step 2a: list_references folded into
    get_project_structure. A reference shows up as a document row with
    is_reference=true + media_type/source_url. Since plan structure-layered-walk
    the ROOT call hides references by default — the fold is reached with
    include_references (or a start_id call)."""
    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    # Create a reference attached to the index doc
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "RefOne",
              "content": "ref body", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    # Default root call: hidden, but COUNTED on the host row (never silent).
    resp = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    idx_row = next(
        d for d in resp.json()["documents"] if d["document_id"] == idx_id
    )
    assert idx_row["n_references"] == 1
    assert not any(d.get("is_reference") for d in resp.json()["documents"])

    resp = await client.post(
        "/api/tool/get_project_structure",
        json={"include_references": True}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    refs = [d for d in docs if d.get("is_reference")]
    assert any(d["title"] == "RefOne" and d.get("media_type") == "markdown" for d in refs)


# ─── RBAC ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readonly_user_cannot_edit(client, test_db, admin_user, regular_user, project_with_doc):
    """Agent acts under the OWNING user's rights (Q2): a viewer's agent key cannot write."""
    pid, _, _ = project_with_doc
    _, admin_tok = admin_user
    user_uid, _ = regular_user
    await _add_member(test_db, pid, user_uid, "readonly")
    doc_id = await _make_doc(client, admin_tok, pid, "RbacDoc", "original text")
    agent_tok = await _make_agent_key(test_db, user_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "original", "new_string": "changed",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    # Even with apply=auto, a viewer cannot write — never mutates.
    assert resp.status_code == 403
    # Document untouched
    rd = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(agent_tok),
    )
    assert "original text" in rd.json()["content"]


@pytest.mark.asyncio
async def test_cross_project_doc_rejected(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)
    # A doc id that does not exist in this project
    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": "does-not-exist-xyz", "old_string": "a", "new_string": "b",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 404


# ─── Edit: AS (auto-apply) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_auto_applies_and_checkpoints(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "AutoDoc", "alpha beta gamma")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "beta", "new_string": "BETA",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    # Content mutated through the CRDT path
    rd = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(agent_tok),
    )
    assert "alpha BETA gamma" in rd.json()["content"]

    # A pre-edit checkpoint was created
    cps = await test_db.query(
        "SELECT * FROM checkpoints WHERE document_id = $did AND label = 'agent-auto'",
        {"did": doc_id},
    )
    assert len(cps) >= 1


# ─── Region-lock (advisory pre-apply UX check, plan §3.6) ─────────────────────


@pytest.mark.asyncio
async def test_as_edit_region_locked_by_human_selection(
    client, test_db, admin_user, project_with_doc,
):
    """An AS edit whose resolved region intersects another participant's active
    selection is rejected with 409 region_locked — best-effort, advisory."""
    from collab import selection_registry as reg

    reg._reset()
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "LockDoc", "alpha beta gamma")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    # Simulate another human participant actively selecting across "beta" [6,10).
    reg.track(doc_id, "human-observer", "Human", 6, 10)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "beta", "new_string": "BETA",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 409, resp.text
    data = resp.json()["detail"]
    assert data["code"] == "region_locked"
    assert data["user"] == "Human"

    # NOT mutated.
    rd = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(agent_tok),
    )
    assert "alpha beta gamma" in rd.json()["content"]
    reg._reset()


@pytest.mark.asyncio
async def test_as_edit_allowed_when_selection_disjoint(
    client, test_db, admin_user, project_with_doc,
):
    from collab import selection_registry as reg

    reg._reset()
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "LockDoc2", "alpha beta gamma")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    # Human selecting "alpha" [0,5) — disjoint from the agent's edit of "beta".
    reg.track(doc_id, "human-observer", "Human", 0, 5)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "beta", "new_string": "BETA",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    reg._reset()


# ─── Create document / reference ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_document_auto(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "NewDoc", "content": "# Fresh", "parent_id": None,
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    assert data["doc_id"]


@pytest.mark.asyncio
async def test_create_reference_unified_via_create_document(
    client, test_db, admin_user, project_with_doc,
):
    """D4 (plan internal-agent-surface-reconciliation): the standalone
    /create_reference route is gone; a reference is created through the unified
    create_document surface (node_type="reference" + parent_id = the host)."""
    pid, idx_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/create_document",
        json={"parent_id": idx_id, "node_type": "reference",
              "title": "RefAuto", "content": "ref",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"


# ─── CRDT convergence / isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_isolates_other_documents(
    client, test_db, admin_user, project_with_doc,
):
    """Editing doc A leaves doc B untouched (negative test, plan §5)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_a = await _make_doc(client, token, pid, "DocA", "target text")
    doc_b = await _make_doc(client, token, pid, "DocB", "bystander text")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_a, "old_string": "target", "new_string": "hit",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.json()["status"] == "applied"

    rb = await client.post(
        "/api/tool/read_document", json={"document_id": doc_b}, headers=_hdr(agent_tok),
    )
    assert "bystander text" in rb.json()["content"]


# ─── Review fixes: stale-key revocation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_key_revoked_membership_denied(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """A revoked user's agent key can no longer read (review: stale-credential IDOR)."""
    pid, _, _ = project_with_doc
    _, admin_tok = admin_user
    user_uid, _ = regular_user
    await _add_member(test_db, pid, user_uid, "full")
    await _make_doc(client, admin_tok, pid, "RevokableDoc", "secret")
    agent_tok = await _make_agent_key(test_db, user_uid, pid)

    # While a member: read works.
    ok = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(agent_tok),
    )
    assert ok.status_code == 200

    # Revoke membership → the key (still in api_keys) must stop working.
    await test_db.query(
        "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": pid, "uid": user_uid},
    )
    denied = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(agent_tok),
    )
    assert denied.status_code == 403


# ─── Minimal-tool-set plan: unified create_document +is_reference ──────────────


@pytest.mark.asyncio
async def test_create_document_reference_auto(client, test_db, admin_user, project_with_doc):
    """create_document with node_type="reference" + apply=auto creates a
    reference (auto path). media_type is DERIVED server-side (markdown) — an
    audio/image node with no file is unexpressible."""
    pid, idx_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "UnifiedRef", "content": "ref body", "parent_id": idx_id,
              "node_type": "reference", "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    ref_id = data.get("reference_id") or data.get("doc_id")
    assert ref_id

    # The row is a reference; the kind-derived media_type is markdown.
    rows = await test_db.query(
        "SELECT is_reference, media_type, parent_id FROM "
        "type::record('documents', $id)",
        {"id": ref_id},
    )
    assert rows, "reference row must exist"
    row = rows[0]
    assert row["is_reference"] is True
    assert row["media_type"] == "markdown"
    from db import extract_id
    assert extract_id(row["parent_id"]) == idx_id


@pytest.mark.asyncio
async def test_create_document_reference_without_host_rejected(
    client, test_db, admin_user, project_with_doc,
):
    """A reference requires a host (parent_id). The deprecated
    create_reference made document_id required; the unified create_document must
    preserve that — a host-less reference is rejected with 422 (no orphan).

    Plan agent-document-placement-and-node-type: the omitted-placement carve-out
    is gone — a plain create without parent_id is ALSO a 422 under an unscoped
    key (explicit null is the deliberate-root spelling)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "Orphan", "content": "x", "parent_id": None,
              "node_type": "reference", "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 422

    # A plain create without parent_id is no longer a silent root landing.
    bare = await client.post(
        "/api/tool/create_document",
        json={"title": "Bare", "content": "x", "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert bare.status_code == 422

    # Explicit null IS the deliberate root.
    ok = await client.post(
        "/api/tool/create_document",
        json={"title": "PlainOk", "content": "x", "parent_id": None,
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_get_project_structure_accepts_bodyless_post(
    client, test_db, admin_user, project_with_doc,
):
    """Review fix: the body is optional — a bodyless POST still works (contract
    preserved from the pre-unification endpoint)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)
    resp = await client.post(
        "/api/tool/get_project_structure", headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    assert "documents" in resp.json()


# ─── Minimal-tool-set plan: get_project_structure subtree + IDOR ───────────────


@pytest.mark.asyncio
async def test_get_project_structure_subtree_with_start_id(
    client, test_db, admin_user, project_with_doc,
):
    """start_id scopes to that node + its descendants; the index/root outside the
    subtree is intentionally NOT included."""
    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    # Build: idx -> child -> grandchild (siblings to keep BFS meaningful).
    child = await _make_doc(client, token, pid, "Child", "c")
    await client.patch(
        f"/api/documents/{child}",
        json={"parent_id": idx_id},
        cookies={"lore_session": token},
    )
    grand = await _make_doc(client, token, pid, "Grand", "g")
    await client.patch(
        f"/api/documents/{grand}",
        json={"parent_id": child},
        cookies={"lore_session": token},
    )

    resp = await client.post(
        "/api/tool/get_project_structure",
        json={"start_id": child}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    ids = {d["document_id"] for d in docs}
    assert child in ids          # seed
    assert grand in ids          # descendant
    assert idx_id not in ids     # parent of seed intentionally excluded


@pytest.mark.asyncio
async def test_get_project_structure_depth_cap(
    client, test_db, admin_user, project_with_doc,
):
    """depth=1 returns only the seed's direct children (no grandchildren)."""
    pid, idx_id, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    child = await _make_doc(client, token, pid, "CapChild", "c")
    await client.patch(
        f"/api/documents/{child}", json={"parent_id": idx_id},
        cookies={"lore_session": token},
    )
    grand = await _make_doc(client, token, pid, "CapGrand", "g")
    await client.patch(
        f"/api/documents/{grand}", json={"parent_id": child},
        cookies={"lore_session": token},
    )

    resp = await client.post(
        "/api/tool/get_project_structure",
        json={"start_id": idx_id, "depth": 1}, headers=_hdr(agent_tok),
    )
    docs = resp.json()["documents"]
    ids = {d["document_id"] for d in docs}
    assert idx_id in ids
    assert child in ids      # level 1
    assert grand not in ids  # level 2 — capped out


@pytest.mark.asyncio
async def test_get_project_structure_includes_is_system(
    client, test_db, admin_user, project_with_doc,
):
    """Each row carries is_system (parity with read_document metadata)."""
    from db import create_record
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    sys_id = "struct-sys-parity"
    await test_db.query("DELETE type::record('documents', $id)", {"id": sys_id})
    await create_record("documents", sys_id, {
        "project_id": pid, "parent_id": None, "title": "Rules",
        "content": "r", "path": ".lore/system/rules-parity",
        "is_system": True, "system_role": "rules",
    })
    resp = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(agent_tok),
    )
    docs = resp.json()["documents"]
    sys_row = next((d for d in docs if d["document_id"] == sys_id), None)
    assert sys_row is not None
    assert sys_row["is_system"] is True


@pytest.mark.asyncio
async def test_get_project_structure_cross_project_start_id_empty(
    client, test_db, admin_user, project_with_doc,
):
    """Negative / IDOR: a start_id from ANOTHER project returns nothing — the
    per-level project_id invariant prevents a subtree leak."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/get_project_structure",
        json={"start_id": "does-not-exist-in-project"}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["documents"] == []


@pytest.mark.asyncio
async def test_read_document_cross_project_404(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """Negative: read_document on a doc id that exists in another project still
    404s (no existence oracle / no cross-project read)."""
    # Create a doc in a SECOND project owned by the regular user.
    pid2 = "test-project-struct-other"
    uid2, tok2 = regular_user
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid2})
    await test_db.query("DELETE type::record('project_members', $id)", {"id": "pm-other"})
    await test_db.query(
        "CREATE type::record('projects', $id) SET name='Other', status='active', "
        "project_context='', owner_id=$uid",
        {"id": pid2, "uid": uid2},
    )
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id=$pid, "
        "user_id=$uid, access_level='full'",
        {"id": "pm-other", "pid": pid2, "uid": uid2},
    )
    other_doc = await _make_doc(client, tok2, pid2, "OtherDoc", "secret other")

    # The admin's agent key is scoped to the FIRST project (project_with_doc).
    _, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, project_with_doc[0])

    resp = await client.post(
        "/api/tool/read_document",
        json={"document_id": other_doc}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 404
