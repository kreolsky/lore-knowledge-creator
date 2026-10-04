"""Integration tests for project routes."""

import pytest
from helpers import make_token


@pytest.mark.asyncio
async def test_create_project(client, admin_user):
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "My Project"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "My Project"
    assert data["my_access"] == "full"
    assert data["index_doc_id"]


@pytest.mark.asyncio
async def test_create_project_index_doc_has_sort_key(client, admin_user):
    """W6: the project index doc is born with a sort_key — no boot-sweep dependency.

    The index doc is a non-reference document, so it must satisfy the same sort_key
    invariant as documents.service.create_document. Before W6 create_project created it
    via raw CREATE without sort_key (sort_key=NONE), relying on the startup sweep.
    """
    from db import fetch_one
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "SortKey Project"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    idx_id = resp.json()["index_doc_id"]
    record = await fetch_one("documents", idx_id)
    assert record is not None, "index doc not found"
    assert record.get("sort_key") is not None, "index doc born without sort_key"


@pytest.mark.asyncio
async def test_list_projects(client, admin_user, project_with_doc):
    _, token = admin_user
    resp = await client.get("/api/projects", cookies={"lore_session": token})
    assert resp.status_code == 200
    projects = resp.json()
    assert any(p["name"] == "Test Project" for p in projects)


