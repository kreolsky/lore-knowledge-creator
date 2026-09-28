"""Access sanitation regression suite.

Covers:
- Owner is never a member row (one shared grant write path + guard).
- `create_project` does NOT create an owner project_members row.
- Public viewers leave no membership row; last-doc lives in user_preferences.
- Read-side fallback (member row → user_preferences → drop) across all 3 sites.
- Pending-invite cancellation.
- `_flush_pending_invites` grants through `_apply_grant` without re-resolving the email.
- Admin owner guards on set/remove member.
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
    from db import create_record
    uid = "san-owner-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "owner", "email": "owner@san.com",
        "password_hash": hash_secret("pw"), "role": "user", "user_facts": "",
    })
    token = make_token(uid, "owner", "user", "owner@san.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def stranger_user(test_db):
    from db import create_record
    uid = "san-stranger-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "stranger", "email": "stranger@san.com",
        "password_hash": hash_secret("pw"), "role": "user", "user_facts": "",
    })
    token = make_token(uid, "stranger", "user", "stranger@san.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def public_project(test_db, owner_user):
    """A public project owned by owner_user with one doc (no member rows).

    Yields (project_id, document_id, owner_uid, owner_token).
    """
    from db import create_record
    owner_uid, owner_token = owner_user
    pid = "san-pub-proj-001"
    did = "san-pub-doc-001"
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await test_db.query("DELETE type::record('documents', $id)", {"id": did})
    await test_db.query("DELETE project_members WHERE project_id = $p", {"p": pid})
    await create_record("projects", pid, {
        "name": "Public", "status": "active", "project_context": "",
        "owner_id": owner_uid, "is_public": True,
    })
    await create_record("documents", did, {
        "project_id": pid, "parent_id": None,
        "title": "doc.md", "content": "", "path": "doc.md",
    })
    yield pid, did, owner_uid, owner_token
    await test_db.query("DELETE pending_invites WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE project_members WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE user_preferences WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE type::record('documents', $id)", {"id": did})
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})


async def _db():
    from db import get_db
    return await get_db()


async def _direct_project_member(test_db, *, project_id, user_id, access_level="full", **extra):
    from db import create_record
    payload = {"project_id": project_id, "user_id": user_id, "access_level": access_level}
    payload.update(extra)
    await create_record("project_members", str(uuid4()), payload)


# ─── Owner never a member row: read filters ─────────────────────────────────


@pytest.mark.asyncio
async def test_get_members_excludes_owner_even_with_direct_row(
    client, public_project, test_db,
):
    """A legacy owner project_members row must NOT leak into GET /members."""
    pid, _, owner_uid, owner_token = public_project
    await _direct_project_member(test_db, project_id=pid, user_id=owner_uid, access_level="full")
    resp = await client.get(
        f"/api/projects/{pid}/members", cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    emails = {m.get("email") for m in resp.json()["members"]}
    assert "owner@san.com" not in emails


@pytest.mark.asyncio
async def test_admin_list_project_members_excludes_owner(
    client, public_project, admin_user, test_db,
):
    pid, _, owner_uid, _ = public_project
    _, admin_token = admin_user
    await _direct_project_member(test_db, project_id=pid, user_id=owner_uid, access_level="full")
    resp = await client.get(
        f"/api/admin/projects/{pid}/members", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert owner_uid not in resp.json()


# ─── Owner guard on invite (silent no-op) ───────────────────────────────────


@pytest.mark.asyncio
async def test_invite_owner_email_is_silent_noop(client, public_project, test_db):
    pid, _, owner_uid, owner_token = public_project
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "owner@san.com", "access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}
    rows = await (await _db()).query(
        "SELECT id FROM project_members WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": owner_uid},
    )
    assert len(rows) == 0


# ─── Admin owner guards ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_set_project_member_owner_rejected(client, public_project, admin_user):
    pid, _, owner_uid, _ = public_project
    _, admin_token = admin_user
    resp = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": owner_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_remove_project_member_owner_rejected(client, public_project, admin_user):
    pid, _, owner_uid, _ = public_project
    _, admin_token = admin_user
    resp = await client.delete(
        f"/api/admin/projects/{pid}/members/{owner_uid}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400


# ─── Dedup / single write path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_existing_member_updates_role_single_row(
    client, public_project, stranger_user,
):
    pid, _, _, owner_token = public_project
    s_uid, _ = stranger_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@san.com", "access_level": "readonly"},
        cookies={"lore_session": owner_token},
    )
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@san.com", "access_level": "full"},
        cookies={"lore_session": owner_token},
    )
    rows = await (await _db()).query(
        "SELECT access_level FROM project_members WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": s_uid},
    )
    assert len(rows) == 1
    assert rows[0]["access_level"] == "full"


# ─── create_project: owner row never created ────────────────────────────────


@pytest.mark.asyncio
async def test_create_project_no_owner_member_row(client, owner_user):
    owner_uid, owner_token = owner_user
    resp = await client.post(
        "/api/projects", json={"name": "Fresh"},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["my_access"] == "full"
    pid = body["project_id"]
    rows = await (await _db()).query(
        "SELECT id FROM project_members WHERE project_id = $p",
        {"p": pid},
    )
    assert len(rows) == 0
    members = (await client.get(
        f"/api/projects/{pid}/members", cookies={"lore_session": owner_token},
    )).json()["members"]
    assert members == []


# ─── Public viewer leaves no row; last-doc in user_preferences ──────────────


@pytest.mark.asyncio
async def test_public_viewer_leaves_no_member_row_last_doc_in_prefs(
    client, public_project, stranger_user,
):
    pid, did, _, _ = public_project
    _, s_token = stranger_user
    resp = await client.get(
        f"/api/documents/{did}?track=1", cookies={"lore_session": s_token},
    )
    assert resp.status_code == 200
    db = await _db()
    pm = await db.query(
        "SELECT id FROM project_members WHERE project_id = $p",
        {"p": pid},
    )
    assert len(pm) == 0
    prefs = await db.query(
        "SELECT preferences FROM user_preferences WHERE project_id = $p",
        {"p": pid},
    )
    assert prefs and prefs[0]["preferences"].get("last_accessed_doc_id") == did


@pytest.mark.asyncio
async def test_public_viewer_last_doc_resumes_on_reads(
    client, public_project, stranger_user,
):
    pid, did, _, _ = public_project
    _, s_token = stranger_user
    await client.get(f"/api/documents/{did}?track=1", cookies={"lore_session": s_token})
    projects = (await client.get("/api/projects", cookies={"lore_session": s_token})).json()
    proj = next(p for p in projects if p["project_id"] == pid)
    assert proj["last_accessed_doc_id"] == did
    single = (await client.get(f"/api/projects/{pid}", cookies={"lore_session": s_token})).json()
    assert single["project"]["last_accessed_doc_id"] == did


@pytest.mark.asyncio
async def test_public_viewer_access_revoked_when_project_private(
    client, public_project, stranger_user,
):
    pid, did, _, owner_token = public_project
    _, s_token = stranger_user
    await client.get(f"/api/documents/{did}?track=1", cookies={"lore_session": s_token})
    await client.patch(
        f"/api/projects/{pid}", json={"is_public": False},
        cookies={"lore_session": owner_token},
    )
    resp = await client.get(f"/api/documents/{did}", cookies={"lore_session": s_token})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_real_member_last_doc_uses_member_row(
    client, public_project, stranger_user,
):
    """A real member still tracks last-doc on project_members, not user_preferences."""
    pid, did, _, owner_token = public_project
    s_uid, s_token = stranger_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@san.com", "access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    await client.get(f"/api/documents/{did}?track=1", cookies={"lore_session": s_token})
    db = await _db()
    pm = await db.query(
        "SELECT last_accessed_doc_id FROM project_members WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": s_uid},
    )
    assert pm and pm[0]["last_accessed_doc_id"] == did
    prefs = await db.query(
        "SELECT id FROM user_preferences WHERE project_id = $p AND user_id = $u",
        {"p": pid, "u": s_uid},
    )
    assert len(prefs) == 0


# ─── Pending-invite flush ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_uses_apply_grant_not_email_resolve(
    client, public_project, admin_user, monkeypatch,
):
    """`_flush_pending_invites` calls `_apply_grant` with the known new uid and
    never re-resolves the email per pending row (no double-resolve).
    """
    pid, _, owner_uid, owner_token = public_project
    _, admin_token = admin_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "flush3@san.com", "access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    import routes._access_grants as grants_mod
    import routes.users as users_mod
    calls: list[dict] = []
    orig = grants_mod._apply_grant

    async def spy(*, project_id, target_uid, access_level, db=None):
        calls.append({"target_uid": target_uid})
        return await orig(
            project_id=project_id, target_uid=target_uid,
            access_level=access_level, db=db,
        )

    monkeypatch.setattr(grants_mod, "_apply_grant", spy)
    monkeypatch.setattr(users_mod, "_apply_grant", spy, raising=False)

    import access as access_mod
    email_calls = {"n": 0}
    orig_lookup = access_mod.get_user_by_email

    async def spy_lookup(email):
        email_calls["n"] += 1
        return await orig_lookup(email)

    monkeypatch.setattr(access_mod, "get_user_by_email", spy_lookup)
    monkeypatch.setattr(grants_mod, "get_user_by_email", spy_lookup, raising=False)

    resp = await client.post(
        "/api/admin/users",
        json={"name": "f3", "email": "flush3@san.com", "password": "password1", "role": "user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert calls, "_apply_grant must be called during flush"
    assert email_calls["n"] == 0, "flush must not re-resolve emails"


# ─── Pending-invite cancellation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_project_pending_invite(client, public_project):
    pid, _, _, owner_token = public_project
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "cancel1@san.com", "access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    db = await _db()
    before = await db.query(
        "SELECT id FROM pending_invites WHERE project_id = $p",
        {"p": pid},
    )
    assert len(before) == 1
    resp = await client.delete(
        f"/api/projects/{pid}/invites?email=cancel1@san.com",
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200
    after = await db.query(
        "SELECT id FROM pending_invites WHERE project_id = $p",
        {"p": pid},
    )
    assert len(after) == 0


@pytest.mark.asyncio
async def test_cancel_invite_non_owner_forbidden(client, public_project, stranger_user):
    pid, _, _, _ = public_project
    _, s_token = stranger_user
    resp = await client.delete(
        f"/api/projects/{pid}/invites?email=cancel3@san.com",
        cookies={"lore_session": s_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cancel_invite_unknown_email_noop(client, public_project):
    pid, _, _, owner_token = public_project
    resp = await client.delete(
        f"/api/projects/{pid}/invites?email=ghost@san.com",
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200


# ─── No over-filtering: real member still returned & editable ───────────────


@pytest.mark.asyncio
async def test_real_member_still_listed_and_editable(client, public_project, stranger_user):
    pid, _, _, owner_token = public_project
    s_uid, _ = stranger_user
    await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@san.com", "access_level": "readonly"},
        cookies={"lore_session": owner_token},
    )
    members = (await client.get(
        f"/api/projects/{pid}/members", cookies={"lore_session": owner_token},
    )).json()["members"]
    assert any(m["email"] == "stranger@san.com" for m in members)
    resp = await client.patch(
        f"/api/projects/{pid}/members/{s_uid}",
        json={"access_level": "commentator"},
        cookies={"lore_session": owner_token},
    )
    assert resp.status_code == 200

