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


# ─── Per-section scope cut (dashboard own/shared pagination) ─────────────────


async def _create_project(client, token, name):
    resp = await client.post("/api/projects", json={"name": name}, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["project_id"]


async def _add_member(client, admin_token, pid, uid, access_level="readonly"):
    resp = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": uid, "access_level": access_level},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text


async def _set_public_audience(test_db, pid):
    await test_db.query(
        "UPDATE type::record('projects', $id) SET audiences = [{subject: 'public', access_level: 'readonly'}]",
        {"id": pid},
    )


# TEST-WHY: cross-process-contract — the shared cut over real HTTP must surface another user's member- and audience-shared projects and never the caller's own rows, whose own audience rows the caller's subjects match.
@pytest.mark.asyncio
async def test_scope_shared_member_and_audience_without_own(client, regular_user, admin_user, test_db):
    """scope=shared returns exactly the two share shapes: the admin project the
    caller is a member of and the admin project shared via a 'public' audience
    row. NEVER returned: the caller's own project, the caller's own
    public-audience project (the trap — the caller's subjects match 'public', so
    without `owner_id != $uid` it would leak into shared), and the admin's
    private project."""
    uid, token = regular_user
    _, admin_token = admin_user

    member_pid = await _create_project(client, admin_token, "Admin member-shared")
    await _add_member(client, admin_token, member_pid, uid)
    audience_pid = await _create_project(client, admin_token, "Admin audience-shared")
    await _set_public_audience(test_db, audience_pid)
    private_pid = await _create_project(client, admin_token, "Admin private")
    own_pid = await _create_project(client, token, "Mine plain")
    own_public_pid = await _create_project(client, token, "Mine public")
    await _set_public_audience(test_db, own_public_pid)

    resp = await client.get("/api/projects?scope=shared", cookies={"lore_session": token})
    assert resp.status_code == 200
    rows = resp.json()
    ids = {p["project_id"] for p in rows}
    assert member_pid in ids
    assert audience_pid in ids
    assert own_pid not in ids
    assert own_public_pid not in ids
    assert private_pid not in ids
    assert all(p["owner_id"] != uid for p in rows)


# TEST-WHY: cross-process-contract — a zero-subjects caller (soft-deleted mid-session, auth served from the 60s token-version cache) gets 200 with member rows only, never an audience leak of foreign projects.
@pytest.mark.asyncio
async def test_scope_shared_zero_subjects_no_audience_leak(client, regular_user, admin_user, test_db):
    """The empty-list trap on the shared cut: user_subjects → [] must OMIT the
    audience clause, so the caller sees only their member row — the foreign
    private project AND the public-audience one stay invisible (a
    CONTAINSANY-over-[] degeneration would leak them). The zero-subjects caller
    reaches HTTP for real only inside auth's 60s token-version cache window: the
    first request primes the cache while the user is live, then the row is
    soft-deleted (fetch_one drops it → a cache-cold request would 401)."""
    uid, token = regular_user
    _, admin_token = admin_user

    member_pid = await _create_project(client, admin_token, "Admin member-shared")
    await _add_member(client, admin_token, member_pid, uid)
    audience_pid = await _create_project(client, admin_token, "Admin public")
    await _set_public_audience(test_db, audience_pid)
    await _create_project(client, admin_token, "Admin private")

    resp = await client.get("/api/projects?scope=shared", cookies={"lore_session": token})
    assert resp.status_code == 200

    await test_db.query(
        "UPDATE type::record('users', $id) SET deleted_at = time::now()", {"id": uid},
    )

    resp = await client.get("/api/projects?scope=shared", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert {p["project_id"] for p in resp.json()} == {member_pid}


# TEST-WHY: cross-process-contract — the own cut pages strictly over the caller's rows over real HTTP: disjoint limit/offset pages, foreign and shared rows never present, bare-array response.
@pytest.mark.asyncio
async def test_scope_own_pages_over_own_projects_only(client, regular_user, admin_user):
    """scope=own paginates only the caller's projects: page 0 (limit=2) and page 1
    (offset=2) over 3 own projects are disjoint and together exhaustive, every
    row is owned by the caller, the foreign project never appears, and the
    response stays a bare Project[] (no envelope — has_more is derived by the
    client as page.length === limit)."""
    uid, token = regular_user
    _, admin_token = admin_user
    own = [await _create_project(client, token, f"Own {i}") for i in range(3)]
    foreign_pid = await _create_project(client, admin_token, "Foreign")

    page0 = await client.get("/api/projects?scope=own&limit=2", cookies={"lore_session": token})
    assert page0.status_code == 200
    rows0 = page0.json()
    assert isinstance(rows0, list)
    assert len(rows0) == 2

    page1 = await client.get(
        "/api/projects?scope=own&limit=2&offset=2", cookies={"lore_session": token}
    )
    assert page1.status_code == 200
    rows1 = page1.json()
    assert len(rows1) == 1

    ids0 = {p["project_id"] for p in rows0}
    ids1 = {p["project_id"] for p in rows1}
    assert not ids0 & ids1
    assert ids0 | ids1 == set(own)
    assert all(p["owner_id"] == uid for p in rows0 + rows1)
    assert foreign_pid not in ids0 | ids1
