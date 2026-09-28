"""Self-service project access.

Covers:
- Owner-gated member CRUD with silent invite semantics.
- Pending invites flushed on user creation.
- Resolver: get_document_access returns None without project access.
- Member changes emit access_changed.
- members_count in project list payload.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from emit_recorder import EmitRecorder
from helpers import make_token
from password import hash_secret

# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def owner_user(test_db):
    """Project owner (non-admin)."""
    from db import create_record
    uid = "test-owner-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "owner",
        "email": "owner@test.com",
        "password_hash": hash_secret("ownerpass"),
        "role": "user",
        "user_facts": "",
    })
    token = make_token(uid, "owner", "user", "owner@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def stranger_user(test_db):
    """A registered user who is NOT a project member."""
    from db import create_record
    uid = "test-stranger-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "stranger",
        "email": "stranger@test.com",
        "password_hash": hash_secret("pw"),
        "role": "user",
        "user_facts": "",
    })
    token = make_token(uid, "stranger", "user", "stranger@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def owner_project(test_db, owner_user, client):
    """Project owned by owner_user with one doc."""
    from db import create_record
    owner_uid, owner_token = owner_user
    pid = "test-owned-proj-001"
    did = "test-owned-doc-001"
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await test_db.query("DELETE type::record('documents', $id)", {"id": did})
    await test_db.query("DELETE project_members WHERE project_id = $p", {"p": pid})
    await create_record("projects", pid, {
        "name": "Owned Project",
        "status": "active",
        "project_context": "",
        "owner_id": owner_uid,
    })
    await create_record("documents", did, {
        "project_id": pid,
        "parent_id": None,
        "title": "doc.md",
        "content": "",
        "path": "doc.md",
    })
    yield pid, did, owner_uid, owner_token
    await test_db.query("DELETE pending_invites WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE project_members WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE type::record('documents', $id)", {"id": did})
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})


# ─── Membership: silent invite ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invite_existing_user_creates_membership(client, owner_project, stranger_user, test_db):
    pid, _, _, owner_token = owner_project
    s_uid, _ = stranger_user
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "full"},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}
    from db import get_db
    _db = await get_db()
    rows = await _db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $p",
        {"p": pid},
    )
    user_ids = {r["user_id"] for r in rows}
    assert any("test-stranger-001" in str(u) for u in user_ids)


@pytest.mark.asyncio
async def test_invite_unknown_email_queues_pending(client, owner_project, test_db):
    pid, _, _, owner_token = owner_project
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "ghost@example.com", "access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}
    from db import get_db
    _db = await get_db()
    rows = await _db.query(
        "SELECT email, access_level FROM pending_invites WHERE project_id = $p",
        {"p": pid},
    )
    assert len(rows) == 1
    assert rows[0]["email"] == "ghost@example.com"
    assert rows[0]["access_level"] == "commentator"
    members = await _db.query(
        "SELECT id FROM project_members WHERE project_id = $p",
        {"p": pid},
    )
    assert len(members) == 0


@pytest.mark.asyncio
async def test_invite_response_identical_for_known_and_unknown(client, owner_project, stranger_user):
    pid, _, _, owner_token = owner_project
    r1 = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "readonly"},
        cookies={"lore_session": owner_token},
    )
    r2 = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "nobody@example.com", "access_level": "readonly"},
        cookies={"lore_session": owner_token},
    )
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() == {"queued": True}


# ─── Membership: owner gate ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_non_owner_cannot_invite(client, owner_project, stranger_user, test_db):
    pid, _, _, _ = owner_project
    s_uid, s_token = stranger_user
    # Make stranger a full-access member (still not owner)
    from db import create_record
    await create_record("project_members", str(uuid4()), {
        "project_id": pid, "user_id": s_uid, "access_level": "full",
    })
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "x@y.com", "access_level": "readonly"},
        cookies={"lore_session": s_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_owner_cannot_list_members(client, owner_project, stranger_user):
    pid, _, _, _ = owner_project
    _, s_token = stranger_user
    resp = await client.get(
        f"/api/projects/{pid}/members",
        cookies={"lore_session": s_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_lists_members_with_pending(client, owner_project, stranger_user):
    pid, _, _, owner_token = owner_project
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "full"},
        cookies={"lore_session": owner_token},
    )
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "ghost@example.com", "access_level": "readonly"},
        cookies={"lore_session": owner_token},
    )
    resp = await client.get(
        f"/api/projects/{pid}/members",
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    body = resp.json()
    members = body["members"]
    emails = {m["email"] for m in members}
    assert "stranger@test.com" in emails
    assert "ghost@example.com" in emails
    ghost = next(m for m in members if m["email"] == "ghost@example.com")
    assert ghost["pending"] is True
    real = next(m for m in members if m["email"] == "stranger@test.com")
    assert real.get("pending") is not True


@pytest.mark.asyncio
async def test_owner_cannot_self_remove(client, owner_project):
    pid, _, owner_uid, owner_token = owner_project
    resp = await client.delete(
        f"/api/projects/{pid}/members/{owner_uid}",
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_owner_removes_member(client, owner_project, stranger_user, test_db):
    pid, _, _, owner_token = owner_project
    s_uid, _ = stranger_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "full"},
        cookies={"lore_session": owner_token},
    )
    resp = await client.delete(
        f"/api/projects/{pid}/members/{s_uid}",
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    from db import get_db
    _db = await get_db()
    rows = await _db.query(
        "SELECT id FROM project_members WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": s_uid},
    )
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_owner_patches_member_role(client, owner_project, stranger_user, test_db):
    pid, _, _, owner_token = owner_project
    s_uid, _ = stranger_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "readonly"},
        cookies={"lore_session": owner_token},
    )
    resp = await client.patch(
        f"/api/projects/{pid}/members/{s_uid}",
        json={"access_level": "full"},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    from db import get_db
    _db = await get_db()
    rows = await _db.query(
        "SELECT access_level FROM project_members WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": s_uid},
    )
    assert rows[0]["access_level"] == "full"


# ─── Pending-invite flush on registration ──────────────────────────────────

@pytest.mark.asyncio
async def test_pending_invite_flushes_on_user_creation(
    client, owner_project, admin_user, test_db,
):
    pid, _, _, owner_token = owner_project
    _, admin_token = admin_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "newbie@example.com", "access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    resp = await client.post(
        "/api/admin/users",
        json={"name": "newbie", "email": "newbie@example.com", "password": "password1", "role": "user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    new_uid = resp.json()["user_id"]
    from db import get_db
    _db = await get_db()
    members = await _db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $p",
        {"p": pid},
    )
    matched = [m for m in members if new_uid in str(m["user_id"])]
    assert len(matched) == 1
    assert matched[0]["access_level"] == "commentator"
    remaining = await _db.query(
        "SELECT id FROM pending_invites WHERE email = $e",
        {"e": "newbie@example.com"},
    )
    assert len(remaining) == 0


# ─── Resolver + events ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolver_returns_none_for_no_access(owner_project, stranger_user):
    _, did, _, _ = owner_project
    s_uid, _ = stranger_user
    from access import get_document_access
    assert await get_document_access(did, {"user_id": s_uid}) is None


@pytest.mark.asyncio
async def test_member_change_emits_access_changed(client, owner_project, stranger_user, monkeypatch):
    pid, _, _, owner_token = owner_project
    with EmitRecorder.active(passthrough=True) as rec:
        await client.post(
            f"/api/projects/{pid}/members",
            json={"email": "stranger@test.com", "access_level": "full"},
            cookies={"lore_session": owner_token},
        )
    assert "access_changed" in rec.names()


# ─── members_count in project list ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_project_list_includes_members_count(client, owner_project, stranger_user):
    pid, _, _, owner_token = owner_project
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "full"},
        cookies={"lore_session": owner_token},
    )
    resp = await client.get("/api/projects", cookies={"lore_session": owner_token})
    assert resp.status_code == 200
    projs = resp.json()
    target = next(p for p in projs if p.get("project_id") == pid)
    assert target.get("members_count", 0) >= 1


# ─── Regression: owner must not get an implicit project_members row ─────────

@pytest.mark.asyncio
async def test_owner_opening_doc_does_not_create_member_row(
    client, owner_project, test_db,
):
    """GET /api/documents/{id} as the owner must NOT auto-create a project_members
    row. The owner's access derives from projects.owner_id; a stale row leaks
    into GET /members as 'owner: readonly'.
    """
    pid, did, owner_uid, owner_token = owner_project
    resp = await client.get(
        f"/api/documents/{did}",
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    from db import get_db
    _db = await get_db()
    rows = await _db.query(
        "SELECT id FROM project_members WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": owner_uid},
    )
    assert len(rows) == 0, "owner must have no project_members row"
