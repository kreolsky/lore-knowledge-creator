"""Owner-only public-share coverage on the document tree (plan "orange-tree-icon").

GET /api/projects/{id} annotates each doc with `public_share: true` when it is
covered by a live anonymous public link (document_shares) — but ONLY for the
project owner. The coverage mirrors the anonymous resolve funnel
(public_share.py `_find_share_row`): doc-scope → own doc only; subtree-scope →
root + all descendants; a doc-scope ancestor propagates nothing; the
agent-config subtree is excluded even with a pre-guard row. Derived IN-PROCESS
from the already-loaded parent_id map, so the document_shares query count is
constant (1) regardless of tree size.
"""
import pytest
from share_guard import system_root_id

import db.pool as db_pool
from db import create_record, get_db

# ─── helpers (mirror test_public_share.py's minimal shapes) ─────────────────


async def _make_doc(client, token, project_id, title, *, parent_id=None):
    payload: dict = {"project_id": project_id, "title": title, "content": ""}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=payload, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _mint_share(client, token, project_id, document_id, scope):
    resp = await client.post(
        f"/api/projects/{project_id}/documents/{document_id}/shares",
        json={"scope": scope}, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_id(documents):
    return {d["document_id"]: d for d in documents}


async def _tree(client, token, pid):
    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return _by_id(resp.json()["documents"])


# ─── coverage semantics (mirror public_share.py `_find_share_row`) ──────────


@pytest.mark.asyncio
async def test_doc_scope_share_marks_only_that_doc(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    root = await _make_doc(client, token, pid, "Root")
    child = await _make_doc(client, token, pid, "Child", parent_id=root)
    await _mint_share(client, token, pid, root, "doc")

    docs = await _tree(client, token, pid)

    assert docs[root].get("public_share") is True
    assert "public_share" not in docs[child]
    assert "public_share" not in docs[idx_id]


@pytest.mark.asyncio
async def test_subtree_scope_marks_root_and_all_descendants(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    root = await _make_doc(client, token, pid, "Root")
    child = await _make_doc(client, token, pid, "Child", parent_id=root)
    grandchild = await _make_doc(client, token, pid, "Grandchild", parent_id=child)
    outside = await _make_doc(client, token, pid, "Outside")  # no parent: outside the subtree
    await _mint_share(client, token, pid, root, "subtree")

    docs = await _tree(client, token, pid)

    assert docs[root].get("public_share") is True
    assert docs[child].get("public_share") is True
    assert docs[grandchild].get("public_share") is True
    assert "public_share" not in docs[outside]


@pytest.mark.asyncio
async def test_doc_scope_on_parent_does_not_cover_children(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    parent = await _make_doc(client, token, pid, "Parent")
    child = await _make_doc(client, token, pid, "Child", parent_id=parent)
    await _mint_share(client, token, pid, parent, "doc")

    docs = await _tree(client, token, pid)

    assert docs[parent].get("public_share") is True
    assert "public_share" not in docs[child]


@pytest.mark.asyncio
async def test_revoked_share_marks_nobody(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    root = await _make_doc(client, token, pid, "Root")
    child = await _make_doc(client, token, pid, "Child", parent_id=root)
    share = await _mint_share(client, token, pid, root, "subtree")
    resp = await client.delete(
        f"/api/shares/{share['share_id']}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text

    docs = await _tree(client, token, pid)

    assert "public_share" not in docs[root]
    assert "public_share" not in docs[child]


# ─── owner-only visibility (Decision 3) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_non_owner_full_member_sees_no_public_share(
    client, admin_user, regular_user, project_with_doc,
):
    """INVARIANT(security): the orange marker is owner-only — mint/revoke is
    owner-gated, so a non-owner cannot act on the information. A `full` member
    keeps today's payload: `public_share` is absent on every document."""
    pid, _, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    root = await _make_doc(client, admin_token, pid, "Root")
    await _make_doc(client, admin_token, pid, "Child", parent_id=root)
    await _mint_share(client, admin_token, pid, root, "subtree")
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )

    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": user_token})
    assert resp.status_code == 200
    for d in resp.json()["documents"]:
        assert "public_share" not in d, d


# ─── agent-config subtree exclusion (ancestry, not flag) ────────────────────


@pytest.mark.asyncio
async def test_share_inside_agent_config_subtree_not_marked(client, admin_user, project_with_doc):
    """A pre-guard document_shares row on a doc inside the agent-config subtree
    is NOT marked — the resolve funnel 404s it by ancestry, so the tree must not
    claim it is public. The row is inserted directly (the mint chokepoint would
    refuse it); the system root uses the deterministic id share_guard relies on.
    """
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    sys_root = system_root_id(pid)  # "sys-system_root-<pid>"
    await create_record("documents", sys_root, {
        "project_id": pid, "parent_id": None, "title": "system_root",
        "content": "", "path": "system_root", "is_system": True,
    })
    leaf = "sys-root-leaf-001"
    await create_record("documents", leaf, {
        "project_id": pid, "parent_id": sys_root, "title": "leaf",
        "content": "", "path": "leaf",
    })
    # Pre-guard subtree share on the leaf (bypasses the mint ancestry guard).
    await create_record("document_shares", "pre-guard-share-1", {
        "project_id": pid, "document_id": leaf, "scope": "subtree",
        "token": "lore_pre_guard_row", "created_by": admin_uid,
    })

    docs = await _tree(client, token, pid)

    assert "public_share" not in docs.get(leaf, {}), "agent-config leaf leaked as public"
    assert "public_share" not in docs.get(sys_root, {}), "system root leaked as public"


# ─── perf: O(1) document_shares query regardless of tree size ───────────────


@pytest.mark.asyncio
async def test_coverage_uses_a_single_document_shares_query(client, admin_user, project_with_doc):
    """INVARIANT(perf): coverage is derived IN-PROCESS from the already-loaded
    parent_id map — never a per-doc resolve_share call (N round trips on every
    tree load). The GET issues exactly ONE document_shares query for a many-doc
    tree; a per-doc regression would scale with document count.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    root = await _make_doc(client, token, pid, "Root")
    for i in range(30):
        await _make_doc(client, token, pid, f"Child {i}", parent_id=root)
    await _mint_share(client, token, pid, root, "subtree")

    # Class-level patch on the timed proxy: the single chokepoint for every
    # db.query() regardless of which event loop the ASGI handler runs on (the
    # route re-fetches get_db() internally, so an instance-level patch could
    # miss it across the loop boundary).
    counts = {"shares": 0}
    orig = db_pool._TimedDB.query

    async def counting_query(self, sql, params=None, *, site="unspecified"):
        if "document_shares" in (sql or ""):
            counts["shares"] += 1
        return await orig(self, sql, params, site=site)

    db_pool._TimedDB.query = counting_query
    try:
        resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})
    finally:
        db_pool._TimedDB.query = orig
    assert resp.status_code == 200

    # 31 covered docs, exactly one document_shares query → coverage is O(1).
    assert counts["shares"] == 1, counts


# ─── F1: deleted subtree-share root orphans its children ────────────────────


@pytest.mark.asyncio
async def test_subtree_share_on_deleted_root_does_not_mark_children(
    client, admin_user, project_with_doc,
):
    """F1: deleting a subtree-share root orphans its children. The resolve
    funnel's get_ancestor_ids BREAKS at a soft-deleted ancestor, so those
    children 404 anonymously — the tree indicator must agree and NOT paint them
    orange. Doc deletion does not cascade to children (cascade.py), so the
    orphan state is reachable.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    root = await _make_doc(client, token, pid, "Root")
    child = await _make_doc(client, token, pid, "Child", parent_id=root)
    await _mint_share(client, token, pid, root, "subtree")

    # Soft-delete the share root; the child stays alive (orphaned).
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": root},
    )

    docs = await _tree(client, token, pid)

    # Root is deleted → absent from the tree payload entirely.
    assert root not in docs
    # Child is alive but its parent chain breaks at the deleted root, so the
    # resolve funnel would 404 it → it must NOT be marked public.
    assert "public_share" not in docs.get(child, {}), (
        "orphaned child of a deleted subtree-share root leaked as public"
    )
