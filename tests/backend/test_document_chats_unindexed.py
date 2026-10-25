"""Document-level `chats_unindexed`: the subtree hide of chats from chat recall.

A document F flagged `chats_unindexed` hides the chats of F and its whole
subtree from every recall door whose VIEWER document sits outside F's subtree
(the viewer is the calling conversation's root document, or the UI search
route's anchor). Inside sees inside AND outside — one-way, the room is not an
isolation chamber. The flag is read-time and viewer-relative, never copied
onto chats. Digest opts out and keeps reading.
"""
import hashlib
import secrets
from datetime import datetime, timezone

import pytest
from helpers import fake_embedding_provider

from jobs.tasks.chat_index import index_chat_messages

# Keyword axes of the fake embedding space (chat-recall queries and the digest
# query share it); the last axis is a constant so no vector is all-zero.
_AXES = ("ember", "glacier", "harbor", "knee", "sleep", "guitar", "tax")


def _vec(text: str) -> list[float]:
    low = text.lower()
    return [1.0 if a in low else 0.0 for a in _AXES] + [0.01]


@pytest.fixture
def embed(monkeypatch):
    return fake_embedding_provider(monkeypatch, _vec)


# ─── fixtures ────────────────────────────────────────────────────────────────


async def _doc(pid, did, parent=None, **fields):
    from db import create_record

    await create_record("documents", did, {
        "project_id": pid, "parent_id": parent, "title": did, "content": "",
        "path": f"{did}.md", "is_index": False, **fields,
    })


async def _session(db, sid, uid, pid, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid,
                          "title": f"title {sid}", "model": "m", **fields}},
    )


async def _msg(db, mid, chat_id, content, *, role="user", **fields):
    await db.query(
        "CREATE type::record('messages', $id) CONTENT $f",
        {"id": mid, "f": {"chat_id": chat_id, "role": role, "content": content,
                          **fields}},
    )


async def _key(user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": True,
    })
    return token


async def _flag(db, *doc_ids, value=True):
    for did in doc_ids:
        await db.query(
            "UPDATE type::record('documents', $id) SET chats_unindexed = $v",
            {"id": did, "v": value},
        )


@pytest.fixture
async def world(client, test_db, admin_user, project_with_doc, embed):
    """Docs F (flagged) > C, and O (F's sibling outside the room); one chat on
    each of F, C, O on a shared topic; one anchor session per document; an
    internal agent key for the owner."""
    pid, _, a = project_with_doc
    await _doc(pid, "F")
    await _doc(pid, "C", "F")
    await _doc(pid, "O")
    await _flag(test_db, "F")
    for sid, did in (("sF", "F"), ("sC", "C"), ("sO", "O"),
                     ("anchorF", "F"), ("anchorC", "C"), ("anchorO", "O")):
        await _session(test_db, sid, a, pid, document_id=did)
    for sid in ("sF", "sC", "sO"):
        await _msg(test_db, f"{sid}m", sid, "ember glacier room log")
    await index_chat_messages({}, a, pid)
    return {"pid": pid, "a": a, "token": admin_user[1], "ka": await _key(a, pid)}


# ─── helpers ─────────────────────────────────────────────────────────────────


async def _tool(client, token, path, body=None, session_id=None, status=200) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["X-Agent-Session-Id"] = session_id
    resp = await client.post(f"/api/tool/{path}", json=body or {}, headers=headers)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _search(client, token, query, session_id=None) -> dict:
    return await _tool(client, token, "search_my_chats", {"query": query}, session_id)


def _hit_chats(result: dict) -> list[str]:
    return [h["chat_id"] for h in result["hits"]]


def _chat_ids(listed: dict) -> list[str]:
    return [r["chat_id"] for r in listed["rows"]]


# ─── the viewer rule ─────────────────────────────────────────────────────────


async def test_inside_viewer_sees_the_room_and_the_rest_of_the_project(client, world):
    """Inside sees inside, upward to the flagged root included, AND outside —
    the hide is one-way (the room is not an isolation chamber)."""
    result = await _search(client, world["ka"], "ember glacier", "anchorC")
    assert set(_hit_chats(result)) == {"sF", "sC", "sO"}


async def test_outside_viewer_sees_nothing_until_unflagged(client, test_db, world):
    result = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert _hit_chats(result) == ["sO"]
    await _flag(test_db, "F", value=False)
    result = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert set(_hit_chats(result)) == {"sF", "sC", "sO"}


