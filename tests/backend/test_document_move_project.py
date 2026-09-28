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

import json
from uuid import uuid4

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
async def test_move_requires_full_on_source_doc(
    client, admin_user, regular_user, project_with_doc,
):
    """A readonly member of the SOURCE project gets 403; a non-member gets the
    uniform 404 (no cross-project existence oracle)."""
    pid, idx_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user

    target_pid, _ = await _mk_project(client, admin_token, "Move Target A")
    doc = await _mk_doc(client, pid, admin_token, title="Movable", parent_id=idx_id)

    r = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    resp = await _move(client, user_token, doc, target_pid)
    assert resp.status_code == 403, resp.text
    assert "Write access required" in resp.json()["detail"]

    # A user with NO access to the source doc's project: uniform 404 (never a
    # distinct 403 — no cross-project existence oracle).
    from helpers import make_token

    from db import create_record

    outsider_uid = "move-outsider-001"
    await create_record("users", outsider_uid, {
        "name": "outsider", "email": "outsider-move@test.com",
        "password_hash": "x", "role": "user", "user_facts": "",
    })
    outsider_token = make_token(outsider_uid, "outsider", "user", "outsider-move@test.com")
    resp = await _move(client, outsider_token, doc, target_pid)
    assert resp.status_code == 404, resp.text


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


