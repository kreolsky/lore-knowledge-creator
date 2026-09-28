"""Subtree delete for documents — the delete_children flag (single + batch).

Covers:
- delete_children=true (default): whole-subtree soft-delete, NO reparenting anywhere,
  exactly one documents_deleted_batch event, per-descendant cascade cleanup.
- delete_children=false: legacy lift mode verbatim (live children reparented to the
  grandparent).
- Batch endpoint expands the id set to live descendants; the protected-skeleton 403
  runs over the EXPANDED set (a protected doc under a deleted parent rejects the batch).
- Access gates are split by mode: subtree gates require_project_full on the target's
  project; lift mode keeps require_document_full.
"""

import json
from uuid import uuid4

import pytest
from emit_recorder import EmitRecorder


async def _mk_doc(client, pid, token, *, title, parent_id=None, is_reference=False):
    body = {"project_id": pid, "title": title, "parent_id": parent_id}
    if is_reference:
        body.update({"is_reference": True, "media_type": "markdown", "content": "ref body"})
    r = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _doc_rows(test_db, ids):
    """Raw documents rows keyed by bare id (deleted_at + parent_id snapshot)."""
    refs = ",".join(f"type::record('documents','{i}')" for i in ids)
    rows = await test_db.query(
        f"SELECT meta::id(id) AS id, parent_id, deleted_at FROM documents WHERE id IN [{refs}]"
    )
    return {r["id"]: r for r in (rows or [])}


# ─── Single delete: subtree mode ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subtree_delete_tombstones_whole_tree_no_reparent(
    client, test_db, admin_user, project_with_doc,
):
    """Default delete_children=true: target + all live descendants (docs AND refs)
    tombstoned; no parent_id mutation anywhere; an outside sibling stays live."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent", parent_id=idx_id)
    child = await _mk_doc(client, pid, token, title="Child", parent_id=parent)
    grandchild = await _mk_doc(client, pid, token, title="Grandchild", parent_id=child)
    ref_child = await _mk_doc(client, pid, token, title="RefChild", parent_id=parent, is_reference=True)
    ref_deep = await _mk_doc(client, pid, token, title="RefDeep", parent_id=grandchild, is_reference=True)
    outsider = await _mk_doc(client, pid, token, title="Outsider", parent_id=idx_id)

    subtree = [parent, child, grandchild, ref_child, ref_deep]
    before = await _doc_rows(test_db, subtree)

    resp = await client.delete(f"/api/documents/{parent}", cookies=cookies)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["deleted"] == 5

    after = await _doc_rows(test_db, subtree)
    for did in subtree:
        assert after[did]["deleted_at"] is not None, f"{did} must be tombstoned"
        assert after[did]["parent_id"] == before[did]["parent_id"], \
            "subtree delete must never reparent"

    outsider_rows = await _doc_rows(test_db, [outsider])
    assert outsider_rows[outsider]["deleted_at"] is None


@pytest.mark.asyncio
async def test_lift_mode_reparents_children_to_grandparent(
    client, test_db, admin_user, project_with_doc,
):
    """delete_children=false keeps today's behavior: child lifted to the grandparent."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent", parent_id=idx_id)
    child = await _mk_doc(client, pid, token, title="Child", parent_id=parent)

    resp = await client.delete(
        f"/api/documents/{parent}?delete_children=false", cookies=cookies,
    )
    assert resp.status_code == 200, resp.text

    rows = await _doc_rows(test_db, [parent, child])
    assert rows[parent]["deleted_at"] is not None
    assert rows[child]["deleted_at"] is None, "lift mode keeps the child alive"
    assert rows[child]["parent_id"] == idx_id, "child lifted to the grandparent"


@pytest.mark.asyncio
async def test_subtree_delete_emits_single_batch_event(
    monkeypatch, client, admin_user, project_with_doc,
):
    """Subtree mode emits ONE documents_deleted_batch and no per-doc
    entity_deleted/document_deleted/reference_deleted."""

    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent", parent_id=idx_id)
    child = await _mk_doc(client, pid, token, title="Child", parent_id=parent)
    ref = await _mk_doc(client, pid, token, title="Ref", parent_id=child, is_reference=True)

    with EmitRecorder.active() as rec:
        resp = await client.delete(f"/api/documents/{parent}", cookies=cookies)
    assert resp.status_code == 200, resp.text
    captured = rec.calls

    batch = [(t, kw) for t, kw in captured if t == "documents_deleted_batch"]
    assert len(batch) == 1, f"exactly one batch event, got {[t for t, _ in captured]}"
    assert set(batch[0][1]["document_ids"]) == {parent, child, ref}
    assert set(batch[0][1]["reference_ids"]) == {ref}
    for forbidden in ("entity_deleted", "document_deleted", "reference_deleted"):
        assert forbidden not in [t for t, _ in captured], \
            "subtree mode must not emit per-doc events"