@pytest.mark.asyncio
async def test_get_project_with_tree(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"]["name"] == "Test Project"
    assert isinstance(data["documents"], list)


@pytest.mark.asyncio
async def test_patch_project(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"name": "Updated Name"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_project_with_description(client, admin_user):
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "Described Project", "description": "A world of endless seas"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "A world of endless seas"
    listed = await client.get("/api/projects", cookies={"lore_session": token})
    row = next(p for p in listed.json() if p["project_id"] == data["project_id"])
    assert row["description"] == "A world of endless seas"


@pytest.mark.asyncio
async def test_create_project_without_description_reads_none(client, admin_user):
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "Plain Project"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json().get("description") is None


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
async def test_create_project_description_over_2000_rejected(client, admin_user):
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "Too Long", "description": "x" * 2001},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


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
async def test_search_documents(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    # Create a doc with searchable content via API
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Searchable Doc", "content": "This document contains the keyword findme in its text"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/projects/{pid}/search?q=findme",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) >= 1
    assert results[0]["title"] == "Searchable Doc"
    assert results[0]["snippet"] is not None


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
async def test_set_project_member(client, admin_user, regular_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, _ = regular_user
    resp = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_remove_project_member(client, admin_user, regular_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user
    # Grant then remove
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    resp = await client.delete(
        f"/api/admin/projects/{pid}/members/{user_uid}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # Verify user lost access (non-admin, non-owner, non-public → 404 to hide existence)
    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": user_token})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_list_all_projects(client, admin_user, regular_user, test_db):
    """GET /api/admin/projects returns ALL projects regardless of membership."""
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    # Admin creates a project (is owner + member)
    resp = await client.post(
        "/api/projects",
        json={"name": "Admin Owned"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200

    # Regular user creates a project (admin is NOT owner/member)
    resp = await client.post(
        "/api/projects",
        json={"name": "User Owned"},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200

    # Admin endpoint returns both
    resp = await client.get("/api/admin/projects", cookies={"lore_session": admin_token})
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "Admin Owned" in names
    assert "User Owned" in names

    # Regular /api/projects does NOT return user's project for admin (no membership)
    resp = await client.get("/api/projects", cookies={"lore_session": admin_token})
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "Admin Owned" in names
    assert "User Owned" not in names


@pytest.mark.asyncio
async def test_admin_projects_requires_admin(client, regular_user, test_db):
    """GET /api/admin/projects rejects non-admin users."""
    _, user_token = regular_user
    resp = await client.get("/api/admin/projects", cookies={"lore_session": user_token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_projects_enrichment(client, admin_user, regular_user, test_db):
    """GET /api/admin/projects includes my_access and owner_name."""
    admin_uid, admin_token = admin_user

    resp = await client.post(
        "/api/projects",
        json={"name": "Enrichment Test"},
        cookies={"lore_session": admin_token},
    )
    pid = resp.json()["project_id"]

    resp = await client.get("/api/admin/projects", cookies={"lore_session": admin_token})
    assert resp.status_code == 200
    project = next(p for p in resp.json() if p["project_id"] == pid)
    assert project["my_access"] == "full"
    assert project["owner_name"] == "testadmin"


# ─── System prompts doc ─────────────────────────────────────────────────────


# Plan "unify-agent-config": the three system_prompts_doc_id tests below were
# removed — the project prompts-folder field is gone (personas now live under
# `.lore/system`).


@pytest.mark.asyncio
async def test_delete_project(client, admin_user, test_db):
    _, token = admin_user
    resp = await client.post(
        "/api/projects",
        json={"name": "To Delete"},
        cookies={"lore_session": token},
    )
    pid = resp.json()["project_id"]
    resp = await client.delete(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_delete_project_marks_only_the_project(client, admin_user, test_db):
    """Deleting a project marks ONLY the project row: its documents, references,
    chat sessions and checkpoints keep their own deleted_at = NONE.

    The handler is one UPDATE with no per-document walk, so restore stays a
    one-field flip."""
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/projects", json={"name": "Cascade Test"}, cookies=cookies)
    pid = resp.json()["project_id"]

    resp = await client.post("/api/documents", json={"project_id": pid, "title": "Doc A", "content": "AAA"}, cookies=cookies)
    doc_a = resp.json()["document_id"]

    resp = await client.post("/api/documents", json={"project_id": pid, "title": "Doc B"}, cookies=cookies)
    doc_b = resp.json()["document_id"]

    resp = await client.post("/api/references", json={
        "project_id": pid, "document_id": doc_a, "title": "Ref 1", "media_type": "markdown",
    }, cookies=cookies)
    ref_id = resp.json()["reference_id"]

    resp = await client.post("/api/chat/sessions", json={
        "project_id": pid, "document_id": doc_a,
    }, cookies=cookies)
    session_id = resp.json()["session_id"]

    resp = await client.post("/api/checkpoints", json={"document_id": doc_a, "content": "AAA", "comment": "snap"}, cookies=cookies)
    cp_id = resp.json()["checkpoint_id"]

    resp = await client.delete(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 200

    from db import get_db
    db = await get_db()

    # The project row is the ONLY thing marked.
    proj = await db.query("SELECT deleted_at FROM type::record('projects', $id)", {"id": pid})
    assert proj[0]["deleted_at"] is not None

    # Every child keeps its own state — alive, exactly as before the delete.
    for table, row_id in (
        ("documents", doc_a), ("documents", doc_b), ("documents", ref_id),
        ("chat_sessions", session_id), ("checkpoints", cp_id),
    ):
        rows = await db.query(
            f"SELECT deleted_at FROM type::record('{table}', $id)", {"id": row_id},
        )
        assert rows[0]["deleted_at"] is None, f"{table}:{row_id} was touched by project delete"


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


@pytest.mark.asyncio
async def test_delete_project_returns_without_touching_documents(client, admin_user, test_db):
    """A project with 50 references deletes in one round-trip — no per-document
    walk — and every reference row is left exactly as it was."""
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/projects", json={"name": "Fifty Refs"}, cookies=cookies)
    pid = resp.json()["project_id"]
    for i in range(50):
        resp = await client.post("/api/references", json={
            "project_id": pid, "title": f"Ref {i}", "media_type": "markdown", "content": "body",
        }, cookies=cookies)
        assert resp.status_code == 200

    resp = await client.delete(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 200

    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT count() AS c FROM documents "
        "WHERE project_id = $pid AND deleted_at IS NONE AND is_reference = true "
        "GROUP ALL",
        {"pid": pid},
    )
    assert rows and rows[0]["c"] == 50, "project delete must not touch reference rows"


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


@pytest.mark.asyncio
async def test_owner_last_accessed_doc_persisted(client, admin_user):
    """Owner opens a non-index doc; GET /api/projects reflects it (list)."""
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    # Owner opens the extra (non-index) document
    resp = await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})
    assert resp.status_code == 200

    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    proj = next(p for p in projects if p["project_id"] == pid)
    assert proj["last_accessed_doc_id"] == extra_id


@pytest.mark.asyncio
async def test_owner_last_accessed_single_project(client, admin_user):
    """Owner opens a non-index doc; GET /api/projects/{id} reflects it (single)."""
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})

    data = (await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})).json()
    assert data["project"]["last_accessed_doc_id"] == extra_id


@pytest.mark.asyncio
async def test_owner_last_accessed_updates_on_each_open(client, admin_user):
    """Opening doc A then doc B leaves last-accessed as B."""
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    # Open index first, then extra — final state must be extra
    await client.get(f"/api/documents/{idx_id}?track=1", cookies={"lore_session": token})
    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    assert next(p for p in projects if p["project_id"] == pid)["last_accessed_doc_id"] == idx_id

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})
    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    assert next(p for p in projects if p["project_id"] == pid)["last_accessed_doc_id"] == extra_id


@pytest.mark.asyncio
async def test_per_user_last_accessed_independence(client, admin_user, regular_user):
    """Owner and a member each get their OWN last-accessed doc, no contamination."""
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, admin_token)

    # Grant the regular user full access
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )

    # Owner opens extra, member opens index
    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": admin_token})
    await client.get(f"/api/documents/{idx_id}?track=1", cookies={"lore_session": user_token})

    admin_projects = (await client.get("/api/projects", cookies={"lore_session": admin_token})).json()
    user_projects = (await client.get("/api/projects", cookies={"lore_session": user_token})).json()

    admin_proj = next(p for p in admin_projects if p["project_id"] == pid)
    user_proj = next(p for p in user_projects if p["project_id"] == pid)
    assert admin_proj["last_accessed_doc_id"] == extra_id
    assert user_proj["last_accessed_doc_id"] == idx_id


@pytest.mark.asyncio
async def test_owner_member_row_not_created_as_readonly(client, admin_user):
    """Owner's last-accessed write must NOT create a stray 'readonly' member row."""
    _, token = admin_user
    admin_uid = admin_user[0]
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})

    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT access_level FROM project_members "
        "WHERE project_id = $pid AND user_id = $uid",
        {"pid": pid, "uid": admin_uid},
    )
    levels = [r["access_level"] for r in (rows or [])]
    assert "readonly" not in levels, f"owner must not get a readonly row, got {levels}"