async def test_a_viewer_without_a_document_is_outside_every_room(client, world):
    """No calling conversation (agent key alone) and no UI anchor both mean
    V = None: outside every flagged subtree, so the hide fails safe."""
    result = await _search(client, world["ka"], "ember glacier")
    assert _hit_chats(result) == ["sO"]
    resp = await client.get(
        f"/api/projects/{world['pid']}/chat-search",
        params={"q": "ember glacier"}, cookies={"lore_session": world["token"]},
    )
    assert resp.status_code == 200, resp.text
    assert [h["chat_id"] for h in resp.json()["hits"]] == ["sO"]


async def test_nested_flags_exclude_the_inner_room_from_its_owner(client, test_db, world):
    await _flag(test_db, "C")
    from_f = await _search(client, world["ka"], "ember glacier", "anchorF")
    assert set(_hit_chats(from_f)) == {"sF", "sO"}
    from_c = await _search(client, world["ka"], "ember glacier", "anchorC")
    assert set(_hit_chats(from_c)) == {"sF", "sC", "sO"}
    from_o = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert _hit_chats(from_o) == ["sO"]


async def test_sibling_rooms_do_not_see_each_other(client, test_db, world):
    await _doc(world["pid"], "C2", "F")
    await _session(test_db, "sC2", world["a"], world["pid"], document_id="C2")
    await _msg(test_db, "sC2m", "sC2", "ember glacier room log")
    await _flag(test_db, "C", "C2")
    await index_chat_messages({}, world["a"], world["pid"])
    from_c = await _search(client, world["ka"], "ember glacier", "anchorC")
    assert set(_hit_chats(from_c)) == {"sF", "sC", "sO"}


async def test_an_archived_room_still_hides(client, test_db, world):
    """`archived` is not a liveness axis for the hide — archiving a room must
    not open it."""
    await test_db.query(
        "UPDATE type::record('documents', 'F') SET archived = true")
    result = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert _hit_chats(result) == ["sO"]


async def test_lift_deleting_the_room_opens_what_is_left(client, world):
    """Lift mode takes the flag with the room (user ruling, same model as
    inherited restrictions): the lifted children's chats reopen project-wide;
    the room's own chats die with it."""
    resp = await client.delete(
        "/api/documents/F", params={"delete_children": False},
        cookies={"lore_session": world["token"]},
    )
    assert resp.status_code == 200, resp.text
    result = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert set(_hit_chats(result)) == {"sC", "sO"}


# ─── the five doors ──────────────────────────────────────────────────────────


async def test_every_door_hides_from_outside_and_serves_inside(client, world):
    pid, ka, token = world["pid"], world["ka"], world["token"]
    # search
    assert "sC" not in _hit_chats(await _search(client, ka, "ember glacier", "anchorO"))
    assert "sC" in _hit_chats(await _search(client, ka, "ember glacier", "anchorC"))
    # window read
    await _tool(client, ka, "search_my_chats",
                {"message_id": "sCm", "offset": 0}, "anchorO", status=404)
    win = await _tool(client, ka, "search_my_chats",
                      {"message_id": "sCm", "offset": 0}, "anchorC")
    assert win["chat_id"] == "sC"
    # list
    assert "sC" not in _chat_ids(await _tool(client, ka, "list_my_chats",
                                             session_id="anchorO"))
    assert "sC" in _chat_ids(await _tool(client, ka, "list_my_chats",
                                         session_id="anchorC"))
    # read
    await _tool(client, ka, "read_my_chat", {"chat_id": "sC"}, "anchorO", status=404)
    read = await _tool(client, ka, "read_my_chat", {"chat_id": "sC"}, "anchorC")
    assert read["chat_id"] == "sC"
    # UI search route
    out = await client.get(
        f"/api/projects/{pid}/chat-search",
        params={"q": "ember glacier", "anchor_document_id": "O"},
        cookies={"lore_session": token},
    )
    assert out.status_code == 200, out.text
    assert "sC" not in [h["chat_id"] for h in out.json()["hits"]]
    inside = await client.get(
        f"/api/projects/{pid}/chat-search",
        params={"q": "ember glacier", "anchor_document_id": "C"},
        cookies={"lore_session": token},
    )
    assert inside.status_code == 200, inside.text
    assert "sC" in [h["chat_id"] for h in inside.json()["hits"]]


async def test_a_room_deeper_than_any_walk_cap_hides_its_bottom(test_db, world):
    """The subtree walk has no depth cap: a cap would fail OPEN and leak the
    chats under it."""
    from chat_sessions.recall_search import hidden_doc_ids

    parent = "C"
    for i in range(60):
        await _doc(world["pid"], f"deep{i}", parent)
        parent = f"deep{i}"
    hidden = await hidden_doc_ids(test_db, world["pid"], "O")
    assert {"F", "C", "deep0", "deep59"} <= hidden
    assert "O" not in hidden


