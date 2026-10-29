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