@pytest.mark.asyncio
async def test_owner_last_accessed_persists_without_member_row(client, admin_user):
    """Owner with NO project_members row (legacy/seed projects) still gets last-accessed.

    Reproduces the production bug: seed/legacy projects predate create_project granting
    the owner a 'full' membership row, so the project_members-based write finds no row and
    silently records nothing. Last-accessed must live on the project itself for owners.
    """
    _, token = admin_user
    admin_uid = admin_user[0]
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    # Simulate a legacy/seed project: owner has no membership row at all.
    from db import get_db
    db = await get_db()
    await db.query(
        "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": pid, "uid": admin_uid},
    )

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})

    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    proj = next(p for p in projects if p["project_id"] == pid)
    assert proj["last_accessed_doc_id"] == extra_id
    single = (await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})).json()
    assert single["project"]["last_accessed_doc_id"] == extra_id


@pytest.mark.asyncio
async def test_untracked_fetch_does_not_change_last_accessed(client, admin_user):
    """A non-navigation fetch (no track=1) must NOT clobber last-accessed.

    Reproduces the real bug: hover previews / chat Sources / snapshot banner all hit
    GET /api/documents/{id}; without the track gate they overwrote last-accessed, so
    re-entering a project landed on the last *previewed* doc, not the last *opened* one.
    """
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    # Intentional open of extra_id (tracked).
    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})
    # Auxiliary fetch of the index doc (preview-style, NOT tracked) must not change it.
    await client.get(f"/api/documents/{idx_id}", cookies={"lore_session": token})

    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    proj = next(p for p in projects if p["project_id"] == pid)
    assert proj["last_accessed_doc_id"] == extra_id


@pytest.mark.asyncio
async def test_non_member_does_not_see_owner_last_doc(client, admin_user, regular_user):
    """A non-member viewer of a public project must NOT inherit the owner's last-accessed.

    Guards the regression introduced by storing last-accessed on the project record:
    SELECT * now carries the owner's value, which must be stripped for non-members.
    """
    _, admin_token = admin_user
    _, user_token = regular_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, admin_token)

    # Make the project public so the regular (non-member) user can list it.
    await client.patch(
        f"/api/projects/{pid}",
        json={"is_public": True},
        cookies={"lore_session": admin_token},
    )

    # Owner records a last-accessed doc.
    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": admin_token})

    user_projects = (await client.get("/api/projects", cookies={"lore_session": user_token})).json()
    proj = next((p for p in user_projects if p["project_id"] == pid), None)
    assert proj is not None
    assert proj.get("last_accessed_doc_id") in (None, idx_id) and proj.get("last_accessed_doc_id") != extra_id


