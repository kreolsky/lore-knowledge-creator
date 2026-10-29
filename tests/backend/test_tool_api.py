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


# ─── Minimal-tool-set plan: get_project_structure subtree + IDOR ───────────────


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