async def test_a_cyclic_parent_chain_still_ends_the_walk(test_db, world):
    from chat_sessions.recall_search import hidden_doc_ids

    await _doc(world["pid"], "X", "Y")
    await _doc(world["pid"], "Y", "X")
    await _flag(test_db, "X")
    hidden = await hidden_doc_ids(test_db, world["pid"], None)
    assert {"X", "Y", "F", "C"} <= hidden
    assert "O" not in hidden


# ─── read-time: moves change visibility with no write to any chat ───────────


async def test_a_chat_moved_out_of_the_room_loses_the_hide(client, test_db, world):
    """The user's rule: a chat moved to another document loses the hide. The
    viewer left inside still sees it — one-way: hidden(C) is empty (C is
    inside F), so inside-sees-outside applies."""
    resp = await client.patch(
        "/api/chat/sessions/sC", json={"document_id": "O"},
        cookies={"lore_session": world["token"]},
    )
    assert resp.status_code == 200, resp.text
    from_o = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert set(_hit_chats(from_o)) == {"sC", "sO"}
    from_c = await _search(client, world["ka"], "ember glacier", "anchorC")
    assert set(_hit_chats(from_c)) == {"sF", "sC", "sO"}
    row = (await test_db.query(
        "SELECT unindexed FROM type::record('chat_sessions', $id)",
        {"id": "sC"},
    ))[0]
    assert row["unindexed"] is False


async def test_a_document_moved_out_of_the_room_reopens_its_chats(client, world):
    resp = await client.patch(
        "/api/documents/C", json={"parent_id": "O"},
        cookies={"lore_session": world["token"]},
    )
    assert resp.status_code == 200, resp.text
    result = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert set(_hit_chats(result)) == {"sC", "sO"}


# ─── honest incompleteness ───────────────────────────────────────────────────


async def test_coverage_counts_only_what_the_viewer_can_see(client, test_db, world):
    await _msg(test_db, "sOpend", "sO", "a pending ember glacier", role="assistant")
    from_o = await _search(client, world["ka"], "ember glacier", "anchorO")
    assert from_o["indexed"] == "1 of 2"
    from_c = await _search(client, world["ka"], "ember glacier", "anchorC")
    assert from_c["indexed"] == "3 of 4"


# ─── digest opts out ─────────────────────────────────────────────────────────


async def test_digest_material_still_reads_the_room(client, test_db, collab_project, embed):
    """Digest is not affected (user ruling): the anchored path over the flagged
    theme and the related-turns path over a hidden non-theme doc both keep
    reading. Driven at material.collect — the one seam that owns both paths."""
    from chat_sessions.recall_search import Window
    from pipeline.digest.material import collect

    pid, _d, _admin_token, _ut, admin_uid, _uu = collab_project
    await _doc(pid, "dTheme")
    await _doc(pid, "dC", "dTheme")
    await _flag(test_db, "dTheme")
    await _session(test_db, "anch", admin_uid, pid, document_id="dTheme")
    await _msg(test_db, "anch0", "anch", "my knee hurts after running")
    await _session(test_db, "rel", admin_uid, pid, document_id="dC")
    await _msg(test_db, "rel0", "rel", "slept badly, sleep is broken")
    await index_chat_messages({}, admin_uid, pid)

    # A real `until`, like every production caller (window_end): `created_at
    # <= NONE` would drop every row.
    material = await collect(
        pid, "dTheme", (admin_uid,), "knee sleep",
        Window(until=datetime.now(timezone.utc)))

    assert any("my knee hurts after running" in line for line in material.anchored)
    assert any("sleep is broken" in line for line in material.related)


# ─── write path + tree payload ───────────────────────────────────────────────


async def _grant(pid, uid, level):
    from db import create_record

    await create_record("project_members", f"test-pm-{uid}", {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


async def _set_level(test_db, uid, level):
    await test_db.query(
        "UPDATE type::record('project_members', $id) SET access_level = $lv",
        {"id": f"test-pm-{uid}", "lv": level},
    )


async def test_patch_gate_and_tree_row(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """Editor rights (require_document_full) gate the flag; the PATCH response
    and the GET /api/projects/{id} tree row both carry it."""
    pid, doc_id, _ = project_with_doc
    uid, token = regular_user
    await _grant(pid, uid, "readonly")
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"chats_unindexed": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403, resp.text
    await _set_level(test_db, uid, "commentator")
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"chats_unindexed": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403, resp.text
    await _set_level(test_db, uid, "full")
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"chats_unindexed": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["chats_unindexed"] is True
    tree = await client.get(f"/api/projects/{pid}",
                            cookies={"lore_session": admin_user[1]})
    assert tree.status_code == 200, tree.text
    row = next(d for d in tree.json()["documents"]
               if d["document_id"] == doc_id)
    assert row["chats_unindexed"] is True