@pytest.mark.asyncio
async def test_subtree_delete_cascades_descendant_dependents(
    client, test_db, admin_user, project_with_doc,
):
    """Per-descendant cascade: chat sessions/messages soft-deleted, doc_chunks /
    doc_mentions for a DESCENDANT cleaned."""
    from db import create_record

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent")
    child = await _mk_doc(client, pid, token, title="Child", parent_id=parent)

    session_id = str(uuid4())
    await create_record("chat_sessions", session_id, {
        "project_id": pid, "document_id": child, "user_id": "cascade-user",
    })
    await create_record("messages", str(uuid4()), {
        "chat_id": session_id, "role": "user", "content": "hi",
    })
    await create_record("doc_chunks", str(uuid4()), {
        "document_id": child, "project_id": pid, "ord": 0, "content": "chunk",
        "offset_start": 0, "offset_end": 5, "content_version": 1,
        "embedding": [0.1, 0.2],
    })
    # ids are server-generated UUIDs (from our own POSTs) — safe to interpolate;
    # ⟨⟩ brackets escape the dashes for the record-literal parse.
    await test_db.query(
        f"RELATE documents:⟨{parent}⟩->doc_mentions->documents:⟨{child}⟩"
    )

    resp = await client.delete(f"/api/documents/{parent}", cookies=cookies)
    assert resp.status_code == 200, resp.text

    chat_rows = await test_db.query(
        "SELECT deleted_at FROM type::record('chat_sessions', $id)", {"id": session_id},
    )
    assert chat_rows and chat_rows[0]["deleted_at"] is not None

    msg_rows = await test_db.query(
        "SELECT deleted_at FROM messages WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": session_id},
    )
    assert msg_rows == [], "messages of a descendant chat must be soft-deleted"

    chunks = await test_db.query(
        "SELECT VALUE count() FROM doc_chunks WHERE document_id = $did GROUP ALL",
        {"did": child},
    )
    assert not chunks or chunks[0] == 0, "descendant chunks must be hard-deleted"

    mentions = await test_db.query(
        "SELECT VALUE count() FROM doc_mentions "
        "WHERE in = type::record('documents', $src) OR out = type::record('documents', $dst)",
        {"src": parent, "dst": child},
    )
    assert not mentions or mentions[0] == 0, "descendant mentions must be deleted"


@pytest.mark.asyncio
async def test_subtree_delete_rejects_protected_descendant(
    client, test_db, admin_user, project_with_doc,
):
    """A protected skeleton under the target rejects the whole subtree delete (403),
    all-or-nothing — structurally impossible via the API today, so the row is seeded."""
    from db import create_record

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent")
    protected_id = "subtree-protected-child"
    await create_record("documents", protected_id, {
        "project_id": pid, "parent_id": parent, "title": "ProtectedFolder",
        "content": "x", "path": ".lore/system/subtree_protected",
        "is_system": True, "system_role": "memory_folder",
    })

    resp = await client.delete(f"/api/documents/{parent}", cookies=cookies)
    assert resp.status_code == 403

    rows = await _doc_rows(test_db, [parent, protected_id])
    assert rows[parent]["deleted_at"] is None, "nothing tombstoned on rejection"
    assert rows[protected_id]["deleted_at"] is None


# ─── Batch endpoint: expansion + lift mode ───────────────────────────────────