@pytest.mark.asyncio
async def test_move_same_project_400(client, test_db, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _mk_doc(client, pid, token, title="Doc")
    resp = await _move(client, token, doc, pid)
    assert resp.status_code == 400, resp.text
    rows = await _doc_rows(test_db, [doc])
    assert rows[doc]["project_id"] == pid, "refused move must write nothing"


# ─── Structural refusals ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_refuses_system_and_memory_and_index(
    client, test_db, admin_user, project_with_doc,
):
    """is_system / is_memory / is_index on the root OR a descendant → 403,
    all-or-nothing (nothing written)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, _ = await _mk_project(client, token, "Move Target C")

    for flag in ("is_system", "is_memory", "is_index"):
        # flag on the ROOT
        root = await _mk_doc(client, pid, token, title=f"Root {flag}")
        await test_db.query(
            f"UPDATE type::record('documents', $id) SET {flag} = true",
            {"id": root},
        )
        resp = await _move(client, token, root, target_pid)
        assert resp.status_code == 403, f"{flag} root must be refused: {resp.text}"

        # flag on a DESCENDANT
        root2 = await _mk_doc(client, pid, token, title=f"Root2 {flag}")
        child = await _mk_doc(client, pid, token, title="Child", parent_id=root2)
        await test_db.query(
            f"UPDATE type::record('documents', $id) SET {flag} = true",
            {"id": child},
        )
        resp = await _move(client, token, root2, target_pid)
        assert resp.status_code == 403, f"{flag} descendant must be refused: {resp.text}"

        rows = await _doc_rows(test_db, [root, root2, child])
        for did in (root, root2, child):
            assert rows[did]["project_id"] == pid, "refused move must write nothing"


@pytest.mark.asyncio
async def test_move_target_parent_must_be_in_target_project(
    client, admin_user, project_with_doc,
):
    """A parent from a THIRD project → uniform 404 (same rule as
    assert_parent_valid: no cross-project existence oracle)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, _ = await _mk_project(client, token, "Move Target D")
    third_pid, third_idx = await _mk_project(client, token, "Move Third D")

    doc = await _mk_doc(client, pid, token, title="Doc")
    resp = await _move(client, token, doc, target_pid, parent_id=third_idx)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_move_target_parent_reference_400(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, target_idx = await _mk_project(client, token, "Move Target E")
    ref_parent = await _mk_doc(
        client, target_pid, token, title="RefParent", parent_id=target_idx,
        is_reference=True,
    )
    doc = await _mk_doc(client, pid, token, title="Doc")
    resp = await _move(client, token, doc, target_pid, parent_id=ref_parent)
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_move_target_parent_system_doc_400(client, test_db, admin_user, project_with_doc):
    from db import create_record

    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, target_idx = await _mk_project(client, token, "Move Target F")
    sys_parent = "move-target-system-parent"
    await create_record("documents", sys_parent, {
        "project_id": target_pid, "parent_id": target_idx, "title": "SystemFolder",
        "content": "x", "path": ".lore/system/move_folder",
        "is_system": True, "system_role": "memory_folder",
    })
    doc = await _mk_doc(client, pid, token, title="Doc")
    resp = await _move(client, token, doc, target_pid, parent_id=sys_parent)
    assert resp.status_code == 400, resp.text


# ─── Subtree rewrite ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_subtree_rewrites_project_id_and_keeps_descendant_parents(
    client, test_db, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, target_idx = await _mk_project(client, token, "Move Target G")

    parent = await _mk_doc(client, pid, token, title="Parent")
    child = await _mk_doc(client, pid, token, title="Child", parent_id=parent)
    ref = await _mk_doc(client, pid, token, title="Ref", parent_id=child, is_reference=True)
    outsider = await _mk_doc(client, pid, token, title="Outsider")
    target_doc = await _mk_doc(client, target_pid, token, title="TargetDoc", parent_id=target_idx)

    resp = await _move(client, token, parent, target_pid, parent_id=target_doc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved"] == 3
    assert set(body["document_ids"]) == {parent, child}
    assert body["reference_ids"] == [ref]

    rows = await _doc_rows(test_db, [parent, child, ref, outsider])
    for did in (parent, child, ref):
        assert rows[did]["project_id"] == target_pid, f"{did} must carry the target project"
        assert rows[did]["deleted_at"] is None
    # Root re-parented; descendants keep their parents; outsider untouched.
    assert rows[parent]["parent_id"] == target_doc
    assert rows[child]["parent_id"] == parent
    assert rows[ref]["parent_id"] == child
    assert rows[outsider]["project_id"] == pid
    # Reference rows keep their synthetic _ref path.
    assert rows[ref]["path"].startswith("_ref/")


@pytest.mark.asyncio
async def test_move_root_lands_top_of_target_siblings(
    client, test_db, admin_user, project_with_doc,
):
    """The moved root lands at the TOP of the target sibling group (same rule
    as _apply_parent_update): its sort_key sorts before every existing
    sibling's."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, target_idx = await _mk_project(client, token, "Move Target H")
    existing = await _mk_doc(client, target_pid, token, title="Existing", parent_id=target_idx)
    doc = await _mk_doc(client, pid, token, title="Newcomer")

    resp = await _move(client, token, doc, target_pid, parent_id=target_idx)
    assert resp.status_code == 200, resp.text

    rows = await _doc_rows(test_db, [doc, existing])
    assert rows[doc]["sort_key"] < rows[existing]["sort_key"], \
        "moved root must sort FIRST among target siblings"


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


# ─── Denormalized rows + grant cleanup + last-accessed pointers ─────────────


@pytest.mark.asyncio
async def test_move_carries_doc_chunks_shares_chat_sessions_agent_configs_api_keys(
    client, test_db, admin_user, project_with_doc,
):
    from db import create_record

    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, _ = await _mk_project(client, token, "Move Target J")

    doc = await _mk_doc(client, pid, token, title="Carrier")
    chunk_id, share_id, chat_id, cfg_id, key_id = (str(uuid4()) for _ in range(5))
    await create_record("doc_chunks", chunk_id, {
        "document_id": doc, "project_id": pid, "ord": 0, "content": "c",
        "offset_start": 0, "offset_end": 1, "content_version": 1,
        "kind": "document", "embedding": [0.1],
    })
    await create_record("document_shares", share_id, {
        "document_id": doc, "project_id": pid, "scope": "doc", "token": "tok",
    })
    await create_record("chat_sessions", chat_id, {
        "project_id": pid, "document_id": doc, "user_id": "u1",
    })
    await create_record("agent_configs", cfg_id, {
        "project_id": pid, "document_id": doc,
        "config_doc_id": doc, "target_doc_id": doc,
    })
    await create_record("api_keys", key_id, {
        "project_id": pid, "document_id": doc, "user_id": "u1",
        "token_hash": "x", "label": "k", "internal": False,
        "capabilities": ["widget"],
    })

    resp = await _move(client, token, doc, target_pid)
    assert resp.status_code == 200, resp.text

    for table, row_id in (
        ("doc_chunks", chunk_id), ("document_shares", share_id),
        ("chat_sessions", chat_id), ("agent_configs", cfg_id), ("api_keys", key_id),
    ):
        rows = await test_db.query(
            f"SELECT project_id FROM type::record('{table}', $id)", {"id": row_id},
        )
        assert rows and rows[0]["project_id"] == target_pid, \
            f"{table} must carry the target project_id"


@pytest.mark.asyncio
async def test_move_clears_source_last_accessed_pointers(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """Owner column (projects), member row (project_members) and
    user_preferences pointers naming a moved doc are cleared SOURCE-side only,
    and the preferences object's OTHER keys survive the field-level update."""
    from db import create_record

    pid, _, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, _ = regular_user
    target_pid, _ = await _mk_project(client, admin_token, "Move Target L")

    doc = await _mk_doc(client, pid, admin_token, title="PointerDoc")
    other_doc = await _mk_doc(client, pid, admin_token, title="OtherDoc")
    await test_db.query(
        "UPDATE type::record('projects', $pid) SET last_accessed_doc_id = $did, "
        "voice_recording_doc_id = $did",
        {"pid": pid, "did": doc},
    )
    await test_db.query(
        "UPDATE project_members SET last_accessed_doc_id = $did "
        "WHERE project_id = $pid AND user_id = $uid",
        {"pid": pid, "uid": admin_uid, "did": doc},
    )
    # A member row pointing at a NON-moved doc must survive the clear.
    await test_db.query(
        "CREATE project_members CONTENT {"
        "  project_id: $pid, user_id: $uid, access_level: 'readonly',"
        "  last_accessed_doc_id: $other"
        "}",
        {"pid": pid, "uid": user_uid, "other": other_doc},
    )
    await create_record("user_preferences", str(uuid4()), {
        "user_id": admin_uid, "project_id": pid,
        "preferences": {"last_accessed_doc_id": doc, "sidebar_tab": "docs"},
    })

    resp = await _move(client, admin_token, doc, target_pid)
    assert resp.status_code == 200, resp.text

    proj = await test_db.query(
        "SELECT last_accessed_doc_id, voice_recording_doc_id "
        "FROM type::record('projects', $pid)", {"pid": pid},
    )
    assert proj[0]["last_accessed_doc_id"] is None
    # The voice-recording destination names a moved doc → cleared with it, or
    # A's voice notes would keep landing on a doc that now lives in B.
    assert proj[0]["voice_recording_doc_id"] is None

    member = await test_db.query(
        "SELECT last_accessed_doc_id FROM project_members "
        "WHERE project_id = $pid AND user_id = $uid",
        {"pid": pid, "uid": admin_uid},
    )
    assert member[0]["last_accessed_doc_id"] is None

    kept = await test_db.query(
        "SELECT last_accessed_doc_id FROM project_members "
        "WHERE project_id = $pid AND user_id = $uid2",
        {"pid": pid, "uid2": user_uid},
    )
    assert kept[0]["last_accessed_doc_id"] == other_doc, \
        "a pointer naming a NON-moved doc stays"

    prefs = await test_db.query(
        "SELECT preferences FROM user_preferences "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": admin_uid, "pid": pid},
    )
    assert prefs[0]["preferences"].get("last_accessed_doc_id") is None
    assert prefs[0]["preferences"].get("sidebar_tab") == "docs", \
        "field-level update must not clobber sibling preference keys"


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


@pytest.mark.asyncio
async def test_move_emits_out_and_in_events_to_the_right_projects(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, token = admin_user
    target_pid, _ = await _mk_project(client, token, "Move Target N")

    parent = await _mk_doc(client, pid, token, title="EventParent")
    child = await _mk_doc(client, pid, token, title="EventChild", parent_id=parent)
    ref = await _mk_doc(client, pid, token, title="EventRef", parent_id=child, is_reference=True)

    # Subscribe on the bus (no monkeypatch): the command's emits are
    # fire-and-forget tasks, so poll until both arrive.
    import asyncio

    import event_bus
    captured: list[tuple[str, dict]] = []

    async def on_out(**kwargs):
        captured.append(("documents_moved_out", kwargs))

    async def on_in(**kwargs):
        captured.append(("documents_moved_in", kwargs))

    event_bus.on("documents_moved_out", on_out)
    event_bus.on("documents_moved_in", on_in)
    try:
        resp = await _move(client, token, parent, target_pid)
        assert resp.status_code == 200, resp.text
        for _ in range(50):
            if len(captured) >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        event_bus.off("documents_moved_out", on_out)
        event_bus.off("documents_moved_in", on_in)

    outs = [kw for t, kw in captured if t == "documents_moved_out"]
    ins = [kw for t, kw in captured if t == "documents_moved_in"]
    assert len(outs) == 1 and len(ins) == 1, \
        f"exactly one out + one in event, got {captured}"
    out, inn = outs[0], ins[0]
    assert out["project_id"] == pid, "OUT rides the SOURCE project"
    assert set(out["document_ids"]) == {parent, child}
    assert out["reference_ids"] == [ref]
    assert out["target_project_id"] == target_pid
    assert out["target_project_name"] == "Move Target N"
    assert inn["project_id"] == target_pid, "IN rides the TARGET project"
    assert set(inn["document_ids"]) == {parent, child}


@pytest.mark.asyncio
async def test_move_broadcasts_over_project_ws(
    sync_app, client, admin_user, regular_user, project_with_doc,
):
    """End-to-end WS: a source-project subscriber receives documents_moved_out
    with the allowlisted fields; a target-project subscriber receives
    documents_moved_in."""
    pid, _, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    target_pid, _ = await _mk_project(client, admin_token, "Move Target O")
    r = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    doc = await _mk_doc(client, pid, admin_token, title="WsDoc")

    with sync_app.websocket_connect(
        f"/ws/project/{pid}", cookies={"lore_session": user_token},
    ) as src_ws, sync_app.websocket_connect(
        f"/ws/project/{target_pid}", cookies={"lore_session": admin_token},
    ) as tgt_ws:
        json.loads(src_ws.receive_text())  # init
        json.loads(tgt_ws.receive_text())  # init

        resp = await _move(client, admin_token, doc, target_pid)
        assert resp.status_code == 200, resp.text

        src_msg = json.loads(src_ws.receive_text())
        assert src_msg["type"] == "documents_moved_out"
        assert src_msg["document_ids"] == [doc]
        assert src_msg["reference_ids"] == []
        assert src_msg["target_project_id"] == target_pid
        assert src_msg["target_project_name"] == "Move Target O"
        assert "project_id" not in src_msg, "project_id is stripped by the subscriber"

        tgt_msg = json.loads(tgt_ws.receive_text())
        assert tgt_msg["type"] == "documents_moved_in"
        assert tgt_msg["document_ids"] == [doc]
