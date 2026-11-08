"""Typed-edges walls — /edges and the get_links agent tool (plan typed-edges, step 1).

The plan's owed TEST-WHY set: a peer outside the reading document's project is
skipped in BOTH directions (doc_mentions edges survive cross-project subtree
moves), another member's private chat is never revealed by its context, and a
subtree-scoped agent key gets no peer outside its root. The kind/shape behavior
(mention vs transclusion, NONE→mention coalesce, pair-based flush detection) is
pinned inside the existing flush test (test_flush_perf).
"""

import hashlib
import secrets

import pytest


async def _mk_project(client, token, name):
    r = await client.post("/api/projects", json={"name": name}, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["project_id"], body["index_doc_id"]


async def _mk_doc(client, pid, token, *, title, parent_id=None, content=""):
    body = {"project_id": pid, "title": title, "parent_id": parent_id, "content": content}
    r = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _patch_content(client, token, doc_id, content):
    r = await client.patch(
        f"/api/documents/{doc_id}", json={"content": content},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text


async def _add_member(client, admin_token, pid, uid, level="full"):
    r = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": uid, "access_level": level},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text


async def _edges(client, token, doc_id, **params):
    r = await client.get(
        f"/api/documents/{doc_id}/edges", params=params,
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["edges"]


# TEST-WHY: refused-principal — a B-only member (refused on project A, proven by the direct read) must not learn foreign peers through /edges in either direction: doc_mentions edges survive cross-project subtree moves, so without the wall titles/timestamps of unreadable documents leak.
@pytest.mark.asyncio
async def test_edges_project_wall_hides_foreign_peers_both_directions(client, admin_user, regular_user):
    """A1 links to D and D links to A2; D's subtree moves to project B. The
    B-only member's /edges on D hide both the foreign SOURCE (in) and the
    foreign TARGET (out — skipped, never marked broken); same-project peers
    stay."""
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user

    pid_a, idx_a = await _mk_project(client, admin_token, "Edges Wall A")
    pid_b, _ = await _mk_project(client, admin_token, "Edges Wall B")
    await _add_member(client, admin_token, pid_b, user_uid, level="readonly")

    a1 = await _mk_doc(client, pid_a, admin_token, title="A1 secret source", parent_id=idx_a)
    a2 = await _mk_doc(client, pid_a, admin_token, title="A2 secret target", parent_id=idx_a)
    d = await _mk_doc(client, pid_a, admin_token, title="Movable D", parent_id=idx_a)
    await _patch_content(client, admin_token, a1, f"see [D](doc:{d})")
    await _patch_content(client, admin_token, d, f"see [A2](doc:{a2})")

    r = await client.post(
        f"/api/documents/{d}/move",
        json={"target_project_id": pid_b},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    b1 = await _mk_doc(client, pid_b, admin_token, title="B1 visible")
    await _patch_content(client, admin_token, b1, f"see [D](doc:{d})")

    # The principal really is refused on A: a direct read of A1 is denied.
    r = await client.get(f"/api/documents/{a1}", cookies={"lore_session": user_token})
    assert r.status_code in (403, 404), "the B-only member must not read A-docs directly"

    incoming = await _edges(client, user_token, d, direction="in")
    incoming_ids = {e["document_id"] for e in incoming}
    assert a1 not in incoming_ids, "a foreign SOURCE must not appear on the moved doc"
    assert b1 in incoming_ids, "a same-project source must stay"

    outgoing = await _edges(client, user_token, d, direction="out")
    assert all(e["document_id"] != a2 for e in outgoing), \
        "a foreign TARGET must not appear on the moved doc"
    assert not any(e["broken"] for e in outgoing), \
        "a foreign target is SKIPPED by the wall, never reported as broken"


# TEST-WHY: refused-principal — chat-context must serve only the viewer's OWN chats: the other member's chat is private (its transcript read is refused 404 to the admin), so revealing it through the graph would be a privacy hole the direct read refuses.
@pytest.mark.asyncio
async def test_edges_chat_context_shows_only_own_chats(client, admin_user, regular_user):
    """Two members each anchor a chat on the same document via context_ids.
    Each member's /edges?kinds=chat-context lists exactly their own chat; the
    default kind set never includes chat-context."""
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user

    pid, idx = await _mk_project(client, admin_token, "Edges Chats")
    await _add_member(client, admin_token, pid, user_uid)
    d = await _mk_doc(client, pid, admin_token, title="Anchored doc", parent_id=idx)

    async def _context_chat(token):
        r = await client.post(
            "/api/chat/sessions",
            json={"project_id": pid, "document_id": d},
            cookies={"lore_session": token},
        )
        assert r.status_code == 201, r.text
        sid = r.json()["session_id"]
        r = await client.patch(
            f"/api/chat/sessions/{sid}",
            json={"context_ids": [d]},
            cookies={"lore_session": token},
        )
        assert r.status_code == 200, r.text
        return sid

    sid_other = await _context_chat(user_token)
    sid_own = await _context_chat(admin_token)

    # The other member's chat really is private: the admin's direct read is refused.
    r = await client.get(
        f"/api/chat/sessions/{sid_other}/messages", cookies={"lore_session": admin_token},
    )
    assert r.status_code in (403, 404), "an AI chat is owner-only on the direct read"

    own = await _edges(client, admin_token, d, kinds="chat-context")
    assert [e["session_id"] for e in own if e["kind"] == "chat-context"] == [sid_own], \
        "the viewer sees exactly their own chat under chat-context"

    theirs = await _edges(client, user_token, d, kinds="chat-context")
    assert [e["session_id"] for e in theirs if e["kind"] == "chat-context"] == [sid_other]

    default_kinds = {e["kind"] for e in await _edges(client, admin_token, d)}
    assert "chat-context" not in default_kinds, \
        "chat-context is opt-in — never in the default kind set"


# TEST-WHY: refused-principal — a subtree-scoped agent key reading get_links gets no document peer outside its root (dropped), and the anchor itself outside the scope is a 403.
@pytest.mark.asyncio
async def test_get_links_scoped_key_drops_peers_outside_root(client, test_db, admin_user):
    """A key scoped to subtree R: inner (child of R) links to `outer` and
    `outer` links back. get_links on inner serves NO peer outside the root in
    either direction (the same edges ARE there for a full member), and
    get_links on `outer` itself is a 403 — outside the key's scope."""
    from db import create_record

    admin_uid, admin_token = admin_user
    pid, idx = await _mk_project(client, admin_token, "Edges Scope")
    root = await _mk_doc(client, pid, admin_token, title="Scope root", parent_id=idx)
    inner = await _mk_doc(client, pid, admin_token, title="Inner", parent_id=root)
    outer = await _mk_doc(client, pid, admin_token, title="Outer", parent_id=idx)
    await _patch_content(client, admin_token, inner, f"see [outer](doc:{outer})")
    await _patch_content(client, admin_token, outer, f"see [inner](doc:{inner})")

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"edges-key-{secrets.token_hex(4)}", {
        "user_id": admin_uid, "project_id": pid, "document_id": root,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"],
    })

    async def _get_links(doc_id, **body):
        return await client.post(
            "/api/tool/get_links",
            json={"document_id": doc_id, **body},
            headers={"Authorization": f"Bearer {token}"},
        )

    r = await _get_links(inner, direction="both")
    assert r.status_code == 200, r.text
    scoped_ids = {e["document_id"] for e in r.json()["edges"]}
    assert not scoped_ids, \
        "every peer of `inner` lives outside the key's root — all must be dropped"

    # The same graph DOES hold the outer edges — a full member sees them.
    full = await _edges(client, admin_token, inner)
    assert outer in {e["document_id"] for e in full}, \
        "the dropped peer exists (the drop is the scope ceiling, not an empty graph)"

    r = await _get_links(outer)
    assert r.status_code == 403, "an anchor outside the key's subtree must be refused"


# TEST-WHY: cross-process-contract — deleting a linked document must keep its incoming doc_mentions edge in SurrealDB so the source's /edges read reports the target as broken instead of silently dropping the row.
@pytest.mark.asyncio
async def test_deleted_target_stays_on_source_as_broken(client, admin_user):
    _, token = admin_user
    pid, idx = await _mk_project(client, token, "Edges Broken")
    a = await _mk_doc(client, pid, token, title="Source A", parent_id=idx)
    b = await _mk_doc(client, pid, token, title="Target B", parent_id=idx)
    await _patch_content(client, token, a, f"see [B](doc:{b})")
    assert any(e["document_id"] == b and not e["broken"] for e in await _edges(client, token, a, direction="out"))

    r = await client.delete(f"/api/documents/{b}", cookies={"lore_session": token})
    assert r.status_code == 200, r.text

    rows = [e for e in await _edges(client, token, a, direction="out") if e["document_id"] == b]
    assert len(rows) == 1, "the deleted target must stay on the source's outgoing list"
    assert rows[0]["broken"] is True