async def _make_owned_project_with_full_member(test_db, owner_uid, member_uid):
    """Create a project owned by `owner_uid` (non-admin) with `member_uid` as a
    full-access member. Returns project_id. Used by the is_public owner-gate tests."""
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
async def test_owner_can_toggle_is_public(client, test_db):
    """The (non-admin) project owner may flip is_public → 200."""
    owner_token = make_token("vis-owner-001", "owner", "user", "owner@vis.com")
    pid = await _make_owned_project_with_full_member(test_db, "vis-owner-001", "vis-member-001")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"is_public": True},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    assert resp.json()["is_public"] is True


@pytest.mark.asyncio
async def test_full_not_owner_cannot_toggle_is_public(client, test_db):
    """A full-access non-owner member gets 403 when is_public is in the PATCH body."""
    member_token = make_token("vis-member-001", "member", "user", "member@vis.com")
    pid = await _make_owned_project_with_full_member(test_db, "vis-owner-001", "vis-member-001")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"is_public": True},
        cookies={"lore_session": member_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_full_not_owner_can_edit_other_fields(client, test_db):
    """Regression guard: only is_public is tightened — a full non-owner may still
    edit name/project_context."""
    member_token = make_token("vis-member-001", "member", "user", "member@vis.com")
    pid = await _make_owned_project_with_full_member(test_db, "vis-owner-001", "vis-member-001")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"name": "Renamed By Member"},
        cookies={"lore_session": member_token},
    )
    assert resp.status_code == 200


# ─── ref_image_preview: project-wide references gallery toggle ───────────────


@pytest.mark.asyncio
async def test_ref_image_preview_defaults_true_and_patches(client, admin_user, project_with_doc):
    """A new project renders image refs as a gallery; PATCH stores the flag and every
    subsequent GET returns it, so all members see the same mode."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["project"]["ref_image_preview"] is True

    resp = await client.patch(
        f"/api/projects/{pid}", json={"ref_image_preview": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["ref_image_preview"] is False

    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.json()["project"]["ref_image_preview"] is False


@pytest.mark.asyncio
async def test_full_not_owner_can_toggle_ref_image_preview(client, test_db):
    """The gallery mode is a display preference, not an access decision — a full
    non-owner member may flip it (unlike is_public)."""
    member_token = make_token("vis-member-002", "member", "user", "member2@vis.com")
    pid = await _make_owned_project_with_full_member(test_db, "vis-owner-002", "vis-member-002")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"ref_image_preview": False},
        cookies={"lore_session": member_token},
    )
    assert resp.status_code == 200
    assert resp.json()["ref_image_preview"] is False


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


@pytest.mark.asyncio
async def test_member_last_accessed_dropped_when_doc_soft_deleted(client, admin_user, regular_user):
    """The member pointer lives in project_members, a different column than the
    owner's — it must be validated by the same predicate."""
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, admin_token)
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": user_token})
    await client.delete(f"/api/documents/{extra_id}", cookies={"lore_session": admin_token})

    projects = (await client.get("/api/projects", cookies={"lore_session": user_token})).json()
    proj = next(p for p in projects if p["project_id"] == pid)
    assert proj.get("last_accessed_doc_id") is None

    data = (await client.get(f"/api/projects/{pid}", cookies={"lore_session": user_token})).json()
    assert data["project"].get("last_accessed_doc_id") is None


@pytest.mark.asyncio
async def test_live_last_accessed_survives_the_liveness_filter(client, admin_user):
    """The failing branch: a LIVE pointer must still be returned by both reads —
    the filter drops dangling ids only, never a working one."""
    _, token = admin_user
    pid, idx_id, extra_id = await _make_project_with_two_docs(client, token)

    await client.get(f"/api/documents/{extra_id}?track=1", cookies={"lore_session": token})

    projects = (await client.get("/api/projects", cookies={"lore_session": token})).json()
    assert next(p for p in projects if p["project_id"] == pid)["last_accessed_doc_id"] == extra_id
    data = (await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})).json()
    assert data["project"]["last_accessed_doc_id"] == extra_id
