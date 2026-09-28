"""Moderator role — group scoping, manager guard, PATCH transition rules, role overlay.

Plan: .kilo/plans/moderator-role-foundation.md step 1. The moderator is a group
manager over role='user' rows carrying moderator_id = <their uid>; everything
else (foreign users, role field, other moderators) is out of scope and answered
404/403 so a moderator cannot learn that a foreign user exists.
"""

import pytest
import pytest_asyncio
from helpers import make_token, wipe_project_children_sql
from password import hash_secret

from db import create_record


@pytest_asyncio.fixture
async def moderator_user(test_db):
    """A role='moderator' user — the group head."""
    uid = "test-mod-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "testmod",
        "email": "mod@test.com",
        "password_hash": hash_secret("pass123"),
        "role": "moderator",
        "user_facts": "",
    })
    token = make_token(uid, "testmod", "moderator", "mod@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


@pytest_asyncio.fixture
async def group_user(test_db, moderator_user):
    """A role='user' row in the moderator's group (moderator_id = moderator)."""
    mod_uid, _ = moderator_user
    uid = "test-group-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "groupmember",
        "email": "group@test.com",
        "password_hash": hash_secret("pass123"),
        "role": "user",
        "user_facts": "",
        "moderator_id": mod_uid,
    })
    token = make_token(uid, "groupmember", "user", "group@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


# ─── List / create gates ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_moderator_lists_only_own_group(client, admin_user, moderator_user, group_user, regular_user):
    """Moderator's list is scoped to the own group and carries provenance fields."""
    mod_uid, mod_token = moderator_user
    resp = await client.get("/api/admin/users", cookies={"lore_session": mod_token})
    assert resp.status_code == 200
    users = resp.json()
    names = {u["name"] for u in users}
    assert "groupmember" in names
    assert "testadmin" not in names
    assert "testuser" not in names
    assert "testmod" not in names  # the moderator themself is not in the group
    member = next(u for u in users if u["name"] == "groupmember")
    assert member["moderator_id"] == mod_uid
    assert "created_by" in member


@pytest.mark.asyncio
async def test_list_resolves_group_and_creator_names(client, test_db, admin_user, moderator_user, group_user):
    """The row carries moderator_name / created_by_name resolved server-side, so a
    moderator (whose scoped list does not contain themself) still sees names, not uids."""
    mod_uid, mod_token = moderator_user
    admin_uid, admin_token = admin_user
    member_uid, _ = group_user
    await test_db.query(
        "UPDATE type::record('users', $id) SET created_by = $by", {"id": member_uid, "by": admin_uid},
    )
    for token in (mod_token, admin_token):
        resp = await client.get("/api/admin/users", cookies={"lore_session": token})
        assert resp.status_code == 200
        member = next(u for u in resp.json() if u["user_id"] == member_uid)
        assert member["moderator_name"] == "testmod"
        assert member["created_by_name"] == "testadmin"


@pytest.mark.asyncio
async def test_patch_and_create_return_resolved_names(client, admin_user, moderator_user, regular_user):
    """PATCH and POST answer with the LIST row shape — moderator_name / created_by_name
    resolved — so the admin page can splice the response in without a reload
    (the group chip appeared only after a refresh when the PATCH row lacked them)."""
    mod_uid, _ = moderator_user
    _, admin_token = admin_user
    reg_uid, _ = regular_user
    resp = await client.patch(
        f"/api/admin/users/{reg_uid}",
        json={"moderator_id": mod_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["moderator_name"] == "testmod"
    resp = await client.post(
        "/api/admin/users",
        json={"name": "named", "email": "named@test.com", "password": "pass123",
              "role": "user", "moderator_id": mod_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["moderator_name"] == "testmod"
    assert resp.json()["created_by_name"] == "testadmin"


@pytest.mark.asyncio
async def test_admin_role_param_filters_moderators(client, admin_user, moderator_user):
    """?role=moderator feeds the admin's moderator select."""
    _, admin_token = admin_user
    mod_uid, _ = moderator_user
    resp = await client.get(
        "/api/admin/users", params={"role": "moderator"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    users = resp.json()
    assert users, "moderator must be listed"
    assert all(u["role"] == "moderator" for u in users)
    assert any(u["user_id"] == mod_uid for u in users)


@pytest.mark.asyncio
async def test_create_user_non_admin_403(client, regular_user):
    """A plain user cannot create users through the admin route."""
    _, token = regular_user
    resp = await client.post(
        "/api/admin/users",
        json={"name": "x", "email": "x@test.com", "password": "pass123", "role": "user"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_create_forces_user_role_and_own_group(client, moderator_user):
    """Moderator's create ignores the body's role and pins the own group."""
    mod_uid, mod_token = moderator_user
    resp = await client.post(
        "/api/admin/users",
        json={"name": "invited", "email": "invited@test.com", "password": "pass123", "role": "admin"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "user"
    assert data["moderator_id"] == mod_uid
    assert data["created_by"] == mod_uid


@pytest.mark.asyncio
async def test_admin_create_with_moderator_id(client, admin_user, moderator_user):
    """Admin's create takes role + moderator_id from the body."""
    mod_uid, _ = moderator_user
    _, admin_token = admin_user
    resp = await client.post(
        "/api/admin/users",
        json={"name": "bound", "email": "bound@test.com", "password": "pass123",
              "role": "user", "moderator_id": mod_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["moderator_id"] == mod_uid


# ─── require_user_manager guard ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_moderator_patch_group_member(client, moderator_user, group_user):
    """The happy path: moderator edits a own-group member's manager fields."""
    group_uid, mod_token = group_user[0], moderator_user[1]
    resp = await client.patch(
        f"/api/admin/users/{group_uid}",
        json={"name": "renamed_by_mod"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed_by_mod"


@pytest.mark.asyncio
async def test_moderator_patch_foreign_user_404(client, moderator_user, regular_user):
    """A foreign user must not be known to exist — 404, not 403."""
    foreign_uid, mod_token = regular_user[0], moderator_user[1]
    resp = await client.patch(
        f"/api/admin/users/{foreign_uid}",
        json={"name": "hijack"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_moderator_patch_with_role_403(client, moderator_user, group_user):
    """Admin-only fields in a moderator's PATCH are refused before any write."""
    group_uid, mod_token = group_user[0], moderator_user[1]
    resp = await client.patch(
        f"/api/admin/users/{group_uid}",
        json={"name": "ok", "role": "admin"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_patch_with_moderator_id_403(client, moderator_user, group_user):
    """moderator_id is an admin field too — a moderator cannot regroup members."""
    group_uid, mod_token = group_user[0], moderator_user[1]
    resp = await client.patch(
        f"/api/admin/users/{group_uid}",
        json={"moderator_id": None},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_foreign_project_access_404(client, moderator_user, regular_user):
    """project-access of a foreign user reads as nonexistent."""
    foreign_uid, mod_token = regular_user[0], moderator_user[1]
    resp = await client.get(
        f"/api/admin/users/{foreign_uid}/project-access",
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_moderator_group_member_project_access(client, moderator_user, group_user):
    """project-access of an own-group member is readable."""
    group_uid, mod_token = group_user[0], moderator_user[1]
    resp = await client.get(
        f"/api/admin/users/{group_uid}/project-access",
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200


# ─── Transition rules ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_promote_clears_moderator_id(client, admin_user, moderator_user, group_user):
    """Promotion to moderator flattens the tree — the target's group link is dropped."""
    group_uid, admin_token = group_user[0], admin_user[1]
    resp = await client.patch(
        f"/api/admin/users/{group_uid}",
        json={"role": "moderator"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "moderator"
    assert data["moderator_id"] is None


@pytest.mark.asyncio
async def test_demote_with_nonempty_group_409(client, admin_user, moderator_user, group_user):
    """A moderator with members cannot be demoted until the group is reassigned."""
    mod_uid, admin_token = moderator_user[0], admin_user[1]
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}",
        json={"role": "user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 409
    assert resp.json()["group_size"] == 1

    # Admin reassigns first, then the demote goes through.
    group_uid = group_user[0]
    resp = await client.patch(
        f"/api/admin/users/{group_uid}",
        json={"moderator_id": None},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}",
        json={"role": "user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"


@pytest.mark.asyncio
async def test_delete_moderator_with_group_409(client, admin_user, moderator_user, group_user):
    """Soft-deleting a moderator with a non-empty group is guarded the same way."""
    mod_uid, admin_token = moderator_user[0], admin_user[1]
    resp = await client.delete(f"/api/admin/users/{mod_uid}", cookies={"lore_session": admin_token})
    assert resp.status_code == 409
    assert resp.json()["group_size"] == 1


@pytest.mark.asyncio
async def test_moderator_id_targeting_non_moderator_422(client, admin_user, regular_user):
    """A group link must point at a role='moderator' row — an admin uid is not one."""
    admin_uid, admin_token = admin_user
    target_uid, _ = regular_user
    resp = await client.patch(
        f"/api/admin/users/{target_uid}",
        json={"moderator_id": admin_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_moderator_id_only_on_user_role_422(client, admin_user, moderator_user):
    """Pinning a group onto a moderator/admin row is a contradictory request."""
    mod_uid, _ = moderator_user
    _, admin_token = admin_user
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}",
        json={"moderator_id": mod_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_self_role_change_400(client, admin_user):
    """Self-demotion (any self role change) is refused like self-delete."""
    uid, token = admin_user
    resp = await client.patch(
        f"/api/admin/users/{uid}",
        json={"role": "user"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


# ─── Role overlay — role change without logout ────────────────────────────────


@pytest.mark.asyncio
async def test_role_overlay_promotion(client, admin_user, group_user):
    """After a promote, the target's NEXT request on the OLD cookie is a moderator's."""
    group_uid, group_token = group_user
    _, admin_token = admin_user
    # Before: plain user → 403 on the manager gate.
    resp = await client.get("/api/admin/users", cookies={"lore_session": group_token})
    assert resp.status_code == 403
    resp = await client.patch(
        f"/api/admin/users/{group_uid}",
        json={"role": "moderator"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    # Same cookie, no re-login: authorized under the new role.
    resp = await client.get("/api/admin/users", cookies={"lore_session": group_token})
    assert resp.status_code == 200
    resp = await client.get("/api/auth/me", cookies={"lore_session": group_token})
    assert resp.status_code == 200
    me = resp.json()
    assert me["role"] == "moderator"
    assert me["can_manage_users"] is True
    assert me["is_admin"] is False


@pytest.mark.asyncio
async def test_role_overlay_demotion(client, admin_user, moderator_user):
    """After a demote (empty group), the OLD moderator cookie loses the gate."""
    mod_uid, mod_token = moderator_user
    _, admin_token = admin_user
    resp = await client.get("/api/admin/users", cookies={"lore_session": mod_token})
    assert resp.status_code == 200
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}",
        json={"role": "user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    resp = await client.get("/api/admin/users", cookies={"lore_session": mod_token})
    assert resp.status_code == 403
    resp = await client.get("/api/auth/me", cookies={"lore_session": mod_token})
    assert resp.status_code == 200
    me = resp.json()
    assert me["role"] == "user"
    assert me["can_manage_users"] is False
    assert me["is_admin"] is False


@pytest.mark.asyncio
async def test_me_flags_for_admin(client, admin_user):
    """/me capability flags for an admin."""
    _, token = admin_user
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200
    me = resp.json()
    assert me["is_admin"] is True
    assert me["can_manage_users"] is True


# ─── Moderator's scoped admin panel: Projects ─────────────────────────────────


@pytest_asyncio.fixture
async def scoped_projects(test_db, admin_user, moderator_user, group_user):
    """Three projects: owned by the moderator, by the group member, by the admin."""
    mod_uid, _ = moderator_user
    group_uid, _ = group_user
    admin_uid, _ = admin_user
    rows = {
        "p-mod": mod_uid,
        "p-group": group_uid,
        "p-admin": admin_uid,
    }
    for pid, owner in rows.items():
        await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
        await create_record("projects", pid, {
            "name": pid,
            "status": "active",
            "project_context": "",
            "owner_id": owner,
        })
    yield rows
    for pid in rows:
        await test_db.query(
            wipe_project_children_sql() + "; DELETE type::record('projects', $id)",
            {"id": pid},
        )


@pytest.mark.asyncio
async def test_moderator_project_list_scoped(client, admin_user, moderator_user, group_user, scoped_projects):
    """A moderator's project list = own projects + group members' projects only."""
    mod_uid, mod_token = moderator_user
    resp = await client.get("/api/admin/projects", cookies={"lore_session": mod_token})
    assert resp.status_code == 200
    ids = {p["project_id"] for p in resp.json()}
    assert "p-mod" in ids and "p-group" in ids
    assert "p-admin" not in ids

    _, admin_token = admin_user
    resp = await client.get("/api/admin/projects", cookies={"lore_session": admin_token})
    assert resp.status_code == 200
    ids = {p["project_id"] for p in resp.json()}
    assert {"p-mod", "p-group", "p-admin"} <= ids


@pytest.mark.asyncio
async def test_project_list_requires_manager_role(client, regular_user):
    """A plain user gets no admin project list."""
    _, token = regular_user
    resp = await client.get("/api/admin/projects", cookies={"lore_session": token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_project_members_scoped(client, moderator_user, scoped_projects):
    """Members of a project owned by the moderator's group are readable; a
    foreign project reads as nonexistent."""
    _, mod_token = moderator_user
    resp = await client.get(
        "/api/admin/projects/p-group/members", cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200

    resp = await client.get(
        "/api/admin/projects/p-admin/members", cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404


# ─── Member writes (step 4) ───────────────────────────────────────────────────


async def _member_row(test_db, pid: str, uid: str) -> list:
    return await test_db.query(
        "SELECT user_id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": pid, "uid": uid},
    ) or []


@pytest.mark.asyncio
async def test_moderator_sets_group_member_on_own_project(client, test_db, moderator_user, group_user, scoped_projects):
    """A moderator grants a group member access to the moderator's own project;
    the row exists afterwards and the members list shows it."""
    _, mod_token = moderator_user
    group_uid, _ = group_user
    resp = await client.post(
        "/api/admin/projects/p-mod/members",
        json={"user_id": group_uid, "access_level": "full"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200
    assert len(await _member_row(test_db, "p-mod", group_uid)) == 1
    resp = await client.get("/api/admin/projects/p-mod/members", cookies={"lore_session": mod_token})
    assert resp.json() == {group_uid: "full"}


@pytest.mark.asyncio
async def test_moderator_adds_self_to_group_member_project(client, test_db, moderator_user, group_user, scoped_projects):
    """A moderator can add themself to a project owned by a group member."""
    mod_uid, mod_token = moderator_user
    resp = await client.post(
        "/api/admin/projects/p-group/members",
        json={"user_id": mod_uid, "access_level": "full"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200
    assert len(await _member_row(test_db, "p-group", mod_uid)) == 1


@pytest.mark.asyncio
async def test_moderator_sets_foreign_user_404_no_row(client, test_db, moderator_user, regular_user, scoped_projects):
    """A uid outside the group is refused as nonexistent and no row is written —
    the UI dropdown never offers it, this guards the direct API call."""
    _, mod_token = moderator_user
    reg_uid, _ = regular_user
    resp = await client.post(
        "/api/admin/projects/p-mod/members",
        json={"user_id": reg_uid, "access_level": "readonly"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404
    assert await _member_row(test_db, "p-mod", reg_uid) == []


@pytest.mark.asyncio
async def test_moderator_sets_member_on_foreign_project_404(client, test_db, moderator_user, group_user, scoped_projects):
    """A group member cannot be granted access to a project outside the scope."""
    _, mod_token = moderator_user
    group_uid, _ = group_user
    resp = await client.post(
        "/api/admin/projects/p-admin/members",
        json={"user_id": group_uid, "access_level": "readonly"},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404
    assert await _member_row(test_db, "p-admin", group_uid) == []


@pytest.mark.asyncio
async def test_moderator_removes_group_member(client, test_db, moderator_user, group_user, scoped_projects):
    """DELETE mirrors POST: a group member's row on a scoped project is removed;
    a foreign uid and a foreign project read as nonexistent and leave rows alone."""
    _, mod_token = moderator_user
    group_uid, _ = group_user
    for pid in ("p-mod", "p-admin"):
        await create_record("project_members", f"pm-{pid}", {
            "project_id": pid, "user_id": group_uid, "access_level": "readonly",
        })
    try:
        resp = await client.delete(
            f"/api/admin/projects/p-mod/members/{group_uid}", cookies={"lore_session": mod_token},
        )
        assert resp.status_code == 200
        assert await _member_row(test_db, "p-mod", group_uid) == []

        resp = await client.delete(
            "/api/admin/projects/p-mod/members/someone-else", cookies={"lore_session": mod_token},
        )
        assert resp.status_code == 404

        resp = await client.delete(
            f"/api/admin/projects/p-admin/members/{group_uid}", cookies={"lore_session": mod_token},
        )
        assert resp.status_code == 404
        assert len(await _member_row(test_db, "p-admin", group_uid)) == 1
    finally:
        await test_db.query("DELETE project_members WHERE user_id = $uid", {"uid": group_uid})


@pytest.mark.asyncio
async def test_plain_user_member_write_403(client, regular_user, scoped_projects):
    """A plain user gets no member-write surface at all."""
    reg_uid, token = regular_user
    resp = await client.post(
        "/api/admin/projects/p-mod/members",
        json={"user_id": reg_uid, "access_level": "readonly"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403
    resp = await client.delete(
        f"/api/admin/projects/p-mod/members/{reg_uid}", cookies={"lore_session": token},
    )
    assert resp.status_code == 403


# ─── Ownership transfer ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_moderator_transfers_own_project_to_group_member(
    client, test_db, moderator_user, group_user, scoped_projects,
):
    """Happy path: a moderator transfers their own project to a group member."""
    mod_uid, mod_token = moderator_user
    group_uid, _ = group_user
    resp = await client.post(
        "/api/admin/projects/p-mod/owner",
        json={"user_id": group_uid},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 200, resp.text
    rows = await test_db.query(
        "SELECT owner_id FROM type::record('projects', $id)", {"id": "p-mod"},
    )
    assert rows[0]["owner_id"] == group_uid


@pytest.mark.asyncio
async def test_moderator_transfer_to_foreign_user_404(
    client, test_db, moderator_user, regular_user, scoped_projects,
):
    """A target outside the scope set reads as nonexistent and nothing changes."""
    _, mod_token = moderator_user
    resp = await client.post(
        "/api/admin/projects/p-mod/owner",
        json={"user_id": regular_user[0]},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404
    rows = await test_db.query(
        "SELECT owner_id FROM type::record('projects', $id)", {"id": "p-mod"},
    )
    assert rows[0]["owner_id"] == "test-mod-001"


@pytest.mark.asyncio
async def test_moderator_transfer_on_foreign_project_404(
    client, test_db, moderator_user, group_user, scoped_projects,
):
    """A foreign project reads as nonexistent even with an in-scope target."""
    _, mod_token = moderator_user
    group_uid, _ = group_user
    resp = await client.post(
        "/api/admin/projects/p-admin/owner",
        json={"user_id": group_uid},
        cookies={"lore_session": mod_token},
    )
    assert resp.status_code == 404
    rows = await test_db.query(
        "SELECT owner_id FROM type::record('projects', $id)", {"id": "p-admin"},
    )
    assert rows[0]["owner_id"] != group_uid


@pytest.mark.asyncio
async def test_login_returns_capability_flags(client, moderator_user):
    """Login carries the same capability flags as /me — the gear gate reads
    can_manage_users straight after login, before any /me refresh."""
    resp = await client.post(
        "/api/auth/login", json={"email": "mod@test.com", "password": "pass123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_manage_users"] is True
    assert data["is_admin"] is False
