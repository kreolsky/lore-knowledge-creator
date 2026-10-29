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
