"""Cross-project document subtree move — POST /api/documents/{id}/move.

Covers:
- Two-ended gate: require_document_full on the moved doc (source) +
  require_project_full on the target project.
- Structural refusals BEFORE any write: same project 400; is_system/is_memory/
  is_index anywhere in the subtree 403; parent must live in the TARGET project
  (404 uniform), not be a reference (400), not be a system doc (400).
- Subtree rewrite: project_id follows docs AND references; descendants keep
  parent_id/sort_key; the root lands at the TOP of the target sibling group;
  colliding non-reference paths re-derive.
- Denormalized rows follow in one gather (doc_chunks, document_shares,
  chat_sessions, agent_configs, api_keys); source last-accessed pointers cleared
  (projects, project_members, user_preferences).
- Live collab session project_id patched in place; both events emitted with
  the right project_id routing.
"""


import pytest


async def _mk_doc(client, pid, token, *, title, parent_id=None, is_reference=False):
    body = {"project_id": pid, "title": title, "parent_id": parent_id}
    if is_reference:
        body.update({"is_reference": True, "media_type": "markdown", "content": "ref body"})
    r = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _mk_project(client, token, name):
    r = await client.post("/api/projects", json={"name": name}, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["project_id"], body["index_doc_id"]


async def _move(client, token, doc_id, target_pid, parent_id=None):
    return await client.post(
        f"/api/documents/{doc_id}/move",
        json={"target_project_id": target_pid, "parent_id": parent_id},
        cookies={"lore_session": token},
    )


async def _doc_rows(test_db, ids):
    """Raw documents rows keyed by bare id."""
    refs = ",".join(f"type::record('documents','{i}')" for i in ids)
    rows = await test_db.query(
        f"SELECT meta::id(id) AS id, project_id, parent_id, sort_key, path, deleted_at "
        f"FROM documents WHERE id IN [{refs}]"
    )
    return {r["id"]: r for r in (rows or [])}


# ─── Access gates: two-ended ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_requires_full_on_target_project(client, admin_user, regular_user):
    """Full on the source is not enough: readonly on the TARGET project → 403."""
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user

    # regular_user owns the source (full), is readonly on the admin-owned target.
    src_pid, src_idx = await _mk_project(client, user_token, "Move Source B")
    tgt_pid, _ = await _mk_project(client, admin_token, "Move Target B")
    doc = await _mk_doc(client, src_pid, user_token, title="Doc", parent_id=src_idx)
    r = await client.post(
        f"/api/admin/projects/{tgt_pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    resp = await _move(client, user_token, doc, tgt_pid)
    assert resp.status_code == 403, resp.text


# ─── Subtree rewrite ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_document_with_target_index_parent_lands_at_target_root(
    client, test_db, admin_user, project_with_doc,
):
    """A document moved with parent_id = the TARGET project's index is stored
    at the target ROOT: the tree renders the index row childless, so a
    document under it exists but is invisible."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, target_idx = await _mk_project(client, token, "Move Target P")

    doc = await _mk_doc(client, pid, token, title="IdxParentDoc")
    resp = await _move(client, token, doc, target_pid, parent_id=target_idx)
    assert resp.status_code == 200, resp.text

    rows = await _doc_rows(test_db, [doc])
    assert rows[doc]["project_id"] == target_pid
    assert rows[doc]["parent_id"] is None, \
        "target index as parent = target root for a document"


@pytest.mark.asyncio
async def test_move_reference_with_target_index_parent_keeps_host(
    client, test_db, admin_user, project_with_doc,
):
    """CARVE-OUT: a REFERENCE moved with parent_id = the target project's
    index keeps the index as its host — the canonical project-level host."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, target_idx = await _mk_project(client, token, "Move Target Q")

    host = await _mk_doc(client, pid, token, title="RefHost")
    ref = await _mk_doc(
        client, pid, token, title="Ref", parent_id=host, is_reference=True,
    )
    resp = await _move(client, token, ref, target_pid, parent_id=target_idx)
    assert resp.status_code == 200, resp.text

    rows = await _doc_rows(test_db, [ref])
    assert rows[ref]["project_id"] == target_pid
    assert rows[ref]["parent_id"] == target_idx, \
        "a reference keeps the target index as its host"


@pytest.mark.asyncio
async def test_move_rederives_colliding_path(
    client, test_db, admin_user, project_with_doc,
):
    """A moved doc whose path already exists in the target gets a re-derived
    unique slug (idx_documents_path is UNIQUE on (project_id, path))."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, _ = await _mk_project(client, token, "Move Target I")

    await _mk_doc(client, target_pid, token, title="Collide")
    mover = await _mk_doc(client, pid, token, title="Collide")

    resp = await _move(client, token, mover, target_pid)
    assert resp.status_code == 200, resp.text

    rows = await _doc_rows(test_db, [mover])
    assert rows[mover]["path"] != "collide.md", "path must be re-derived on collision"
    # No duplicate (project_id, path) pair remains.
    dupes = await test_db.query(
        "SELECT VALUE count() FROM documents "
        "WHERE project_id = $pid AND path = $path AND deleted_at IS NONE GROUP ALL",
        {"pid": target_pid, "path": rows[mover]["path"]},
    )
    assert not dupes or dupes[0] == 1


# ─── Live session patch + events ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_patches_live_session_project_id(
    client, admin_user, project_with_doc, _clear_sessions,
):
    """A live collab session caches project_id; the move patches it in place so
    the next flush writes doc_chunks under the NEW project."""
    from collab.registry import _session_key, _sessions, get_active_session
    from collab.session import CollabSession
    from pycrdt import Doc, Text

    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, _ = await _mk_project(client, token, "Move Target M")
    doc = await _mk_doc(client, pid, token, title="LiveDoc")

    ydoc = Doc()
    text = ydoc.get("content", type=Text)
    text += "live body"
    session = CollabSession(
        entity_type="doc", entity_id=doc, ydoc=ydoc, project_id=pid,
    )
    _sessions[_session_key("doc", doc)] = session
    assert get_active_session("doc", doc).project_id == pid

    resp = await _move(client, token, doc, target_pid)
    assert resp.status_code == 200, resp.text
    assert get_active_session("doc", doc).project_id == target_pid
