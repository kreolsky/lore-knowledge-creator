"""Integration tests for project routes."""

import pytest
from helpers import make_token


@pytest.mark.asyncio
async def test_patch_description_set_and_clear(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"description": "First lore"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "First lore"
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"description": None},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Cleared reads back empty: Surreal drops NONE fields from the row, so the
    # key is absent from the response (contract: absent or None, never stale).
    assert resp.json().get("description") is None


@pytest.mark.asyncio
async def test_readonly_member_cannot_patch_description(client, regular_user, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    user_uid, user_token = regular_user
    _, admin_token = admin_user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"description": "Hacked"},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_access_control_readonly(client, regular_user, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    user_uid, user_token = regular_user
    _, admin_token = admin_user
    # Grant readonly access via admin API
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    # Can read
    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": user_token})
    assert resp.status_code == 200
    # Can't write
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"name": "Hacked"},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_projects_requires_admin(client, regular_user, test_db):
    """GET /api/admin/projects rejects non-admin users."""
    _, user_token = regular_user
    resp = await client.get("/api/admin/projects", cookies={"lore_session": user_token})
    assert resp.status_code == 403


# ─── System prompts doc ─────────────────────────────────────────────────────


# Plan "unify-agent-config": the three system_prompts_doc_id tests below were
# removed — the project prompts-folder field is gone (personas now live under
# `.lore/system`).


@pytest.mark.asyncio
async def test_deleted_project_documents_unreachable(client, admin_user, test_db):
    """After a project delete, the owner gets 404 on the project AND on every
    document in it — document access IS project access, and a soft-deleted
    project row reads as absent."""
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/projects", json={"name": "Unreachable"}, cookies=cookies)
    pid = resp.json()["project_id"]
    resp = await client.post("/api/documents", json={"project_id": pid, "title": "Doc"}, cookies=cookies)
    doc_id = resp.json()["document_id"]

    # False-green check: both reads are live BEFORE the delete.
    assert (await client.get(f"/api/projects/{pid}", cookies=cookies)).status_code == 200
    assert (await client.get(f"/api/documents/{doc_id}", cookies=cookies)).status_code == 200

    resp = await client.delete(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 200

    # require_document_read raises 404 for None access (access.py).
    resp = await client.get(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 404
    resp = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
    assert resp.status_code == 404


# ─── last-accessed document tracking (owner + per-user independence) ─────────


async def _make_project_with_two_docs(client, token, name="LastDoc Project"):
    """Create a project (owner=token user) with the index doc + one extra doc.

    Returns (project_id, index_doc_id, extra_doc_id).
    """
    resp = await client.post(
        "/api/projects",
        json={"name": name},
        cookies={"lore_session": token},
    )
    pid = resp.json()["project_id"]
    idx_id = resp.json()["index_doc_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Doc B", "content": "BBB"},
        cookies={"lore_session": token},
    )
    extra_id = resp.json()["document_id"]
    return pid, idx_id, extra_id


async def _make_owned_project_with_full_member(test_db, owner_uid, member_uid):
    """Create a project owned by `owner_uid` (non-admin) with `member_uid` as a
    full-access member. Returns project_id. Used by the audiences write-gate tests."""
    from password import hash_secret

    from db import create_record
    pid = "test-project-vis-001"
    idx_id = "test-index-doc-vis-001"
    for tbl, rid in (("projects", pid), ("documents", idx_id),
                     ("project_members", "test-pm-vis-owner"),
                     ("project_members", "test-pm-vis-member"),
                     ("users", owner_uid), ("users", member_uid)):
        await test_db.query("DELETE type::record($tbl, $id)", {"tbl": tbl, "id": rid})
    for uid, email in ((owner_uid, "owner@vis.com"), (member_uid, "member@vis.com")):
        await create_record("users", uid, {
            "name": uid, "email": email, "password_hash": hash_secret("x"),
            "role": "user", "user_facts": "",
        })
    await create_record("projects", pid, {
        "name": "Vis Project", "status": "active", "project_context": "",
        "index_doc_id": idx_id, "owner_id": owner_uid,
    })
    await create_record("documents", idx_id, {
        "project_id": pid, "parent_id": None, "title": "project_context.md",
        "content": "", "path": "project_context.md", "is_index": True,
    })
    await create_record("project_members", "test-pm-vis-owner", {
        "project_id": pid, "user_id": owner_uid, "access_level": "full",
    })
    await create_record("project_members", "test-pm-vis-member", {
        "project_id": pid, "user_id": member_uid, "access_level": "full",
    })
    return pid


@pytest.mark.asyncio
async def test_owner_cannot_add_public_row(client, test_db):
    """The 'public' subject is admin-only: a non-admin
    owner's add is refused and the project stays private."""
    owner_token = make_token("vis-owner-001", "owner", "user", "owner@vis.com")
    pid = await _make_owned_project_with_full_member(test_db, "vis-owner-001", "vis-member-001")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"audiences": [{"subject": "public", "access_level": "readonly"}]},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 403
    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": owner_token})
    assert resp.status_code == 200
    assert resp.json()["project"]["audiences"] == []


@pytest.mark.asyncio
async def test_full_not_owner_cannot_write_audiences(client, test_db):
    """A full-access non-owner member gets 403 when audiences is in the PATCH body."""
    member_token = make_token("vis-member-001", "member", "user", "member@vis.com")
    pid = await _make_owned_project_with_full_member(test_db, "vis-owner-001", "vis-member-001")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": []},
        cookies={"lore_session": member_token},
    )
    assert resp.status_code == 403


# ─── Dangling last-accessed pointer (a soft-deleted doc) ─────────────────────
# A soft-deleted document leaves `last_accessed_doc_id` naming it: the Dashboard
# navigates straight into GET /api/documents/open/<id>, which 404s (fetch_one
# drops soft-deleted rows) and bounces the user back to the project list.


@pytest.mark.asyncio
async def test_owner_last_accessed_dropped_when_doc_soft_deleted(client, admin_user):
    """Owner's pointer to a DELETED doc is dropped from the project list, so the
    client falls back to index_doc_id instead of navigating into a 404."""
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})
    resp = await client.delete(f"/api/documents/{extra_id}", cookies={"lore_session": token})
    assert resp.status_code == 200

    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    proj = next(p for p in projects if p["project_id"] == pid)
    assert proj.get("last_accessed_doc_id") is None


@pytest.mark.asyncio
async def test_owner_last_accessed_dropped_single_project(client, admin_user):
    """Same for GET /api/projects/{id} — the endpoint the Dashboard actually reads
    right before navigating (Dashboard.handleProjectClick)."""
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})
    await client.delete(f"/api/documents/{extra_id}", cookies={"lore_session": token})

    data = (await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})).json()
    assert data["project"].get("last_accessed_doc_id") is None