@pytest.mark.asyncio
async def test_batch_delete_expands_subtree(
    sync_app, client, test_db, collab_project,
):
    """Batch-delete of a parent (default delete_children=true) tombstones the whole
    subtree and broadcasts the EXPANDED id set in the single batch event."""
    pid, _, admin_token, user_token, *_ = collab_project

    parent = await _mk_doc(client, pid, admin_token, title="Parent")
    child = await _mk_doc(client, pid, admin_token, title="Child", parent_id=parent)
    grandchild = await _mk_doc(client, pid, admin_token, title="GC", parent_id=child)
    ref = await _mk_doc(client, pid, admin_token, title="Ref", parent_id=grandchild, is_reference=True)
    before = await _doc_rows(test_db, [parent, child, grandchild, ref])

    with sync_app.websocket_connect(
        f"/ws/project/{pid}", cookies={"lore_session": user_token}
    ) as ws:
        json.loads(ws.receive_text())  # init
        resp = await client.post(
            "/api/documents/batch-delete",
            json={"document_ids": [parent]},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == 4

        msg = json.loads(ws.receive_text())
        assert msg["type"] == "documents_deleted_batch"
        assert set(msg["document_ids"]) == {parent, child, grandchild, ref}
        assert ref in msg["reference_ids"]

    after = await _doc_rows(test_db, [parent, child, grandchild, ref])
    for did in (parent, child, grandchild, ref):
        assert after[did]["deleted_at"] is not None
        assert after[did]["parent_id"] == before[did]["parent_id"], \
            "subtree batch delete must never reparent"


@pytest.mark.asyncio
async def test_batch_delete_lift_mode_keeps_children(
    client, test_db, admin_user, project_with_doc,
):
    """delete_children=false on the batch endpoint keeps the lift behavior."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent", parent_id=idx_id)
    child = await _mk_doc(client, pid, token, title="Child", parent_id=parent)

    resp = await client.post(
        "/api/documents/batch-delete",
        json={"document_ids": [parent], "delete_children": False},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1

    rows = await _doc_rows(test_db, [parent, child])
    assert rows[parent]["deleted_at"] is not None
    assert rows[child]["deleted_at"] is None
    assert rows[child]["parent_id"] == idx_id


@pytest.mark.asyncio
async def test_batch_delete_rejects_protected_descendant(
    client, test_db, admin_user, project_with_doc,
):
    """Protected-skeleton 403 runs over the EXPANDED set: a protected folder under a
    deleted parent rejects the batch instead of slipping through as a descendant."""
    from db import create_record

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    parent = await _mk_doc(client, pid, token, title="Parent")
    protected_id = "batch-protected-descendant"
    await create_record("documents", protected_id, {
        "project_id": pid, "parent_id": parent, "title": "RulesFolder",
        "content": "x", "path": ".lore/system/rules_folder",
        "is_system": True, "system_role": "rules_folder",
    })

    resp = await client.post(
        "/api/documents/batch-delete",
        json={"document_ids": [parent]},
        cookies=cookies,
    )
    assert resp.status_code == 403

    rows = await _doc_rows(test_db, [parent, protected_id])
    assert rows[parent]["deleted_at"] is None
    assert rows[protected_id]["deleted_at"] is None


# ─── Access gates: split by mode ─────────────────────────────────────────────


async def _commentator_with_target(client, admin_user, regular_user, project_with_doc):
    """Project-commentator plus a delete TARGET with one child."""
    pid, idx_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user

    r = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "commentator"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    target = await _mk_doc(client, pid, admin_token, title="Target", parent_id=idx_id)
    child = await _mk_doc(client, pid, admin_token, title="Child", parent_id=target)
    return pid, idx_id, target, child, user_token


@pytest.mark.asyncio
async def test_subtree_delete_403_for_commentator(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """Subtree mode gates require_project_full: a commentator cannot delete."""
    pid, _, target, child, user_token = await _commentator_with_target(
        client, admin_user, regular_user, project_with_doc,
    )

    resp = await client.delete(
        f"/api/documents/{target}?delete_children=true",
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403, resp.text

    rows = await _doc_rows(test_db, [target, child])
    assert rows[target]["deleted_at"] is None
    assert rows[child]["deleted_at"] is None


@pytest.mark.asyncio
async def test_project_full_member_both_modes_200(client, admin_user, project_with_doc):
    """A project-full principal passes both gates (owner here)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    t1 = await _mk_doc(client, pid, token, title="T1")
    t2 = await _mk_doc(client, pid, token, title="T2")
    assert (await client.delete(f"/api/documents/{t1}", cookies=cookies)).status_code == 200
    assert (await client.delete(
        f"/api/documents/{t2}?delete_children=false", cookies=cookies,
    )).status_code == 200
